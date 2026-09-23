"""FHIR R4 REST endpoint (/fhir/...). One catch-all route with explicit dispatch for predictable matching."""
from __future__ import annotations

import json
import logging
import traceback
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..auth.server import check_request
from .bundle import process_bundle
from .capability import capability_statement
from .common import FHIR_JSON, R4_RESOURCE_TYPES, FhirError, issue, operation_outcome
from .service import Result
from .xml import FHIR_XML, from_xml, to_xml, wants_xml

log = logging.getLogger("sekmet.fhir")
JSON_TYPES = ("application/fhir+json", "application/json", "application/json-patch+json", "text/json", "json")
XML_TYPES = ("application/fhir+xml", "application/xml", "text/xml", "xml")


def fhir_response(status: int, body: dict | None, headers: dict[str, str] | None = None, pretty: bool = False,
                  xml: bool = False) -> Response:
    h = dict(headers or {})
    if body is None or status == 304:
        return Response(content=b"", status_code=status, headers=h)
    if xml:
        try:
            return Response(content=to_xml(body), status_code=status, headers=h,
                            media_type=f"{FHIR_XML}; charset=utf-8")
        except FhirError as e:  # resource cannot be expressed through the models: say so, in JSON
            h["X-Sekmet-Format-Fallback"] = "json"
            body = {**e.outcome()} if status < 400 else body
            status = e.status if status < 400 else status
    content = json.dumps(body, indent=2 if pretty else None, separators=None if pretty else (",", ":")).encode()
    return Response(content=content, status_code=status, headers=h, media_type=f"{FHIR_JSON}; charset=utf-8")


def _negotiate(request: Request, params: list[tuple[str, str]]) -> bool:
    """Returns True when the response should be XML."""
    fmt = dict(params).get("_format", "")
    if fmt and not any(x in fmt.lower() for x in (*JSON_TYPES, *XML_TYPES)):
        raise FhirError(406, f"Unsupported _format '{fmt}' (use json or xml)", "not-supported")
    accept = request.headers.get("accept", "")
    if accept and not any(t in accept for t in (*JSON_TYPES, *XML_TYPES, "*/*", "application/*")):
        raise FhirError(406, f"Cannot produce any of: {accept}", "not-supported")
    return wants_xml(accept, fmt)


async def _body(request: Request) -> object:
    raw = await request.body()
    if not raw.strip():
        return None
    ctype = request.headers.get("content-type", "application/fhir+json").lower()
    if any(t in ctype for t in XML_TYPES):
        return from_xml(raw)
    if "x-www-form-urlencoded" in ctype:
        return parse_qsl(raw.decode(), keep_blank_values=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FhirError(400, f"Request body is not valid JSON: {e}", "structure")


def absolutize(body: dict, base: str) -> dict:
    """Rewrite relative literal references (Type/id) to absolute ones on the way out."""
    import copy
    from .common import parse_reference, walk_references
    body = copy.deepcopy(body)
    for r in walk_references(body):
        v = r["reference"]
        if not v.startswith(("http://", "https://", "urn:", "#")) and parse_reference(v)[0]:
            r["reference"] = f"{base}/{v}"
    return body


def _to_response(res: Result, pretty: bool, ctx=None, xml: bool = False) -> Response:
    body = res.body
    if body is not None and ctx is not None and ctx.settings.server_behaviour.absolute_references:
        body = absolutize(body, ctx.service.base_url)
    return fhir_response(res.status, body, res.headers, pretty, xml)


def build_router(get_ctx) -> APIRouter:
    router = APIRouter()

    @router.api_route("/fhir", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    @router.api_route("/fhir/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def fhir(request: Request, path: str = ""):
        ctx = get_ctx()
        params = list(request.query_params.multi_items())
        pretty = dict(params).get("_pretty") == "true"
        xml = False
        try:
            xml = _negotiate(request, params)
            segs = [s for s in path.split("/") if s]
            if segs == ["metadata"] and request.method in ("GET", "HEAD"):
                return fhir_response(200, capability_statement(ctx.service), pretty=pretty, xml=xml)
            await run_in_threadpool(check_request, request, ctx)
            body = await _body(request) if request.method in ("POST", "PUT", "PATCH") else None
            res = await run_in_threadpool(dispatch, ctx, request, segs, params, body)
            if res.issues:
                request.scope.setdefault("state", {})["traffic_note"] = "; ".join(
                    f"{i.get('severity')}: {i.get('diagnostics')}" for i in res.issues[:10])
            return _to_response(res, pretty, ctx, xml)
        except FhirError as e:
            if e.status >= 500:
                log.error("FHIR error: %s", e.message)
            request.scope.setdefault("state", {})["traffic_note"] = e.message[:500]
            return fhir_response(e.status, e.outcome(), e.headers, pretty, xml)
        except Exception as e:  # unexpected: still answer with an OperationOutcome
            log.exception("Unhandled error on %s %s", request.method, request.url.path)
            request.scope.setdefault("state", {})["traffic_note"] = traceback.format_exc()[-2000:]
            return fhir_response(500, operation_outcome([issue("fatal", "exception", f"Internal error: {e}")]),
                                 pretty=pretty, xml=xml)

    return router


def dispatch(ctx, request: Request, segs: list[str], params: list[tuple[str, str]], body) -> Result:
    svc = ctx.service
    m = request.method
    h = request.headers
    prefer = h.get("prefer")
    strict = "handling=strict" in (prefer or "")
    # loopback scenario traffic can ask the simulator not to react (it drives both sides itself)
    origin = "loopback" if h.get("x-sekmet-simulate", "").lower() == "off" else "external"
    opctx = {"origin": origin}
    n = len(segs)

    if n == 0:
        if m == "POST":
            if isinstance(body, dict) and body.get("type") == "message":
                return svc.operation(None, None, "$process-message", params, body, opctx)
            return process_bundle(svc, body, origin=origin, prefer=prefer)
        raise FhirError(400, "System-level search is not supported; use [base]/[type]", "not-supported")

    if segs[0] == "_history" and n == 1 and m == "GET":
        return svc.history(None, None, params)
    if segs[0].startswith("$"):
        return svc.operation(None, None, segs[0], params, body, opctx)

    rtype = segs[0]
    if rtype not in R4_RESOURCE_TYPES:
        raise FhirError(404, f"Unknown resource type '{rtype}'", "not-supported")

    if n == 1:
        if m in ("GET", "HEAD"):
            return svc.search(rtype, params, strict=strict)
        if m == "POST":
            return svc.create(rtype, body, if_none_exist=h.get("if-none-exist"), prefer=prefer, origin=origin)
        if m == "PUT":
            return svc.conditional_update(rtype, params, body, prefer=prefer, origin=origin)
        if m == "DELETE":
            return svc.conditional_delete(rtype, params, origin=origin)
        raise FhirError(405, f"{m} not allowed on [base]/{rtype}", "not-supported")

    second = segs[1]
    if n == 2:
        if second == "_search" and m == "POST":
            form = body if isinstance(body, list) else []
            return svc.search(rtype, [*params, *form], strict=strict)
        if second == "_history" and m == "GET":
            return svc.history(rtype, None, params)
        if second.startswith("$"):
            return svc.operation(rtype, None, second, params, body, opctx)
        rid = second
        if m in ("GET", "HEAD"):
            return svc.read(rtype, rid, if_none_match=h.get("if-none-match"), summary=dict(params).get("_summary"))
        if m == "PUT":
            return svc.update(rtype, rid, body, if_match=h.get("if-match"), prefer=prefer, origin=origin)
        if m == "PATCH":
            return svc.patch(rtype, rid, body, h.get("content-type", ""), if_match=h.get("if-match"), prefer=prefer,
                             origin=origin)
        if m == "DELETE":
            return svc.delete(rtype, rid, origin=origin)
        raise FhirError(405, f"{m} not allowed on [base]/{rtype}/[id]", "not-supported")

    rid, third = segs[1], segs[2]
    if third == "_history":
        if n == 3 and m == "GET":
            return svc.history(rtype, rid, params)
        if n == 4 and m == "GET":
            return svc.vread(rtype, rid, segs[3])
    if third.startswith("$") and n == 3:
        return svc.operation(rtype, rid, third, params, body, opctx)
    if n == 3 and m == "GET" and third in R4_RESOURCE_TYPES:
        return svc.compartment_search(rtype, rid, third, params)
    raise FhirError(400, f"Unsupported request {m} /{'/'.join(segs)}", "not-supported")
