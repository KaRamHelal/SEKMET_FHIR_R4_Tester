"""Transaction and batch Bundle processing (R4 rules: DELETE, POST, PUT/PATCH, GET order; urn:uuid rewriting)."""
from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .common import FhirError, instant, new_id, operation_outcome, parse_reference, walk_references
from .service import FhirService, Result

STATUS_TEXT = {200: "200 OK", 201: "201 Created", 204: "204 No Content", 304: "304 Not Modified",
               400: "400 Bad Request", 404: "404 Not Found", 409: "409 Conflict", 410: "410 Gone",
               412: "412 Precondition Failed", 422: "422 Unprocessable Entity"}
ORDER = {"DELETE": 0, "POST": 1, "PUT": 2, "PATCH": 2, "GET": 3, "HEAD": 3}


def _split_url(svc: FhirService, url: str) -> tuple[list[str], list[tuple[str, str]]]:
    if url.startswith(svc.base_url):
        url = url[len(svc.base_url):]
    parts = urlsplit(url)
    return [p for p in parts.path.split("/") if p], parse_qsl(parts.query, keep_blank_values=True)


def process_bundle(svc: FhirService, bundle: Any, origin: str = "external", prefer: str | None = None) -> Result:
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise FhirError(400, "Expected a Bundle", "invalid")
    btype = bundle.get("type")
    if btype not in ("transaction", "batch"):
        raise FhirError(400, f"Bundle.type must be transaction or batch (got {btype})", "invalid")
    entries = bundle.get("entry", []) or []
    for i, e in enumerate(entries):
        req = e.get("request") or {}
        if not req.get("method") or not req.get("url"):
            raise FhirError(400, f"Entry {i}: request.method and request.url are required", "required")
    if btype == "transaction":
        try:
            with svc.store.transaction():
                responses = _run(svc, entries, origin, prefer, atomic=True)
        except FhirError as e:
            raise FhirError(e.status, f"Transaction failed and was rolled back: {e.message}", issues=[
                {**i, "diagnostics": f"Transaction rolled back: {i.get('diagnostics')}"} for i in e.issues])
    else:
        responses = _run(svc, entries, origin, prefer, atomic=False)
    out = {"resourceType": "Bundle", "id": new_id(), "type": f"{btype}-response", "meta": {"lastUpdated": instant()},
           "entry": responses}
    return Result(200, out)


def _resolve_ids(svc: FhirService, entries: list[dict]) -> dict[str, str]:
    """Map entry fullUrls (urn:uuid:..., absolute) to the local Type/id they will become."""
    mapping: dict[str, str] = {}
    for i, e in enumerate(entries):
        req, full = e["request"], e.get("fullUrl")
        method = req["method"].upper()
        segs, query = _split_url(svc, req["url"])
        res = e.get("resource") or {}
        if method == "POST" and full:
            rtype = segs[0] if segs else res.get("resourceType")
            existing = None
            if req.get("ifNoneExist"):
                existing = svc._single_match(rtype, req["ifNoneExist"], f"Entry {i} conditional create")
            e["_assigned_id"] = existing["id"] if existing else new_id()
            e["_existing"] = existing is not None
            mapping[full] = f"{rtype}/{e['_assigned_id']}"
        elif method == "PUT" and full:
            if len(segs) >= 2:
                mapping[full] = f"{segs[0]}/{segs[1]}"
            elif segs and query:
                existing = svc._single_match(segs[0], query, f"Entry {i} conditional update")
                e["_assigned_id"] = existing["id"] if existing else (res.get("id") or new_id())
                mapping[full] = f"{segs[0]}/{e['_assigned_id']}"
    return mapping


def _rewrite(svc: FhirService, resource: dict, mapping: dict[str, str], idx: int) -> None:
    for r in walk_references(resource):
        val = r["reference"]
        if val in mapping:
            r["reference"] = mapping[val]
        elif "?" in val and not val.startswith(("http://", "https://", "urn:")):
            rtype, _, q = val.partition("?")
            match = svc._single_match(rtype, q, f"Entry {idx} conditional reference '{val}'")
            if not match:
                raise FhirError(404, f"Entry {idx}: conditional reference '{val}' matched nothing", "not-found")
            r["reference"] = f"{rtype}/{match['id']}"
        elif val.startswith("urn:uuid:"):
            raise FhirError(400, f"Entry {idx}: unresolved reference '{val}'", "invalid")
        else:
            t, i, _ = parse_reference(val)
            if t and val.startswith(svc.base_url):
                r["reference"] = f"{t}/{i}"


def _entry_response(res: Result, full_url: str | None = None) -> dict:
    out: dict[str, Any] = {"response": {"status": STATUS_TEXT.get(res.status, str(res.status))}}
    if "Location" in res.headers:
        out["response"]["location"] = res.headers["Location"]
    if "ETag" in res.headers:
        out["response"]["etag"] = res.headers["ETag"]
    body = res.body
    if body and body.get("meta", {}).get("lastUpdated"):
        out["response"]["lastModified"] = body["meta"]["lastUpdated"]
    if body is not None:
        if body.get("resourceType") == "OperationOutcome" and res.status >= 400:
            out["response"]["outcome"] = body
        else:
            out["resource"] = body
            if body.get("id") and body.get("resourceType") not in ("Bundle", "OperationOutcome"):
                out["fullUrl"] = full_url or f"{res.headers.get('Location', '').split('/_history')[0]}"
    return out


def _run(svc: FhirService, entries: list[dict], origin: str, prefer: str | None, atomic: bool) -> list[dict]:
    entries = copy.deepcopy(entries)
    mapping = _resolve_ids(svc, entries) if atomic else {}
    if atomic:
        for i, e in enumerate(entries):
            if isinstance(e.get("resource"), dict):
                _rewrite(svc, e["resource"], mapping, i)
    order = sorted(range(len(entries)), key=lambda i: ORDER.get(entries[i]["request"]["method"].upper(), 9))
    responses: list[dict | None] = [None] * len(entries)
    for i in order:
        try:
            res = _execute(svc, entries[i], origin, prefer, i)
            responses[i] = _entry_response(res)
        except FhirError as e:
            if atomic:
                raise FhirError(e.status, f"Entry {i} ({entries[i]['request']['method']} "
                                          f"{entries[i]['request']['url']}): {e.message}", issues=e.issues)
            responses[i] = {"response": {"status": STATUS_TEXT.get(e.status, str(e.status)), "outcome": e.outcome()}}
    return [r for r in responses if r is not None]


def _execute(svc: FhirService, e: dict, origin: str, prefer: str | None, idx: int) -> Result:
    req = e["request"]
    method = req["method"].upper()
    segs, query = _split_url(svc, req["url"])
    res = e.get("resource")
    if not segs:
        raise FhirError(400, f"Entry {idx}: request.url has no resource type", "invalid")
    rtype = segs[0]
    if method == "POST":
        if len(segs) > 1 and segs[-1].startswith("$"):
            return svc.operation(rtype, segs[1] if len(segs) > 2 else None, segs[-1], query, res)
        if e.get("_existing"):
            existing = svc.store.read(rtype, e["_assigned_id"])
            return Result(200, existing, svc._headers_for(existing, location=True))
        if e.get("_assigned_id"):
            body = dict(res or {})
            body["id"] = e["_assigned_id"]
            return svc.create(rtype, body, prefer=prefer, origin=origin, keep_id=True)
        return svc.create(rtype, res, if_none_exist=req.get("ifNoneExist"), prefer=prefer, origin=origin)
    if method == "PUT":
        if len(segs) >= 2:
            body = dict(res or {})
            body.setdefault("id", segs[1])
            return svc.update(rtype, segs[1], body, if_match=req.get("ifMatch"), prefer=prefer, origin=origin)
        if e.get("_assigned_id"):
            body = dict(res or {})
            body["id"] = e["_assigned_id"]
            return svc.update(rtype, e["_assigned_id"], body, prefer=prefer, origin=origin)
        return svc.conditional_update(rtype, query, res, prefer=prefer, origin=origin)
    if method == "PATCH":
        body = res
        if isinstance(res, dict) and res.get("resourceType") == "Binary" and res.get("data"):
            import base64, json
            body = json.loads(base64.b64decode(res["data"]))
        return svc.patch(rtype, segs[1], body, "application/json-patch+json" if isinstance(body, list) else "",
                         if_match=req.get("ifMatch"), prefer=prefer, origin=origin)
    if method == "DELETE":
        if len(segs) >= 2:
            return svc.delete(rtype, segs[1], origin=origin)
        return svc.conditional_delete(rtype, query, origin=origin)
    if method in ("GET", "HEAD"):
        if len(segs) == 1:
            return svc.search(rtype, query)
        if len(segs) == 2:
            return svc.read(rtype, segs[1], if_none_match=req.get("ifNoneMatch"))
        if len(segs) == 4 and segs[2] == "_history":
            return svc.vread(rtype, segs[1], segs[3])
        if segs[-1] == "_history":
            return svc.history(rtype, segs[1] if len(segs) > 2 else None, query)
        if segs[-1].startswith("$"):
            return svc.operation(rtype, segs[1] if len(segs) > 2 else None, segs[-1], query, None)
    raise FhirError(400, f"Entry {idx}: unsupported request {method} {req['url']}", "not-supported")


__all__ = ["process_bundle", "operation_outcome"]
