"""FHIR interaction layer shared by the HTTP router, transaction/batch bundles and messaging."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl

import jsonpatch

from .common import (FHIR_JSON, R4_RESOURCE_TYPES, FhirError, http_date, instant, issue, new_id,
                     operation_outcome)
from .search import SearchEngine, search_links
from .store import Store
from .validation import has_errors, structural_issues, validate


@dataclass
class Result:
    status: int
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)  # validation warnings etc., logged but not returned

    @property
    def ok(self) -> bool:
        return self.status < 400


Operation = Callable[..., Result]


class FhirService:
    def __init__(self, store: Store, settings):
        self.store = store
        self.settings = settings
        self.search_engine = SearchEngine(store)
        # (resource type or "*" for system/type-agnostic, "$name") -> handler(service, rtype, rid, params, body, ctx)
        self.operations: dict[tuple[str, str], Operation] = {}
        self.register_operation("*", "$validate", _op_validate)
        self.register_operation("Patient", "$everything", _op_everything)

    @property
    def base_url(self) -> str:
        return self.settings.base_url.rstrip("/")

    def register_operation(self, rtype: str, name: str, fn: Operation) -> None:
        self.operations[(rtype, name)] = fn

    # ---------------- helpers ----------------

    def check_type(self, rtype: str) -> None:
        if rtype not in R4_RESOURCE_TYPES:
            raise FhirError(404, f"Resource type '{rtype}' is not supported", "not-supported")

    def _headers_for(self, res: dict, location: bool = False) -> dict[str, str]:
        meta = res.get("meta", {})
        h = {"ETag": f'W/"{meta.get("versionId", "1")}"'}
        if meta.get("lastUpdated"):
            h["Last-Modified"] = http_date(meta["lastUpdated"])
        if location:
            h["Location"] = f"{self.base_url}/{res['resourceType']}/{res['id']}/_history/{meta.get('versionId', '1')}"
        return h

    def _check_body(self, rtype: str, body: Any, origin: str) -> list[dict]:
        if not isinstance(body, dict):
            raise FhirError(400, "Request body must be a FHIR JSON resource", "structure")
        if body.get("resourceType") != rtype:
            raise FhirError(400, f"resourceType '{body.get('resourceType')}' does not match URL type '{rtype}'", "invalid")
        mode = self.settings.validation.inbound
        if mode == "off" or origin == "internal":
            return []
        issues = structural_issues(body)
        if mode == "strict" and has_errors(issues):
            raise FhirError(400, "Resource failed structural validation", "structure", issues=issues)
        return issues

    def _respond_write(self, res: dict, status: int, prefer: str | None, issues: list[dict]) -> Result:
        headers = self._headers_for(res, location=True)
        prefer = (prefer or "").lower()
        if "return=minimal" in prefer:
            return Result(status, None, headers, issues)
        if "return=operationoutcome" in prefer:
            return Result(status, operation_outcome(issues or None, "Operation successful"), headers, issues)
        return Result(status, res, headers, issues)

    def _single_match(self, rtype: str, query: str | list[tuple[str, str]], what: str) -> dict | None:
        params = parse_qsl(query, keep_blank_values=True) if isinstance(query, str) else query
        if not params:
            raise FhirError(400, f"{what} requires search parameters", "invalid")
        res = self.search_engine.search(rtype, [*params, ("_count", "2")], strict=True)
        if res.total > 1:
            raise FhirError(412, f"{what}: criteria matched {res.total} resources", "multiple-matches")
        return res.matches[0] if res.matches else None

    # ---------------- interactions ----------------

    def read(self, rtype: str, rid: str, if_none_match: str | None = None, summary: str | None = None) -> Result:
        self.check_type(rtype)
        res = self.store.read(rtype, rid)
        if res is None:
            raise FhirError(404, f"{rtype}/{rid} not found", "not-found")
        headers = self._headers_for(res)
        if if_none_match and if_none_match.strip() in (headers["ETag"], headers["ETag"][2:]):
            return Result(304, None, headers)
        if summary in ("true", "text", "data"):
            from .search import subset
            res = subset(res, summary, set())
        return Result(200, res, headers)

    def vread(self, rtype: str, rid: str, vid: str) -> Result:
        self.check_type(rtype)
        res = self.store.vread(rtype, rid, vid)
        if res is None:
            raise FhirError(404, f"{rtype}/{rid}/_history/{vid} not found", "not-found")
        return Result(200, res, self._headers_for(res))

    def create(self, rtype: str, body: Any, if_none_exist: str | None = None, prefer: str | None = None,
               origin: str = "external", keep_id: bool = False) -> Result:
        self.check_type(rtype)
        issues = self._check_body(rtype, body, origin)
        if if_none_exist:
            existing = self._single_match(rtype, if_none_exist, "Conditional create")
            if existing:
                return self._respond_write(existing, 200, prefer, issues)
        body = copy.deepcopy(body)
        if not keep_id:
            body.pop("id", None)
        res = self.store.create(body, origin=origin, keep_id=keep_id)
        return self._respond_write(res, 201, prefer, issues)

    def update(self, rtype: str, rid: str, body: Any, if_match: str | None = None, prefer: str | None = None,
               origin: str = "external") -> Result:
        self.check_type(rtype)
        issues = self._check_body(rtype, body, origin)
        if body.get("id") and body["id"] != rid:
            raise FhirError(400, f"Resource id '{body['id']}' does not match URL id '{rid}'", "invalid")
        if not body.get("id"):
            raise FhirError(400, "Resource id is required for update", "required")
        res, created = self.store.update(body, origin=origin, if_match=if_match)
        return self._respond_write(res, 201 if created else 200, prefer, issues)

    def conditional_update(self, rtype: str, params: list[tuple[str, str]], body: Any, prefer: str | None = None,
                           origin: str = "external") -> Result:
        self.check_type(rtype)
        existing = self._single_match(rtype, params, "Conditional update")
        body = copy.deepcopy(body)
        if existing:
            if body.get("id") and body["id"] != existing["id"]:
                raise FhirError(400, "Resource id does not match the conditionally matched resource", "invalid")
            body["id"] = existing["id"]
            return self.update(rtype, existing["id"], body, prefer=prefer, origin=origin)
        if body.get("id"):
            return self.update(rtype, body["id"], body, prefer=prefer, origin=origin)
        return self.create(rtype, body, prefer=prefer, origin=origin)

    def patch(self, rtype: str, rid: str, body: Any, content_type: str, if_match: str | None = None,
              prefer: str | None = None, origin: str = "external") -> Result:
        self.check_type(rtype)
        current = self.store.read(rtype, rid)
        if current is None:
            raise FhirError(404, f"{rtype}/{rid} not found", "not-found")
        if "json-patch" in content_type or isinstance(body, list):
            try:
                patched = jsonpatch.apply_patch(current, body)
            except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as e:
                raise FhirError(422, f"JSON Patch failed: {e}", "processing")
        elif isinstance(body, dict) and body.get("resourceType") == "Parameters":
            patched = _fhirpath_patch(current, body)
        else:
            raise FhirError(415, "PATCH requires application/json-patch+json or a FHIRPath Patch Parameters body",
                            "not-supported")
        patched["id"] = rid
        patched["resourceType"] = rtype
        return self.update(rtype, rid, patched, if_match=if_match, prefer=prefer, origin=origin)

    def delete(self, rtype: str, rid: str, origin: str = "external") -> Result:
        self.check_type(rtype)
        self.store.delete(rtype, rid, origin=origin)
        return Result(200, operation_outcome(text=f"Deleted {rtype}/{rid}"))

    def conditional_delete(self, rtype: str, params: list[tuple[str, str]], origin: str = "external") -> Result:
        self.check_type(rtype)
        existing = self._single_match(rtype, params, "Conditional delete")
        if not existing:
            return Result(204, None)
        return self.delete(rtype, existing["id"], origin=origin)

    def search(self, rtype: str, params: list[tuple[str, str]], strict: bool = False,
               extra_where: tuple[str, list] | None = None) -> Result:
        self.check_type(rtype)
        r = self.search_engine.search(rtype, params, strict=strict, extra_where=extra_where)
        entries = [{"fullUrl": f"{self.base_url}/{m['resourceType']}/{m['id']}", "resource": m,
                    "search": {"mode": "match"}} for m in r.matches]
        entries += [{"fullUrl": f"{self.base_url}/{m['resourceType']}/{m['id']}", "resource": m,
                     "search": {"mode": "include"}} for m in r.included]
        if r.issues:
            entries.append({"resource": operation_outcome(r.issues), "search": {"mode": "outcome"}})
        bundle = {"resourceType": "Bundle", "id": new_id(), "meta": {"lastUpdated": instant()},
                  "type": "searchset", "total": r.total, "link": search_links(self.base_url, rtype, r)}
        if entries:
            bundle["entry"] = entries
        return Result(200, bundle, issues=r.issues)

    def compartment_search(self, ctype: str, cid: str, rtype: str, params: list[tuple[str, str]]) -> Result:
        """[base]/Patient/123/Observation: resources of rtype referencing ctype/cid."""
        self.check_type(ctype)
        if not self.store.exists(ctype, cid):
            raise FhirError(404, f"{ctype}/{cid} not found", "not-found")
        where = ("EXISTS (SELECT 1 FROM idx ci WHERE ci.type=r.type AND ci.id=r.id AND ci.rtype=? AND ci.rid=?)",
                 [ctype, cid])
        return self.search(rtype, params, extra_where=where)

    def history(self, rtype: str | None, rid: str | None, params: list[tuple[str, str]]) -> Result:
        if rtype:
            self.check_type(rtype)
        p = dict(params)
        count = min(int(p.get("_count", 50)), 1000)
        offset = int(p.get("_offset", 0))
        total, rows = self.store.history(rtype, rid, p.get("_since"), count, offset)
        import json as _json
        entries = []
        for row in rows:
            url = f"{self.base_url}/{row['type']}/{row['id']}"
            e: dict[str, Any] = {"fullUrl": url}
            method = row["method"] or "PUT"
            if row["json"]:
                e["resource"] = _json.loads(row["json"])
            e["request"] = {"method": method, "url": f"{row['type']}" if method == "POST" else f"{row['type']}/{row['id']}"}
            status = {"POST": "201 Created", "DELETE": "204 No Content"}.get(method, "200 OK")
            e["response"] = {"status": status, "etag": f'W/"{row["version"]}"', "lastModified": row["last_updated"]}
            entries.append(e)
        path = "/".join(x for x in (rtype, rid) if x)
        bundle = {"resourceType": "Bundle", "id": new_id(), "type": "history", "total": total,
                  "link": [{"relation": "self", "url": f"{self.base_url}/{path + '/' if path else ''}_history"}]}
        if offset + count < total:
            bundle["link"].append({"relation": "next", "url": f"{self.base_url}/{path + '/' if path else ''}_history?_count={count}&_offset={offset + count}"})
        if entries:
            bundle["entry"] = entries
        return Result(200, bundle)

    def operation(self, rtype: str | None, rid: str | None, name: str, params: list[tuple[str, str]],
                  body: Any, ctx: dict | None = None) -> Result:
        if rtype:
            self.check_type(rtype)
        fn = self.operations.get((rtype or "*", name)) or self.operations.get(("*", name))
        if fn is None:
            raise FhirError(404, f"Operation {name} is not supported{' on ' + rtype if rtype else ''}", "not-supported")
        return fn(self, rtype, rid, params, body, ctx or {})


# ---------------- built-in operations ----------------

def _op_validate(svc: FhirService, rtype, rid, params, body, ctx) -> Result:
    resource = body
    profile = dict(params).get("profile")
    if isinstance(body, dict) and body.get("resourceType") == "Parameters":
        for p in body.get("parameter", []):
            if p.get("name") == "resource":
                resource = p.get("resource")
            elif p.get("name") == "profile":
                profile = p.get("valueUri") or p.get("valueCanonical")
    if resource is None and rid:
        resource = svc.store.read(rtype, rid)
    if not isinstance(resource, dict):
        raise FhirError(400, "$validate requires a resource", "required")
    oo = validate(resource, svc.settings, conformance=True, profile=profile)
    return Result(200, oo)


def _op_everything(svc: FhirService, rtype, rid, params, body, ctx) -> Result:
    if not rid:
        raise FhirError(400, "Patient/$everything requires a patient id (instance level)", "required")
    patient = svc.store.read("Patient", rid)
    if patient is None:
        raise FhirError(404, f"Patient/{rid} not found", "not-found")
    keys = svc.search_engine.compartment_keys("Patient", rid)
    resources = [patient, *svc.store.load_many(keys)]
    # pull in directly referenced non-patient resources (practitioners, orgs, locations, meds)
    seen = {(r["resourceType"], r["id"]) for r in resources}
    extra: list[tuple[str, str]] = []
    for r in resources:
        for row in svc.store.query("SELECT DISTINCT rtype, rid FROM idx WHERE type=? AND id=? AND rid IS NOT NULL",
                                   (r["resourceType"], r["id"])):
            k = (row[0], row[1])
            if k not in seen:
                seen.add(k); extra.append(k)
    resources += svc.store.load_many(extra)
    entries = [{"fullUrl": f"{svc.base_url}/{r['resourceType']}/{r['id']}", "resource": r,
                "search": {"mode": "match"}} for r in resources]
    return Result(200, {"resourceType": "Bundle", "id": new_id(), "type": "searchset", "total": len(entries),
                        "entry": entries})


def _fhirpath_patch(current: dict, params: dict) -> dict:
    """Minimal FHIRPath Patch: add / replace / delete on simple element paths (Type.a.b)."""
    out = copy.deepcopy(current)
    for op in params.get("parameter", []):
        if op.get("name") != "operation":
            continue
        parts = {p["name"]: p for p in op.get("part", [])}
        typ = parts.get("type", {}).get("valueCode")
        path = parts.get("path", {}).get("valueString", "")
        segs = path.split(".")[1:]
        value = None
        if "value" in parts:
            value = next((v for k, v in parts["value"].items() if k.startswith("value")), None)
            if value is None and "part" in parts["value"]:
                value = {p["name"]: next(v for k, v in p.items() if k.startswith("value")) for p in parts["value"]["part"]}
        if typ == "add":
            segs.append(parts.get("name", {}).get("valueString", ""))
        target = out
        for s in segs[:-1]:
            target = target.setdefault(s, {})
            if isinstance(target, list):
                target = target[0]
        last = segs[-1] if segs else None
        if not last:
            raise FhirError(400, f"Unsupported FHIRPath Patch path '{path}'", "not-supported")
        if typ in ("replace", "add"):
            if typ == "add" and isinstance(target.get(last), list):
                target[last].append(value)
            else:
                target[last] = value
        elif typ == "delete":
            target.pop(last, None)
        else:
            raise FhirError(400, f"Unsupported FHIRPath Patch operation '{typ}'", "not-supported")
    return out


__all__ = ["FhirService", "Result", "FHIR_JSON", "issue"]
