"""Load / concurrency mode: N virtual users run a scenario repeatedly against a peer.

Every HTTP call is measured (per endpoint template: count, errors, p50/p90/p95/p99, max) and every iteration keeps
its scenario assertions, so the run shows both performance and correctness under concurrency.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..fhir.common import R4_RESOURCE_TYPES, instant
from ..scenarios.runner import ScenarioRunner

LOCAL = ("://localhost", "://127.", "://0.0.0.0", "://[::1]")


@dataclass
class LoadConfig:
    scenario: str
    peer: str = "self"
    users: int = 5
    duration_s: float = 30.0
    iterations: int | None = None  # total iterations across users; stops before duration if reached
    ramp_s: float = 0.0
    think_ms: int = 0
    vars: dict = field(default_factory=dict)
    log_traffic: bool = False
    warmup: bool = True


def endpoint_template(method: str, url: str, base: str) -> str:
    """'GET https://h/fhir/Patient/123/_history/2?x' -> 'GET Patient/{id}/_history/{vid}'."""
    path = url[len(base):] if url.startswith(base) else re.sub(r"^https?://[^/]+", "", url)
    path, _, query = path.partition("?")
    segs = [s for s in path.split("/") if s]
    out = []
    for i, s in enumerate(segs):
        prev = segs[i - 1] if i else None
        if prev in R4_RESOURCE_TYPES and not s.startswith(("$", "_")):
            out.append("{id}")
        elif prev == "_history":
            out.append("{vid}")
        else:
            out.append(s)
    t = "/".join(out) or "(base)"
    if query:
        t += "?search"
    return f"{method} {t}"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


class LoadRunner:
    def __init__(self, ctx, cfg: LoadConfig):
        self.ctx = ctx
        self.cfg = cfg
        self.lock = threading.Lock()
        self.requests: list[tuple[float, str, int | None, float, str | None]] = []
        self.iterations: list[dict[str, Any]] = []
        self.t0 = 0.0
        self._started = 0
        self._stop = threading.Event()

    def _observe(self, method, url, status, ms, err):
        key = endpoint_template(method, url, self.base)
        with self.lock:
            self.requests.append((time.perf_counter() - self.t0, key, status, ms, err))

    def _claim_iteration(self) -> bool:
        with self.lock:
            if self.cfg.iterations is not None and self._started >= self.cfg.iterations:
                return False
            self._started += 1
            return True

    def _user(self, n: int, deadline: float):
        if self.cfg.ramp_s and self.cfg.users > 1:
            time.sleep(self.cfg.ramp_s * n / self.cfg.users)
        runner = ScenarioRunner(self.ctx, self.cfg.peer)
        while not self._stop.is_set() and time.perf_counter() < deadline and self._claim_iteration():
            t = time.perf_counter()
            try:
                r = runner.run(self.cfg.scenario, dict(self.cfg.vars), record=False)
                status = r.status
                bad = next((s for s in r.steps if s.status in ("failed", "error")), None)
                detail = f"{bad.name}: {bad.message[:300]}" if bad else ""
            except Exception as e:  # a crashing iteration is an error, the user keeps going
                status, detail = "error", f"{type(e).__name__}: {e}"
            with self.lock:
                self.iterations.append({"user": n, "t": round(t - self.t0, 3), "ms": round((time.perf_counter() - t) * 1000, 1),
                                        "status": status, "detail": detail})
            if self.cfg.think_ms:
                time.sleep(self.cfg.think_ms / 1000)

    def run(self) -> dict:
        cfg = self.cfg
        peer = self.ctx.settings.peer(cfg.peer)
        self.base = peer.base_url.rstrip("/")
        client = self.ctx.peer_client(cfg.peer)
        if cfg.warmup:  # master data once, so users don't race to create the same Organizations/Locations
            from ..workflows.registry import run_workflow
            run_workflow(self.ctx, "adt.facility", ScenarioRunner(self.ctx, cfg.peer).target_spec(None, "peer"))
        previous_log = client.log_traffic
        client.log_traffic = cfg.log_traffic
        client.observers.append(self._observe)
        spec, _ = ScenarioRunner(self.ctx, cfg.peer).load(cfg.scenario)
        quiet = cfg.peer == "self" and not spec.get("simulator", False)
        if quiet:  # the scenario drives both sides; keep the loopback simulator quiet
            client.extra_headers["X-Sekmet-Simulate"] = "off"
        started = instant()
        self.t0 = time.perf_counter()
        deadline = self.t0 + cfg.duration_s
        threads = [threading.Thread(target=self._user, args=(i, deadline), daemon=True) for i in range(cfg.users)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            self._stop.set()
            for t in threads:
                t.join(timeout=30)
        finally:
            client.observers.remove(self._observe)
            client.log_traffic = previous_log
            if quiet:
                client.extra_headers.pop("X-Sekmet-Simulate", None)
        elapsed = time.perf_counter() - self.t0
        return self._summary(started, elapsed)

    def _summary(self, started: str, elapsed: float) -> dict:
        reqs, its = self.requests, self.iterations
        by_key: dict[str, list] = {}
        for r in reqs:
            by_key.setdefault(r[1], []).append(r)
        endpoints = []
        for key, rows in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
            ms = [r[3] for r in rows]
            endpoints.append({
                "endpoint": key, "count": len(rows),
                "server_errors": sum(1 for r in rows if r[2] is None or r[2] >= 500),
                "client_errors": sum(1 for r in rows if r[2] is not None and 400 <= r[2] < 500),
                "p50": round(percentile(ms, 50), 1), "p90": round(percentile(ms, 90), 1),
                "p95": round(percentile(ms, 95), 1), "p99": round(percentile(ms, 99), 1), "max": round(max(ms), 1),
            })
        seconds = max(1, math.ceil(elapsed))
        timeline = []
        for s in range(seconds):
            rows = [r for r in reqs if s <= r[0] < s + 1]
            timeline.append({"second": s, "requests": len(rows),
                             "errors": sum(1 for r in rows if r[2] is None or r[2] >= 500),
                             "p95": round(percentile([r[3] for r in rows], 95), 1)})
        all_ms = [r[3] for r in reqs]
        n_it = len(its)
        failed_its = [i for i in its if i["status"] in ("failed", "error")]
        errors_5xx = sum(e["server_errors"] for e in endpoints)
        return {
            "config": {**self.cfg.__dict__, "vars": self.cfg.vars},
            "peer_base": self.base, "started": started, "elapsed_s": round(elapsed, 2),
            "requests": len(reqs), "throughput_rps": round(len(reqs) / elapsed, 2) if elapsed else 0,
            "server_error_rate_pct": round(100 * errors_5xx / len(reqs), 3) if reqs else 0.0,
            "latency": {"p50": round(percentile(all_ms, 50), 1), "p95": round(percentile(all_ms, 95), 1),
                        "p99": round(percentile(all_ms, 99), 1), "max": round(max(all_ms), 1) if all_ms else 0},
            "iterations": n_it, "iterations_per_s": round(n_it / elapsed, 2) if elapsed else 0,
            "iteration_failure_rate_pct": round(100 * len(failed_its) / n_it, 2) if n_it else 0.0,
            "iteration_p95_ms": round(percentile([i["ms"] for i in its], 95), 1),
            "failures": _group_failures(failed_its),
            "endpoints": endpoints, "timeline": timeline,
        }


def _group_failures(failed: list[dict]) -> list[dict]:
    groups: dict[str, int] = {}
    for f in failed:
        key = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "{uuid}", f["detail"])[:240]
        groups[key] = groups.get(key, 0) + 1
    return [{"detail": k, "count": v} for k, v in sorted(groups.items(), key=lambda kv: -kv[1])][:20]


def is_local(base_url: str) -> bool:
    return any(h in base_url for h in LOCAL)


def check_gates(summary: dict, max_error_rate: float | None, max_p95_ms: float | None,
                max_iteration_failure: float | None) -> list[str]:
    out = []
    if max_error_rate is not None and summary["server_error_rate_pct"] > max_error_rate:
        out.append(f"server error rate {summary['server_error_rate_pct']}% > {max_error_rate}%")
    if max_p95_ms is not None and summary["latency"]["p95"] > max_p95_ms:
        out.append(f"p95 {summary['latency']['p95']} ms > {max_p95_ms} ms")
    if max_iteration_failure is not None and summary["iteration_failure_rate_pct"] > max_iteration_failure:
        out.append(f"iteration failure rate {summary['iteration_failure_rate_pct']}% > {max_iteration_failure}%")
    return out
