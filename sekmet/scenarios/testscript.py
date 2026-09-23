"""FHIR TestScript (R4) execution, TestReport generation and export of SEKMET runs as TestScripts.

Supported: fixtures (contained, local files, URLs; autocreate/autodelete), variables (defaultValue,
expression, path, headerField, overrides), operations (read vread create update updateCreate delete search
history transaction batch patch capabilities validate + explicit method/url/params/targetId/sourceId,
accept/contentType json|xml, requestHeader, responseId, destination -> peers), asserts (response, responseCode,
resource, contentType, headerField, expression, path (XPath via XML rendering), compareToSource*, value +
operator, minimumId, navigationLinks, validateProfileId (structural), requestURL, requestMethod, warningOnly).
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from lxml import etree

from ..fhir.common import instant, new_id, parse_reference
from ..fhir.validation import has_errors, structural_issues
from ..fhir.xml import FHIR_XML, from_xml, to_xml
from ..traffic.log import set_active_run
from .runner import ScenarioResult, StepResult, fhirpath_check

RESPONSE_CODES = {"okay": 200, "created": 201, "noContent": 204, "notModified": 304, "bad": 400, "forbidden": 403,
                  "notFound": 404, "methodNotAllowed": 405, "conflict": 409, "gone": 410,
                  "preconditionFailed": 412, "unprocessable": 422}
MIME = {"json": "application/fhir+json", "xml": FHIR_XML, "ttl": "text/turtle", "none": None}
VAR_RE = re.compile(r"\$\{([A-Za-z0-9_\-.]+)\}")
NS = {"fhir": "http://hl7.org/fhir"}


class AssertFailed(Exception):
    pass


def load_testscript(path: str | Path) -> dict:
    raw = Path(path).read_bytes()
    doc = from_xml(raw) if raw.lstrip().startswith(b"<") else json.loads(raw)
    if doc.get("resourceType") != "TestScript":
        raise ValueError(f"{path} is not a TestScript (got {doc.get('resourceType')})")
    return doc


class TestScriptRunner:
    """Executes a TestScript against one or more peers (destination index n -> peers[n-1], last peer reused)."""
    __test__ = False  # not a pytest test class

    def __init__(self, ctx, peers: list[str], fixture_dirs: list[str] | None = None,
                 fixture_base_url: str | None = "https://hl7.org/fhir/R4", variables: dict | None = None):
        self.ctx = ctx
        self.peers = peers or ["self"]
        self.fixture_dirs = [Path(d) for d in (fixture_dirs or [])]
        self.fixture_base_url = fixture_base_url
        self.overrides = variables or {}

    # ---------------- setup ----------------

    def _resolve_fixture(self, ts: dict, ref: str) -> dict:
        if ref.startswith("#"):
            found = next((c for c in ts.get("contained", []) if c.get("id") == ref[1:]), None)
            if found is None:
                raise FileNotFoundError(f"contained fixture {ref} not found")
            return copy.deepcopy(found)
        t, i, _ = parse_reference(ref)
        names = [ref, f"{ref}.json", f"{ref}.xml"]
        if t:
            names += [f"{t.lower()}-{i}.json", f"{t.lower()}-{i}.xml", f"{t}/{i}.json", f"{t}-{i}.json"]
        for d in self.fixture_dirs:
            for n in names:
                p = d / n
                if p.is_file():
                    raw = p.read_bytes()
                    return from_xml(raw) if raw.lstrip().startswith(b"<") else json.loads(raw)
        urls = [ref] if ref.startswith(("http://", "https://")) else []
        if t and self.fixture_base_url:
            urls.append(f"{self.fixture_base_url.rstrip('/')}/{t.lower()}-{i}.json")
        for u in urls:
            try:
                r = httpx.get(u, timeout=30, headers={"Accept": "application/fhir+json"}, follow_redirects=True)
                if r.status_code == 200:
                    return r.json()
            except (httpx.HTTPError, ValueError):
                continue
        raise FileNotFoundError(f"fixture {ref} not found (dirs {self.fixture_dirs}, base {self.fixture_base_url})")

    # ---------------- values ----------------

    def _subst(self, value: Any, st: dict) -> Any:
        if isinstance(value, str):
            return VAR_RE.sub(lambda m: str(self._variable(m.group(1), st)), value)
        if isinstance(value, list):
            return [self._subst(v, st) for v in value]
        if isinstance(value, dict):
            return {k: self._subst(v, st) for k, v in value.items()}
        return value

    def _variable(self, name: str, st: dict) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        var = st["variables"].get(name)
        if var is None:
            raise AssertFailed(f"undefined variable ${{{name}}}")
        src = self._source(var.get("sourceId"), st) if var.get("sourceId") else None
        if var.get("headerField"):
            resp = st["responses"].get(var.get("sourceId")) or st.get("last") or {}
            v = (resp.get("headers") or {}).get(var["headerField"].lower())
        elif var.get("expression"):
            ok, res = fhirpath_check(src["body"] if src else None, var["expression"])
            v = res[0] if isinstance(res, list) and res else None
            m = re.fullmatch(r"([A-Z][A-Za-z]+)\.id", var["expression"].strip())
            if v is None and m and src:  # empty-bodied response (return=minimal, conditional hit): use Location
                t, i, _ = parse_reference((src.get("headers") or {}).get("location") or
                                          (src.get("headers") or {}).get("content-location") or "")
                v = i if t == m.group(1) else None
        elif var.get("path"):
            v = self._xpath_value(src["body"] if src else None, var["path"])
        else:
            v = None
        if v in (None, "") and var.get("defaultValue") is not None:
            v = var["defaultValue"]
        if v in (None, ""):
            hint = f" ({var.get('hint')})" if var.get("hint") else ""
            raise AssertFailed(f"variable ${{{name}}} has no value{hint}; pass it with --var {name}=...")
        return v

    def _source(self, source_id: str | None, st: dict) -> dict:
        """{'body', 'headers', 'status'} for a fixture or response id (default: last response)."""
        if not source_id:
            return st.get("last") or {"body": None, "headers": {}, "status": None}
        if source_id in st["responses"]:
            return st["responses"][source_id]
        if source_id in st["fixtures"]:
            return {"body": st["fixtures"][source_id], "headers": {}, "status": None}
        raise AssertFailed(f"unknown sourceId '{source_id}'")

    @staticmethod
    def _xpath_value(body: Any, path: str) -> str | None:
        if not isinstance(body, dict):
            return None
        expr = path.strip()
        if "fhir:" not in expr:  # e.g. "Patient/id" (variables) -> "/fhir:Patient/fhir:id"
            expr = "/".join(s if (not s or s.startswith(("@", "text()", "*")) or "(" in s) else f"fhir:{s}"
                            for s in expr.split("/"))
        try:
            root = etree.fromstring(to_xml(body))
            res = root.getroottree().xpath("/" + expr.lstrip("/"), namespaces=NS)
        except Exception:
            return None
        if not res:
            return None
        v = res[0]
        return v if isinstance(v, str) else (v.get("value") if hasattr(v, "get") else str(v))

    # ---------------- run ----------------

    def run(self, ts: dict, name: str | None = None) -> ScenarioResult:
        run_id = f"ts-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        result = ScenarioResult(run_id, name or ts.get("title") or ts.get("name") or ts.get("id", "TestScript"),
                                ts.get("url") or ts.get("id", ""), ",".join(self.peers), ts.get("description", ""))
        st: dict[str, Any] = {"fixtures": {}, "responses": {}, "variables": {v["name"]: v for v in ts.get("variable", [])},
                              "last": None, "last_request": None, "created": {}, "autodelete": []}
        start = time.perf_counter()
        set_active_run(run_id)
        idx = [0]

        def step(section: str, label: str, kind: str) -> StepResult:
            idx[0] += 1
            sr = StepResult(idx[0], f"{section}: {label}", kind)
            result.steps.append(sr)
            return sr

        try:
            # fixtures
            fixtures_ok = True
            for fx in ts.get("fixture", []):
                sr = step("setup", f"fixture {fx.get('id')}", "fixture")
                try:
                    st["fixtures"][fx["id"]] = self._resolve_fixture(ts, fx.get("resource", {}).get("reference", ""))
                    sr.message = f"loaded {fx.get('resource', {}).get('reference')}"
                    if fx.get("autocreate"):
                        resp = self._send(st, {"type": {"code": "create"}, "sourceId": fx["id"]}, sr)
                        if resp["status"] not in (200, 201):
                            raise AssertFailed(f"autocreate failed: HTTP {resp['status']}")
                    if fx.get("autodelete"):
                        st["autodelete"].append(fx["id"])
                except Exception as e:
                    sr.status, sr.message = "error", str(e)
                    fixtures_ok = False
            setup_ok = fixtures_ok and self._actions(ts.get("setup", {}).get("action", []), "setup", st, step)
            for test in ts.get("test", []):
                label = test.get("name") or test.get("id") or "test"
                if not setup_ok:
                    sr = step(label, "skipped", "test")
                    sr.status, sr.message = "skipped", "setup failed"
                    continue
                self._actions(test.get("action", []), label, st, step)
        finally:
            try:
                self._actions(ts.get("teardown", {}).get("action", []), "teardown", st, step, teardown=True)
                for fx_id in st["autodelete"]:
                    sr = step("teardown", f"autodelete {fx_id}", "operation")
                    self._send(st, {"type": {"code": "delete"}, "targetId": fx_id}, sr)
            finally:
                set_active_run(None)
        result.duration_ms = round((time.perf_counter() - start) * 1000, 1)
        statuses = {s.status for s in result.steps}
        result.status = ("error" if "error" in statuses else "failed" if "failed" in statuses
                         else "warning" if "warning" in statuses else "passed")
        runner_persist(self.ctx, result)
        return result

    def _actions(self, actions: list[dict], section: str, st: dict, step, teardown: bool = False) -> bool:
        ok = True
        for a in actions:
            if "operation" in a:
                op = a["operation"]
                sr = step(section, op.get("label") or op.get("description") or op.get("type", {}).get("code", "operation"),
                          "operation")
                if not ok and not teardown:
                    sr.status, sr.message = "skipped", "previous failure in this section"
                    continue
                try:
                    self._send(st, op, sr)
                except Exception as e:
                    sr.status, sr.message = "error", f"{type(e).__name__}: {e}"
                    ok = False
            elif "assert" in a:
                asr = a["assert"]
                sr = step(section, asr.get("label") or asr.get("description") or "assert", "assert")
                if not ok and not teardown:
                    sr.status, sr.message = "skipped", "previous failure in this section"
                    continue
                try:
                    sr.message = self._assert(st, asr)
                except AssertFailed as e:
                    sr.status = "warning" if asr.get("warningOnly") else "failed"
                    sr.message = str(e)
                    ok = ok and bool(asr.get("warningOnly"))
                except Exception as e:
                    sr.status, sr.message = "error", f"{type(e).__name__}: {e}"
                    ok = False
        return ok

    # ---------------- operations ----------------

    def _send(self, st: dict, op: dict, sr: StepResult) -> dict:
        code = (op.get("type") or {}).get("code") or ""
        rtype = op.get("resource")
        dest = int(op.get("destination") or 1)
        peer = self.peers[min(dest, len(self.peers)) - 1]
        client = self.ctx.peer_client(peer)
        body = copy.deepcopy(st["fixtures"].get(op["sourceId"])) if op.get("sourceId") in st["fixtures"] else \
            (copy.deepcopy(st["responses"][op["sourceId"]]["body"]) if op.get("sourceId") in st["responses"] else None)
        if body is not None:
            body = self._subst(body, st)
            rtype = rtype or body.get("resourceType")
        target = None
        if op.get("targetId"):
            target = st["created"].get(op["targetId"])
            if target is None and op["targetId"] in st["fixtures"]:
                fx = st["fixtures"][op["targetId"]]
                target = (fx.get("resourceType"), fx.get("id"))
            rtype = rtype or (target[0] if target else None)
        method = (op.get("method") or "").upper() or {
            "read": "GET", "vread": "GET", "search": "GET", "history": "GET", "capabilities": "GET",
            "create": "POST", "transaction": "POST", "batch": "POST", "validate": "POST",
            "update": "PUT", "updateCreate": "PUT", "delete": "DELETE", "patch": "PATCH",
        }.get(code, "GET")
        if op.get("url"):
            url = self._subst(op["url"], st)
        else:
            params = self._subst(op.get("params") or "", st)
            if code == "capabilities":
                path = "metadata"
            elif code in ("transaction", "batch"):
                path = ""
            elif params:
                path = f"{rtype or ''}{params}"
            elif target and code in ("read", "vread", "update", "updateCreate", "delete", "patch", "history"):
                path = f"{target[0]}/{target[1]}" + ("/_history" if code == "history" else "")
            elif code in ("update", "updateCreate") and body and body.get("id"):
                path = f"{rtype}/{body['id']}"
            elif code == "validate":
                path = f"{rtype}/$validate"
            else:
                path = rtype or ""
            url = path
        accept = MIME.get(op.get("accept") or "json", op.get("accept"))
        ctype = MIME.get(op.get("contentType") or "json", op.get("contentType"))
        headers = {h["field"]: self._subst(h["value"], st) for h in op.get("requestHeader", [])}
        if accept:
            headers.setdefault("Accept", accept)
        raw = None
        if body is not None and method in ("POST", "PUT", "PATCH"):
            if code in ("update", "updateCreate") and target and body.get("id") != target[1]:
                body["id"] = target[1]
            if method == "PUT" and isinstance(body, dict) and body.get("resourceType"):
                segs = [s for s in urlsplit(url).path.split("/") if s]
                if len(segs) >= 2 and segs[-2] == body["resourceType"]:
                    body["id"] = segs[-1]  # the URL decides the id (exported replays use variables there)
            raw = to_xml(body) if ctype == FHIR_XML else json.dumps(body).encode()
            headers.setdefault("Content-Type", ctype or "application/fhir+json")
        resp = client.request(method, url, raw=raw, headers=headers, absolute=url.startswith(("http://", "https://")))
        record = {"status": resp.status, "headers": resp.headers, "body": resp.body, "url": resp.url}
        st["last"] = record
        st["last_request"] = {"method": method, "url": resp.url, "headers": {k.lower(): v for k, v in headers.items()}}
        if op.get("responseId"):
            st["responses"][op["responseId"]] = record
        if op.get("requestId"):
            st["responses"][op["requestId"]] = {"body": body, "headers": st["last_request"]["headers"], "status": None}
        # remember server ids of resources created from fixtures (targetId resolution)
        if op.get("sourceId") and resp.status in (200, 201):
            t, i, _ = resp.location
            if not t and isinstance(resp.body, dict) and resp.body.get("id"):
                t, i = resp.body.get("resourceType"), resp.body.get("id")
            if t:
                st["created"][op["sourceId"]] = (t, i)
        sr.message = f"{method} {resp.url} -> {resp.status}"
        if resp.traffic_id:
            sr.traffic_ids.append(resp.traffic_id)
        return record

    # ---------------- asserts ----------------

    def _assert(self, st: dict, a: dict) -> str:
        a = self._subst(a, st)
        direction = a.get("direction", "response")
        src = self._source(a.get("sourceId"), st)
        req = st.get("last_request") or {}
        checks: list[str] = []

        def compare(actual: Any, expected: Any, op: str | None, what: str) -> None:
            op = op or "equals"
            s_act = "" if actual is None else (str(actual).lower() if isinstance(actual, bool) else str(actual))
            exp_list = [x.strip() for x in str(expected).split(",")] if expected is not None else []
            ok = {
                "equals": s_act == str(expected), "notEquals": s_act != str(expected),
                "in": s_act in exp_list, "notIn": s_act not in exp_list,
                "contains": expected is not None and str(expected) in s_act,
                "notContains": expected is not None and str(expected) not in s_act,
                "empty": actual in (None, "", []), "notEmpty": actual not in (None, "", []),
                "greaterThan": _num(s_act) > _num(expected), "lessThan": _num(s_act) < _num(expected),
                "eval": bool(actual),
            }.get(op)
            if ok is None:
                raise AssertFailed(f"unsupported operator {op}")
            if not ok:
                raise AssertFailed(f"{what}: expected {op} {expected!r}, got {actual!r}")
            checks.append(f"{what} {op} {expected if expected is not None else ''}".strip())

        if a.get("responseCode"):
            compare(src.get("status"), a["responseCode"], a.get("operator") or ("in" if "," in a["responseCode"] else None),
                    "response code")
        if a.get("response"):
            want = RESPONSE_CODES.get(a["response"])
            compare(src.get("status"), want, "equals", f"response ({a['response']})")
        if a.get("resource"):
            compare((src.get("body") or {}).get("resourceType"), a["resource"], "equals", "resource type")
        if a.get("contentType"):
            hdrs = req.get("headers", {}) if direction == "request" else src.get("headers", {})
            got = (hdrs.get("content-type") or hdrs.get("accept") or "")
            want = MIME.get(a["contentType"], a["contentType"]) or ""
            compare(got, want.split("+")[-1] if "/" in want else want, "contains", "content type")
        if a.get("headerField"):
            hdrs = req.get("headers", {}) if direction == "request" else src.get("headers", {})
            got = hdrs.get(a["headerField"].lower())
            if a.get("value") is not None or a.get("operator") not in (None, "notEmpty"):
                compare(got, a.get("value"), a.get("operator") or "equals", f"header {a['headerField']}")
            else:
                compare(got, None, "notEmpty", f"header {a['headerField']}")
        if a.get("requestMethod"):
            compare((req.get("method") or "").lower(), a["requestMethod"].lower(), "equals", "request method")
        if a.get("requestURL"):
            compare(req.get("url", ""), a["requestURL"], a.get("operator") or "contains", "request URL")
        body = src.get("body")
        if a.get("expression") or a.get("path"):
            if a.get("expression"):
                ok, res = fhirpath_check(body, a["expression"])
                actual = res[0] if isinstance(res, list) and len(res) == 1 else (res if not isinstance(res, list) else
                                                                                  (res or None))
                what = f"expression {a['expression']}"
            else:
                actual = self._xpath_value(body, a["path"])
                ok, what = actual is not None, f"path {a['path']}"
            if a.get("compareToSourceId"):
                other = self._source(a["compareToSourceId"], st).get("body")
                if a.get("compareToSourceExpression"):
                    _, r2 = fhirpath_check(other, a["compareToSourceExpression"])
                    expected = r2[0] if isinstance(r2, list) and r2 else None
                else:
                    expected = self._xpath_value(other, a.get("compareToSourcePath") or a.get("path"))
                compare(actual, expected, a.get("operator"), what)
            elif a.get("value") is not None or a.get("operator"):
                compare(actual, a.get("value"), a.get("operator") or "equals", what)
            elif not ok:
                raise AssertFailed(f"{what} is not true / empty (got {res if a.get('expression') else actual!r})")
            else:
                checks.append(what)
        elif a.get("compareToSourceId") and a.get("compareToSourceExpression"):
            other = self._source(a["compareToSourceId"], st).get("body")
            _, r1 = fhirpath_check(body, a["compareToSourceExpression"])
            _, r2 = fhirpath_check(other, a["compareToSourceExpression"])
            compare(r1[0] if r1 else None, r2[0] if r2 else None, a.get("operator"), a["compareToSourceExpression"])
        if a.get("minimumId"):
            minimum = st["fixtures"].get(a["minimumId"])
            missing = _missing(minimum, body)
            if missing:
                raise AssertFailed(f"response lacks minimum content of {a['minimumId']}: {missing[:5]}")
            checks.append(f"contains minimum {a['minimumId']}")
        if a.get("navigationLinks"):
            links = {l.get("relation") for l in (body or {}).get("link", [])}
            if not ({"self"} <= links or {"first", "last"} <= links):
                raise AssertFailed(f"Bundle navigation links missing (have {sorted(links)})")
            checks.append("navigation links")
        if a.get("validateProfileId"):
            issues = structural_issues(body) if isinstance(body, dict) else [{"severity": "error", "diagnostics": "no body"}]
            if has_errors(issues):
                raise AssertFailed("validation: " + "; ".join(i.get("diagnostics", "") for i in issues[:3]))
            checks.append(f"valid against {a['validateProfileId']} (structural)")
        if not checks and not a.get("expression"):
            raise AssertFailed("assert has no supported check")
        return "; ".join(checks)


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _missing(minimum: Any, actual: Any, path: str = "") -> list[str]:
    """Elements of `minimum` not present in `actual` (lists: every minimum item must match some actual item)."""
    if minimum is None:
        return []
    if isinstance(minimum, dict):
        if not isinstance(actual, dict):
            return [path or "/"]
        out = []
        for k, v in minimum.items():
            if k in ("id", "meta", "text"):
                continue
            out += _missing(v, actual.get(k), f"{path}.{k}")
        return out
    if isinstance(minimum, list):
        if not isinstance(actual, list):
            return [path]
        return [f"{path}[{i}]" for i, m in enumerate(minimum) if all(_missing(m, a, path) for a in actual)]
    return [] if minimum == actual else [path]


def runner_persist(ctx, result: ScenarioResult) -> None:
    with ctx.store.lock:
        ctx.store.conn.execute(
            "INSERT OR REPLACE INTO runs(id, ts, scenario, peer, status, summary, result) VALUES (?,?,?,?,?,?,?)",
            (result.run_id, result.started, result.name, result.peer, result.status, json.dumps(result.counts),
             json.dumps(result.to_dict())))


# ---------------- TestReport ----------------

_TR_RESULT = {"passed": "pass", "warning": "warning", "failed": "fail", "error": "error", "skipped": "skip"}


def build_test_report(result: "ScenarioResult | dict", script_ref: str | None = None) -> dict:
    """TestReport resource for a TestScript run (or any SEKMET scenario run, live object or stored dict)."""
    if isinstance(result, dict):
        from types import SimpleNamespace
        d = result
        result = SimpleNamespace(**{k: d.get(k) for k in ("name", "file", "peer", "status")},
                                 steps=[SimpleNamespace(**s) for s in d.get("steps", [])],
                                 counts=d.get("counts") or {})
    setup, tests, teardown = [], {}, []
    for s in result.steps:
        section, _, label = s.name.partition(": ")
        entry = {("assert" if s.kind == "assert" else "operation"): {
            "result": _TR_RESULT.get(s.status, "error"), "message": (s.message or label)[:1000]}}
        if section == "setup":
            setup.append(entry)
        elif section == "teardown":
            teardown.append({"operation": entry.get("operation") or {"result": entry["assert"]["result"],
                                                                     "message": entry["assert"]["message"]}})
        else:
            tests.setdefault(section, []).append(entry)
    counts = result.counts
    total = sum(v for k, v in counts.items() if k != "skipped") or 1
    report: dict[str, Any] = {
        "resourceType": "TestReport",
        "id": new_id(),
        "name": result.name,
        "status": "completed",
        "testScript": {"reference": script_ref or result.file or "urn:sekmet:scenario", "display": result.name},
        "result": "fail" if result.status in ("failed", "error") else "pass",
        "score": round(100 * counts.get("passed", 0) / total, 1),
        "tester": "SEKMET FHIR R4 Tester",
        "issued": instant(),
        "participant": [{"type": "server", "uri": p} for p in result.peer.split(",")],
    }
    if setup:
        report["setup"] = {"action": setup}
    if tests:
        report["test"] = [{"name": n, "action": acts} for n, acts in tests.items()]
    if teardown:
        report["teardown"] = {"action": teardown}
    return report


# ---------------- export: SEKMET run -> TestScript ----------------

def export_run(ctx, run: dict, peer_base: str) -> dict:
    """Turn the outbound traffic of a run into a replayable TestScript: each request becomes an operation (bodies as
    contained fixtures), created ids become variables, and each response code is asserted."""
    tids = [t for s in run["steps"] for t in s.get("traffic_ids", [])]
    rows = {r["id"]: dict(r) for r in ctx.store.query(
        f"SELECT * FROM traffic WHERE id IN ({','.join('?' * len(tids))}) AND direction='outbound' ORDER BY id",
        tids)} if tids else {}
    base = peer_base.rstrip("/")
    contained, variables, actions, id_vars = [], [], [], {}
    for n, tid in enumerate(sorted(rows), start=1):
        r = rows[tid]
        if not (r["url"] or "").startswith(base) or "/auth/" in r["url"]:
            continue
        path = r["url"][len(base):].lstrip("/")
        for real, var in id_vars.items():
            path = path.replace(real, f"${{{var}}}")
        op: dict[str, Any] = {"type": {"system": "http://terminology.hl7.org/CodeSystem/testscript-operation-codes",
                                       "code": _op_code(r["method"], path)},
                              "method": r["method"].lower(), "url": f"{base}/{path}" if path else base,
                              "accept": "json", "encodeRequestUrl": False, "responseId": f"r{n}",
                              "description": f"{r['method']} {path or '(base)'}"}
        op["url"] = op["url"].replace(base, "${baseUrl}", 1)
        if r["req_body"]:
            try:
                body = json.loads(r["req_body"])
            except ValueError:
                body = from_xml(r["req_body"]) if r["req_body"].lstrip().startswith("<") else None
            if isinstance(body, dict) and body.get("resourceType"):
                body = _strip_put_entry_ids(json.loads(_replace_ids(json.dumps(body), id_vars)))
                fx_id = f"fx{n}"
                contained.append(dict(body, id=fx_id))  # PUT targets come from the URL at replay time
                op["sourceId"] = fx_id
                op["contentType"] = "json"
        try:
            req_headers = json.loads(r["req_headers"] or "{}")
        except ValueError:
            req_headers = {}
        kept = {k: v for k, v in req_headers.items() if k.lower() in ("if-none-exist", "if-match", "if-none-match",
                                                                       "prefer", "if-modified-since")}
        if kept:
            op["requestHeader"] = [{"field": k, "value": _replace_ids_plain(v, id_vars)} for k, v in kept.items()]
        codes = str(r["status"])
        if "if-none-exist" in {k.lower() for k in kept} and r["status"] in (200, 201):
            codes = "200,201"  # conditional create: creates or finds depending on server state
        actions.append({"operation": op})
        actions.append({"assert": {"description": f"expect HTTP {codes}", "direction": "response",
                                   "responseCode": codes, "operator": "in" if "," in codes else "equals",
                                   "warningOnly": False}})
        # a create answered with a new id: later references to it go through a variable
        try:
            resp = json.loads(r["resp_body"]) if r["resp_body"] else None
        except ValueError:
            resp = None
        if r["method"] == "POST" and isinstance(resp, dict) and resp.get("id") and resp.get("resourceType") not in (
                "Bundle", "OperationOutcome", "Parameters"):
            var = f"id{n}"
            id_vars[resp["id"]] = var
            variables.append({"name": var, "expression": f"{resp['resourceType']}.id", "sourceId": f"r{n}"})
        elif isinstance(resp, dict) and resp.get("resourceType") == "Bundle" and resp.get("type", "").endswith("-response"):
            for k, e in enumerate(resp.get("entry", [])):
                loc = (e.get("response") or {}).get("location", "")
                t, i, _ = parse_reference(loc)
                if i and i not in id_vars:
                    var = f"id{n}_{k}"
                    id_vars[i] = var
                    variables.append({"name": var, "expression": f"Bundle.entry[{k}].resource.id", "sourceId": f"r{n}"})
    fixtures = [{"id": c["id"], "autocreate": False, "autodelete": False, "resource": {"reference": f"#{c['id']}"}}
                for c in contained]
    return {
        "resourceType": "TestScript",
        "id": f"sekmet-{run['run_id']}",
        "url": f"urn:sekmet:testscript:{run['run_id']}",
        "name": re.sub(r"[^A-Za-z0-9]", "", run["name"].title())[:60] or "SekmetExport",
        "title": f"{run['name']} (exported from SEKMET run {run['run_id']})",
        "status": "draft",
        "date": instant(),
        "publisher": "SEKMET FHIR R4 Tester",
        "description": run.get("description") or "Exported from a SEKMET scenario run",
        "contained": contained,
        "fixture": fixtures,
        "variable": [{"name": "baseUrl", "defaultValue": base}, *variables],
        "test": [{"name": run["name"][:60], "description": "Replay of the recorded requests with response-code asserts",
                  "action": actions}],
    }


def _replace_ids(text: str, id_vars: dict[str, str]) -> str:
    """Parameterize recorded server ids inside a JSON body (references ".../Type/<id>")."""
    for real, var in id_vars.items():
        text = text.replace(f"/{real}\"", f"/${{{var}}}\"")
    return text


def _replace_ids_plain(text: str, id_vars: dict[str, str]) -> str:
    for real, var in id_vars.items():
        text = text.replace(real, f"${{{var}}}")
    return text


def _strip_put_entry_ids(body: dict) -> dict:
    """Transaction/batch PUT entries: the (parameterized) request.url carries the id, so resource.id is dropped
    to keep the fixture a valid resource."""
    if body.get("resourceType") == "Bundle":
        for e in body.get("entry", []):
            if (e.get("request") or {}).get("method") == "PUT" and isinstance(e.get("resource"), dict):
                e["resource"].pop("id", None)
    return body


def _op_code(method: str, path: str) -> str:
    segs = [s for s in path.split("?")[0].split("/") if s]
    if method == "POST":
        if not segs:
            return "transaction"
        return "search" if segs[-1] == "_search" else ("validate" if segs[-1] == "$validate" else "create")
    if method == "PUT":
        return "update"
    if method == "DELETE":
        return "delete"
    if method == "PATCH":
        return "patch"
    if segs and segs[0] == "metadata":
        return "capabilities"
    if "_history" in segs:
        return "vread" if segs[-1] != "_history" else "history"
    return "read" if len(segs) == 2 else "search"

