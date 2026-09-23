"""Bulk Data client: kick off $export on a peer, poll (honouring Retry-After), download and validate NDJSON."""
from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime

import httpx

from ..fhir.validation import has_errors, structural_issues


def _retry_after(value: str | None, default: float = 2.0) -> float:
    if not value:
        return default
    try:
        return max(0.2, float(value))
    except ValueError:
        try:
            return max(0.2, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return default


def bulk_export(client, level: str = "system", group: str | None = None, types: list[str] | None = None,
                since: str | None = None, type_filter: list[str] | None = None, timeout: float = 300,
                max_poll_interval: float | None = None, validate_sample: int = 50) -> dict:
    """max_poll_interval=None honours the server's Retry-After (spec: clients SHOULD); a number caps the wait and
    is recorded as a deliberate deviation (for fast test runs)."""
    path = {"system": "$export", "patient": "Patient/$export"}.get(level) or f"Group/{group}/$export"
    params = {}
    if types:
        params["_type"] = ",".join(types)
    if since:
        params["_since"] = since
    if type_filter:
        params["_typeFilter"] = ",".join(type_filter)
    out: dict = {"level": level, "kickoff_status": None, "status_codes": [], "polls": 0, "manifest": None,
                 "files": [], "all_valid": False, "total_resources": 0, "errors": [], "retry_after_requested": [],
                 "poll_cap_s": max_poll_interval}
    t0 = time.perf_counter()
    r = client.request("GET", path, params=params or None,
                       headers={"Prefer": "respond-async", "Accept": "application/fhir+json"}, note="bulk kick-off")
    out["kickoff_status"] = r.status
    if r.status != 202:
        out["errors"].append(f"kick-off returned HTTP {r.status}: {r.outcome_text()}")
        return out
    status_url = r.headers.get("content-location")
    if not status_url:
        out["errors"].append("kick-off 202 without Content-Location")
        return out
    deadline = time.time() + timeout
    while True:
        s = client.request("GET", status_url, absolute=True, headers={"Accept": "application/json"},
                           note="bulk status")
        out["polls"] += 1
        out["status_codes"].append(s.status)
        if s.status == 200:
            out["manifest"] = s.body if isinstance(s.body, dict) else None
            break
        if s.status != 202:
            out["errors"].append(f"status returned HTTP {s.status}: {s.outcome_text()}")
            return out
        if time.time() > deadline:
            out["errors"].append(f"export not complete after {timeout:g}s (last X-Progress: "
                                 f"{s.headers.get('x-progress')})")
            client.request("DELETE", status_url, absolute=True, note="bulk cancel after timeout")
            return out
        wait = _retry_after(s.headers.get("retry-after"))
        out["retry_after_requested"].append(s.headers.get("retry-after"))
        if max_poll_interval is not None:
            wait = min(wait, max_poll_interval)
        time.sleep(max(0.2, min(wait, deadline - time.time())))
    m = out["manifest"] or {}
    for key in ("transactionTime", "request", "requiresAccessToken", "output"):
        if key not in m:
            out["errors"].append(f"manifest lacks '{key}'")
    needs_token = bool(m.get("requiresAccessToken"))
    all_ok = not out["errors"]
    for o in m.get("output", []):
        f = {"type": o.get("type"), "declared": o.get("count"), "lines": 0, "invalid": 0, "wrong_type": 0,
             "issues": []}
        if needs_token:
            resp = client.request("GET", o["url"], absolute=True, headers={"Accept": "application/fhir+ndjson"},
                                  note="bulk file")
            status, text, ctype = resp.status, resp.text, resp.headers.get("content-type", "")
        else:  # e.g. pre-signed storage URLs: do not send our credentials
            rr = httpx.get(o["url"], headers={"Accept": "application/fhir+ndjson"}, timeout=120)
            status, text, ctype = rr.status_code, rr.text, rr.headers.get("content-type", "")
        f["http_status"], f["content_type"] = status, ctype
        if status != 200:
            f["issues"].append(f"HTTP {status}")
            all_ok = False
        for n, line in enumerate(l for l in text.splitlines() if l.strip()):
            f["lines"] += 1
            try:
                res = json.loads(line)
            except ValueError:
                f["invalid"] += 1
                continue
            if res.get("resourceType") != o.get("type"):
                f["wrong_type"] += 1
            if types and res.get("resourceType") not in types:
                f["issues"].append(f"line {n + 1}: {res.get('resourceType')} not in requested _type")
            if n < validate_sample:
                iss = structural_issues(res)
                if has_errors(iss):
                    f["invalid"] += 1
                    f["issues"].append(f"line {n + 1}: " + "; ".join(i.get("diagnostics", "") for i in iss[:2]))
        if o.get("count") is not None and o["count"] != f["lines"]:
            f["issues"].append(f"manifest count {o['count']} != {f['lines']} lines")
        if f["invalid"] or f["wrong_type"] or f["issues"]:
            all_ok = False
        f["issues"] = f["issues"][:10]
        out["files"].append(f)
        out["total_resources"] += f["lines"]
    out["types"] = sorted({f["type"] for f in out["files"]})
    out["all_valid"] = all_ok
    out["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out
