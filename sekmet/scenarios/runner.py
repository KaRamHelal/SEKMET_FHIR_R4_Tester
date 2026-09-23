"""YAML scenario runner.

Step kinds:
  action:    run a registered workflow on a target            (args, target, save)
  request:   raw FHIR HTTP request to the peer (or local)      (method, path, params, body, headers)
  expect:    assertions on the step response (status, headers, fhirpath) - attaches to action/request
  assert:    fhirpath assertions on any saved value            ({on, fhirpath})
  wait_for:  poll until a condition holds                     (notification | search), timeout, min
  subscribe: create a rest-hook Subscription on the peer pointing to our /hooks endpoint (auto cleanup)
  set:       set variables
  sleep:     seconds
  validate:  structural (+ optional HL7 validator) validation of a saved resource
Values support ${dotted.path} interpolation over saved variables.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fhirpathpy import evaluate as fp_evaluate

from ..fhir.common import instant
from ..fhir.search import parse_criteria
from ..fhir.validation import has_errors, validate
from ..subscriptions.engine import notifications
from ..traffic.log import set_active_run
from ..workflows.registry import run_workflow
from ..workflows.target import make_target

LIBRARY = Path(__file__).parent / "library"
VAR_RE = re.compile(r"\$\{([^}]+)\}")


class StepFailed(Exception):
    pass


@dataclass
class StepResult:
    index: int
    name: str
    kind: str
    status: str = "passed"  # passed | warning | failed | error | skipped
    message: str = ""
    duration_ms: float = 0
    traffic_ids: list[int] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)


@dataclass
class ScenarioResult:
    run_id: str
    name: str
    file: str
    peer: str
    description: str = ""
    status: str = "passed"
    started: str = field(default_factory=instant)
    duration_ms: float = 0
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.status] = out.get(s.status, 0) + 1
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["counts"] = self.counts
        return d


# ---------------- interpolation ----------------

def lookup(vars_: dict, path: str) -> Any:
    cur: Any = vars_
    for part in path.strip().split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"${{{path}}}: '{part}' not found")
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(f"${{{path}}}: bad list index '{part}'")
        elif hasattr(cur, part):
            cur = getattr(cur, part)
        else:
            raise KeyError(f"${{{path}}}: cannot descend into {type(cur).__name__}")
    return cur


def interpolate(value: Any, vars_: dict) -> Any:
    if isinstance(value, str):
        m = VAR_RE.fullmatch(value.strip())
        if m:
            return copy.deepcopy(lookup(vars_, m.group(1)))
        return VAR_RE.sub(lambda mm: _as_text(lookup(vars_, mm.group(1))), value)
    if isinstance(value, list):
        return [interpolate(v, vars_) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, vars_) for k, v in value.items()}
    return value


def _as_text(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


def fhirpath_check(resource: Any, expr: str) -> tuple[bool, Any]:
    try:
        result = fp_evaluate(resource, expr, {})
    except Exception as e:
        return False, f"error: {e}"
    ok = bool(result) and all(r is True or (r not in (False, None) and r != []) for r in result)
    return ok, result


# ---------------- runner ----------------

class ScenarioRunner:
    def __init__(self, app_ctx, peer: str = "self"):
        self.ctx = app_ctx
        self.peer = peer
        self._caps: dict | None = None

    def load(self, ref: str) -> tuple[dict, str]:
        p = Path(ref)
        if not p.exists():
            for base in (Path("scenarios"), LIBRARY):
                for cand in (base / ref, base / f"{ref}.yaml", base / f"{ref}.yml"):
                    if cand.exists():
                        p = cand
                        break
                if p.exists():
                    break
        if not p.exists():
            raise FileNotFoundError(f"Scenario '{ref}' not found (looked in ./scenarios and the built-in library)")
        return _yaml_keys(yaml.safe_load(p.read_text())), str(p)

    def target_spec(self, name: str | None, default: str) -> str:
        name = name or default
        if name == "local":
            return "local"
        if name == "peer":
            return self.peer
        if name in ("rest", "messaging"):
            return f"{name}:{self.peer}"
        return name

    def run(self, ref: str | dict, vars_: dict | None = None) -> ScenarioResult:
        if isinstance(ref, dict):
            spec, path = _yaml_keys(ref), "<inline>"
        else:
            spec, path = self.load(ref)
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        result = ScenarioResult(run_id, spec.get("name", Path(path).stem), path, self.peer, spec.get("description", ""))
        peer_cfg = self.ctx.settings.peer(self.peer)
        v: dict[str, Any] = {
            "peer": self.peer, "peer_base": peer_cfg.base_url.rstrip("/"), "run_id": run_id,
            "now": instant(), "today": instant()[:10], "uid": uuid.uuid4().hex[:8],
            "settings": json.loads(self.ctx.settings.model_dump_json()),
            "ids": self.ctx.settings.identifiers.model_dump(),
            **spec.get("vars", {}), **(vars_ or {}),
        }
        state = {"baseline_notification": self._max_notification(), "cleanup": [], "last": None}
        default_target = spec.get("target", "peer")
        requires = spec.get("requires", {})
        start = time.perf_counter()
        set_active_run(run_id)
        loopback = self.ctx.peer_client("self") if self.peer == "self" else None
        if loopback is not None and not spec.get("simulator", False):
            loopback.extra_headers["X-Sekmet-Simulate"] = "off"
        try:
            skip_reason = self._check_requires(requires)
            failed = False
            for i, step in enumerate(spec.get("steps", [])):
                kind = next((k for k in ("action", "request", "assert", "wait_for", "subscribe", "set", "sleep",
                                         "validate") if k in step), "unknown")
                sr = StepResult(i + 1, step.get("name") or f"{kind} {step.get(kind) if isinstance(step.get(kind), str) else ''}".strip(), kind)
                step_skip = self._check_requires(step.get("requires") or {})
                if skip_reason or step_skip or (failed and not step.get("always", False)):
                    sr.status, sr.message = "skipped", skip_reason or step_skip or "previous step failed"
                    result.steps.append(sr)
                    continue
                t0 = time.perf_counter()
                tmark = self._max_traffic()
                try:
                    self._step(step, kind, v, state, default_target, sr)
                except StepFailed as e:
                    # level: should -> spec SHOULD / best practice: report as a warning, never block later steps
                    sr.status = "warning" if step.get("level") == "should" else "failed"
                    sr.message = str(e)
                except Exception as e:
                    sr.status, sr.message = "error", f"{type(e).__name__}: {e}"
                    if not isinstance(e, (KeyError, ValueError)):
                        sr.message += "\n" + traceback.format_exc(limit=3)
                sr.duration_ms = round((time.perf_counter() - t0) * 1000, 1)
                sr.traffic_ids = self._traffic_since(tmark, run_id)
                result.steps.append(sr)
                if sr.status in ("failed", "error") and not step.get("continue_on_failure",
                                                                     spec.get("continue_on_failure", False)):
                    failed = True
        finally:
            self._cleanup(state["cleanup"])
            set_active_run(None)
            if loopback is not None:
                loopback.extra_headers.pop("X-Sekmet-Simulate", None)
        result.duration_ms = round((time.perf_counter() - start) * 1000, 1)
        statuses = {s.status for s in result.steps}
        result.status = ("error" if "error" in statuses else "failed" if "failed" in statuses
                         else "skipped" if statuses == {"skipped"} else "warning" if "warning" in statuses
                         else "passed")
        self._persist(result)
        return result

    # ---------------- steps ----------------

    def _step(self, step: dict, kind: str, v: dict, state: dict, default_target: str, sr: StepResult) -> None:
        if kind == "action":
            tspec = self.target_spec(step.get("target"), default_target)
            args = interpolate(step.get("args", {}), v)
            out = run_workflow(self.ctx, step["action"], tspec, args)
            sr.message = f"{step['action']} on {out.get('_meta', {}).get('target')}"
            acks = out.get("_meta", {}).get("acks") or []
            if acks:
                sr.checks += [{"check": f"message {a['event']} ack", "ok": a["ok"], "detail": a["detail"]} for a in acks]
            state["last"] = out
            self._expect(step.get("expect"), out, None, v, sr)
        elif kind == "request":
            req = interpolate(step["request"], v)
            resp = self._request(req, step.get("target"))
            state["last"] = resp
            sr.message = f"{req.get('method', 'GET')} {req.get('path', '')} -> {resp['status']}"
            out = resp
            self._expect(step.get("expect", {"status": [200, 201]}), resp.get("body"), resp, v, sr)
        elif kind == "assert":
            spec = interpolate(step["assert"], v) if not isinstance(step["assert"], list) else {"fhirpath": step["assert"]}
            on = spec.get("on", state["last"])
            if isinstance(on, str):
                on = lookup(v, on)
            out = on
            self._fhirpath(spec.get("fhirpath", []), on, v, sr)
            if spec.get("equals"):
                for path, want in spec["equals"].items():
                    got = lookup(v, path)
                    ok = got == want
                    sr.checks.append({"check": f"{path} == {want!r}", "ok": ok, "detail": repr(got)})
            self._raise_if_failed(sr)
        elif kind == "wait_for":
            out = self._wait(interpolate(step["wait_for"], v), state, sr)
        elif kind == "subscribe":
            out = self._subscribe(interpolate(step["subscribe"], v), state, sr)
        elif kind == "set":
            vals = interpolate(step["set"], v)
            v.update(vals)
            out = vals
        elif kind == "sleep":
            time.sleep(float(step["sleep"]))
            out = None
        elif kind == "validate":
            spec = interpolate(step["validate"], v)
            res = spec["resource"] if isinstance(spec, dict) else spec
            oo = validate(res, self.ctx.settings, conformance=bool(spec.get("conformance", True)) if isinstance(spec, dict) else True)
            errs = [i for i in oo.get("issue", []) if i.get("severity") in ("error", "fatal")]
            sr.checks.append({"check": "validation", "ok": not has_errors(oo.get("issue", [])),
                              "detail": "; ".join(i.get("diagnostics", "") for i in errs[:5]) or "no errors"})
            out = oo
            self._raise_if_failed(sr)
        else:
            raise StepFailed(f"Unknown step kind in {list(step)}")
        if step.get("save"):
            v[step["save"]] = out

    def _request(self, req: dict, target: str | None) -> dict:
        method = req.get("method", "GET").upper()
        path = req.get("path", "")
        if target == "local":
            raise StepFailed("request steps go over HTTP; use target 'self' to hit this server")
        client = self.ctx.peer_client(self.peer if target in (None, "peer") else target)
        r = client.request(method, path, params=req.get("params"), body=req.get("body"), headers=req.get("headers"),
                           content_type=req.get("content_type", "application/fhir+json"))
        return {"status": r.status, "headers": r.headers, "body": r.body, "location": list(r.location),
                "traffic_id": r.traffic_id}

    def _expect(self, exp: dict | None, body: Any, resp: dict | None, v: dict, sr: StepResult) -> None:
        if not exp:
            return
        exp = interpolate(exp, v)
        if resp is not None and "status" in exp:
            want = exp["status"] if isinstance(exp["status"], list) else [exp["status"]]
            ok = resp["status"] in want
            detail = str(resp["status"])
            if not ok and isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
                detail += " " + "; ".join(i.get("diagnostics", "") or i.get("code", "") for i in body.get("issue", []))
            sr.checks.append({"check": f"status in {want}", "ok": ok, "detail": detail[:500]})
        for h, want in (exp.get("headers") or {}).items():
            got = (resp or {}).get("headers", {}).get(h.lower())
            ok = got is not None if want == "present" else got == want
            sr.checks.append({"check": f"header {h} {want}", "ok": ok, "detail": str(got)})
        if exp.get("resource_type") and isinstance(body, dict):
            ok = body.get("resourceType") == exp["resource_type"]
            sr.checks.append({"check": f"resourceType == {exp['resource_type']}", "ok": ok,
                              "detail": str(body.get("resourceType"))})
        on = body
        if exp.get("on") and isinstance(body, dict):
            on = lookup(body, exp["on"])
        self._fhirpath(exp.get("fhirpath", []), on, v, sr)
        self._raise_if_failed(sr)

    def _fhirpath(self, exprs: list[str], resource: Any, v: dict, sr: StepResult) -> None:
        for expr in exprs or []:
            expr = interpolate(expr, v)
            ok, result = fhirpath_check(resource, expr)
            sr.checks.append({"check": f"fhirpath: {expr}", "ok": ok, "detail": _short(result)})

    @staticmethod
    def _raise_if_failed(sr: StepResult) -> None:
        bad = [c for c in sr.checks if not c["ok"]]
        if bad:
            raise StepFailed("; ".join(f"{c['check']} (got {c['detail']})" for c in bad)[:2000])

    def _wait(self, spec: dict, state: dict, sr: StepResult) -> Any:
        timeout = float(spec.get("timeout", 30))
        minimum = int(spec.get("min", 1))
        deadline = time.time() + timeout
        last: Any = None
        while True:
            if "notification" in spec:
                n = spec["notification"] or {}
                rows = notifications(self.ctx.store, since_id=state["baseline_notification"],
                                     peer=n.get("peer"), rtype=n.get("resource_type"))
                if n.get("resource_id"):
                    rows = [r for r in rows if r.get("resource_id") == n["resource_id"]]
                if n.get("fhirpath"):
                    rows = [r for r in rows if r.get("resource") and fhirpath_check(r["resource"], n["fhirpath"])[0]]
                if n.get("include_pings") is False:
                    rows = [r for r in rows if r.get("resource")]
                last = rows
                if len(rows) >= minimum:
                    sr.message = f"{len(rows)} notification(s) received"
                    return rows[0].get("resource") if minimum == 1 else [r.get("resource") for r in rows]
            elif "search" in spec:
                s = spec["search"]
                rtype, params = (parse_criteria(s["criteria"]) if "criteria" in s
                                 else (s["type"], list((s.get("params") or {}).items())))
                target = make_target(self.ctx, self.target_spec(s.get("target"), "peer"))
                found = target.search(rtype, params)
                expr = s.get("fhirpath") or spec.get("fhirpath")  # accepted inside search: or beside it
                if expr:
                    found = [r for r in found if fhirpath_check(r, expr)[0]]
                last = found
                if len(found) >= minimum:
                    sr.message = f"{len(found)} {rtype} found"
                    return found[0] if minimum == 1 else found
            else:
                raise StepFailed("wait_for needs 'notification' or 'search'")
            if time.time() >= deadline:
                raise StepFailed(f"Timed out after {timeout:g}s waiting (have {len(last or [])}, need {minimum})")
            time.sleep(float(spec.get("interval", 1)))

    def _subscribe(self, spec: dict, state: dict, sr: StepResult) -> dict:
        key = secrets.token_hex(6)
        peer_name = spec.get("peer", self.peer)
        endpoint = f"{self.ctx.settings.root_url}/hooks/{peer_name}/{key}"
        sub = {"resourceType": "Subscription", "status": "requested", "reason": spec.get("reason", "SEKMET scenario"),
               "criteria": spec["criteria"],
               "channel": {"type": "rest-hook", "endpoint": endpoint,
                           "header": [f"Authorization: Bearer {self.ctx.settings.subscriptions.hook_token}"]}}
        if spec.get("payload", "application/fhir+json"):
            sub["channel"]["payload"] = spec.get("payload", "application/fhir+json")
        if spec.get("end"):
            sub["end"] = spec["end"]
        client = self.ctx.peer_client(peer_name)
        r = client.create(sub)
        self.ctx.store.kv_set(f"hook:{peer_name}:{key}", json.dumps({"criteria": spec["criteria"], "since": instant()}))
        sr.checks.append({"check": "Subscription created", "ok": r.ok, "detail": f"HTTP {r.status} {r.outcome_text() if not r.ok else ''}"})
        self._raise_if_failed(sr)
        created = client.fetch_resource(r, "Subscription")
        if spec.get("cleanup", True):
            state["cleanup"].append((peer_name, "Subscription", created["id"]))
        wait = float(spec.get("wait_active", 10))
        deadline = time.time() + wait
        while wait and created.get("status") != "active" and time.time() < deadline:
            time.sleep(0.5)
            rr = client.read("Subscription", created["id"])
            if rr.ok:
                created = rr.resource
        sr.checks.append({"check": "Subscription status active", "ok": created.get("status") == "active" or not wait,
                          "detail": f"{created.get('status')} {created.get('error', '')}"})
        self._raise_if_failed(sr)
        sr.message = f"Subscription/{created['id']} -> {endpoint}"
        return created

    def _cleanup(self, items: list[tuple[str, str, str]]) -> None:
        for peer_name, rtype, rid in items:
            try:
                self.ctx.peer_client(peer_name).delete(rtype, rid)
            except Exception:
                pass

    def _check_requires(self, req: dict) -> str | None:
        if not req:
            return None
        peer = self.ctx.settings.peer(self.peer)
        if req.get("mode") and peer.mode != req["mode"] and req["mode"] != "any":
            return f"requires peer mode {req['mode']} (peer is {peer.mode})"
        if req.get("simulator") and not self.ctx.settings.simulator.enabled:
            return "requires the simulator to be enabled"
        if req.get("loopback") and self.peer != "self":
            return "requires the loopback peer 'self'"
        if req.get("role"):
            peer = self.ctx.settings.peer(self.peer)
            if req["role"] not in peer.roles:
                return (f"peer does not play role '{req['role']}' (declare peers.{self.peer}.roles to test it)")
        if req.get("public_url") and self.peer != "self":
            root = self.ctx.settings.root_url
            if any(h in root for h in ("://localhost", "://127.", "://0.0.0.0", "://[::1]")):
                return (f"needs SEKMET reachable by the peer for callbacks; set public_url "
                        f"(currently {root})")
        return self._capability_gap(req)

    def _capabilities(self) -> dict:
        if self._caps is None:
            try:
                r = self.ctx.peer_client(self.peer).capabilities()
                self._caps = r.resource if r.ok and (r.resource or {}).get("resourceType") == "CapabilityStatement" else {}
            except Exception:
                self._caps = {}
        return self._caps

    def _capability_gap(self, req: dict | None) -> str | None:
        """Skip reason when the peer's CapabilityStatement does not advertise what a step/scenario needs.
        Keys: resource, interaction (+ resource for type-level), operation, messaging, subscription."""
        if not req:
            return None
        cs = self._capabilities()
        if not cs:
            return None  # unknown capabilities: run it and let the result speak
        rest = next((r for r in cs.get("rest", []) if r.get("mode") == "server"), {})
        types = {r.get("type"): r for r in rest.get("resource", [])}
        rtype = req.get("resource")
        if rtype and rtype not in types:
            return f"peer does not advertise resource {rtype}"
        if req.get("interaction"):
            codes = ({i.get("code") for i in types.get(rtype, {}).get("interaction", [])} if rtype
                     else {i.get("code") for i in rest.get("interaction", [])})
            if req["interaction"] not in codes:
                return f"peer does not advertise {req['interaction']}{' on ' + rtype if rtype else ''}"
        if req.get("operation"):
            name = req["operation"].lstrip("$")
            ops = {o.get("name", "").lstrip("$") for o in rest.get("operation", [])}
            ops |= {o.get("name", "").lstrip("$") for r in rest.get("resource", []) for o in r.get("operation", [])}
            if name not in ops and not (name == "process-message" and cs.get("messaging")):
                return f"peer does not advertise ${name}"
        if req.get("messaging") and not cs.get("messaging") and "process-message" not in {
                o.get("name", "").lstrip("$") for o in rest.get("operation", [])}:
            return "peer does not advertise FHIR messaging"
        return None

    # ---------------- bookkeeping ----------------

    def _max_notification(self) -> int:
        rows = self.ctx.store.query("SELECT COALESCE(MAX(id), 0) FROM notifications")
        return rows[0][0]

    def _max_traffic(self) -> int:
        return self.ctx.store.query("SELECT COALESCE(MAX(id), 0) FROM traffic")[0][0]

    def _traffic_since(self, mark: int, run_id: str) -> list[int]:
        return [r[0] for r in self.ctx.store.query("SELECT id FROM traffic WHERE id > ? AND run_id = ? ORDER BY id",
                                                   (mark, run_id))]

    def _persist(self, result: ScenarioResult) -> None:
        with self.ctx.store.lock:
            self.ctx.store.conn.execute(
                "INSERT OR REPLACE INTO runs(id, ts, scenario, peer, status, summary, result) VALUES (?,?,?,?,?,?,?)",
                (result.run_id, result.started, result.name, result.peer, result.status, json.dumps(result.counts),
                 json.dumps(result.to_dict())))


def _yaml_keys(node: Any) -> Any:
    """YAML 1.1 parses the key `on:` as boolean True; map it back so scenarios can say `on: encounter`."""
    if isinstance(node, dict):
        return {("on" if k is True else "off" if k is False else k): _yaml_keys(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_yaml_keys(v) for v in node]
    return node


def _short(v: Any, n: int = 300) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + "..."


def list_scenarios() -> list[dict]:
    out, seen = [], set()
    for base in (Path("scenarios"), LIBRARY):
        if not base.exists():
            continue
        for p in sorted(base.glob("*.y*ml")):
            if p.stem in seen:
                continue
            seen.add(p.stem)
            try:
                spec = yaml.safe_load(p.read_text()) or {}
            except yaml.YAMLError as e:
                spec = {"name": p.stem, "description": f"YAML error: {e}"}
            out.append({"id": p.stem, "path": str(p), "name": spec.get("name", p.stem),
                        "description": spec.get("description", ""), "tags": spec.get("tags", []),
                        "steps": len(spec.get("steps", [])), "requires": spec.get("requires", {})})
    return out


def load_run(store, run_id: str) -> dict | None:
    rows = store.query("SELECT result FROM runs WHERE id=?", (run_id,))
    return json.loads(rows[0][0]) if rows else None


def list_runs(store, limit: int = 50) -> list[dict]:
    rows = store.query("SELECT id, ts, scenario, peer, status, summary FROM runs ORDER BY ts DESC LIMIT ?", (limit,))
    return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]

