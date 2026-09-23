"""FHIR Bulk Data Access (async $export) served by SEKMET.

Kick-off: [base]/$export, [base]/Patient/$export, [base]/Group/{id}/$export (GET or POST with Parameters),
requires `Prefer: respond-async`; answers 202 + Content-Location of the status endpoint. Status: 202 + X-Progress
while running, 200 + manifest when done, DELETE cancels. Files: NDJSON per resource type.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..auth.server import check_request
from ..fhir.common import FhirError, instant, operation_outcome
from ..fhir.search import parse_criteria
from ..fhir.service import Result

NDJSON_TYPES = ("application/fhir+ndjson", "application/ndjson", "ndjson")
SUPPORTED_PARAMS = {"_outputFormat", "_since", "_type", "_typeFilter"}


class BulkExports:
    def __init__(self, ctx):
        self.ctx = ctx
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.root = Path(ctx.settings.data_dir) / "bulk"

    @property
    def base(self) -> str:
        return self.ctx.settings.base_url.rstrip("/")

    # ---------------- kick-off ----------------

    def kickoff(self, level: str, group_id: str | None, params: list[tuple[str, str]], body, opctx: dict) -> Result:
        prefer = (opctx.get("prefer") or "").lower()
        if "respond-async" not in prefer:
            raise FhirError(400, "Bulk export requires the header 'Prefer: respond-async'", "invalid")
        if isinstance(body, dict) and body.get("resourceType") == "Parameters":
            for p in body.get("parameter", []):
                v = next((val for k, val in p.items() if k.startswith("value")), None)
                if v is not None:
                    params = [*params, (p["name"], str(v))]
        p: dict[str, list[str]] = {}
        for k, v in params:
            p.setdefault(k, []).append(v)
        unknown = [k for k in p if k not in SUPPORTED_PARAMS]
        if unknown and "handling=strict" in prefer:
            raise FhirError(400, f"Unsupported $export parameter(s): {unknown}", "not-supported")
        fmt = (p.get("_outputFormat") or ["application/fhir+ndjson"])[-1]
        if fmt not in NDJSON_TYPES:
            raise FhirError(400, f"Unsupported _outputFormat '{fmt}' (use application/fhir+ndjson)", "not-supported")
        types = [t for v in p.get("_type", []) for t in v.split(",") if t]
        since = (p.get("_since") or [None])[-1]
        type_filters = [f for v in p.get("_typeFilter", []) for f in v.split(",") if f]
        if level == "group":
            if not group_id or not self.ctx.store.exists("Group", group_id):
                raise FhirError(404, f"Group/{group_id} not found", "not-found")
        job_id = secrets.token_hex(8)
        job = {"id": job_id, "level": level, "group": group_id, "types": types, "since": since,
               "type_filters": type_filters, "status": "queued", "progress": "queued", "cancelled": False,
               "request": opctx.get("request_url") or f"{self.base}/$export",
               "transaction_time": instant(), "output": [], "error": [],
               "ignored": unknown}
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"bulk-{job_id}").start()
        return Result(202, None, {"Content-Location": f"{self.base}/bulk-status/{job_id}"})

    # ---------------- job ----------------

    def _patient_scope(self, job: dict) -> set[str] | None:
        """Patient ids in scope (None = no restriction)."""
        if job["level"] == "system":
            return None
        if job["level"] == "patient":
            return {r[0] for r in self.ctx.store.query("SELECT id FROM resources WHERE type='Patient' AND deleted=0")}
        group = self.ctx.store.read("Group", job["group"]) or {}
        ids = set()
        for m in group.get("member", []):
            ref = (m.get("entity") or {}).get("reference", "")
            if ref.startswith("Patient/") or "/Patient/" in ref:
                ids.add(ref.rsplit("/", 1)[-1])
        return ids

    def _run(self, job: dict) -> None:
        try:
            job["status"], job["progress"] = "running", "collecting"
            out_dir = self.root / job["id"]
            out_dir.mkdir(parents=True, exist_ok=True)
            patients = self._patient_scope(job)
            keys: set[tuple[str, str]] = set()
            if patients is None:
                rows = self.ctx.store.query("SELECT type, id FROM resources WHERE deleted=0")
                keys = {(r[0], r[1]) for r in rows}
            else:
                for pid in patients:
                    keys.add(("Patient", pid))
                    keys.update(self.ctx.service.search_engine.compartment_keys("Patient", pid))
            by_type: dict[str, list[dict]] = {}
            for rtype, rid in sorted(keys):
                if job["cancelled"]:
                    return
                if job["types"] and rtype not in job["types"]:
                    continue
                if rtype in ("Bundle", "Subscription", "Basic") and not job["types"]:
                    continue  # infrastructure resources only when asked for explicitly
                res = self.ctx.store.load_many([(rtype, rid)])
                if not res:
                    continue
                r = res[0]
                if job["since"] and (r.get("meta", {}).get("lastUpdated") or "") < job["since"]:
                    continue
                by_type.setdefault(rtype, []).append(r)
            for tf in job["type_filters"]:  # _typeFilter=Observation?status=final narrows that type
                ftype, fparams = parse_criteria(tf)
                if ftype in by_type:
                    by_type[ftype] = [r for r in by_type[ftype] if self.ctx.service.search_engine.matches(r, fparams)]
            job["progress"] = "writing"
            for rtype, items in sorted(by_type.items()):
                if not items:
                    continue
                path = out_dir / f"{rtype}.ndjson"
                with path.open("w") as f:
                    for r in items:
                        f.write(json.dumps(r, separators=(",", ":")) + "\n")
                job["output"].append({"type": rtype, "url": f"{self.base}/bulk-files/{job['id']}/{rtype}.ndjson",
                                      "count": len(items)})
            if job["ignored"]:
                oo = operation_outcome([{"severity": "warning", "code": "not-supported",
                                         "diagnostics": f"Ignored unsupported parameters: {job['ignored']}"}])
                (out_dir / "errors.ndjson").write_text(json.dumps(oo) + "\n")
                job["error"].append({"type": "OperationOutcome",
                                     "url": f"{self.base}/bulk-files/{job['id']}/errors.ndjson", "count": 1})
            time.sleep(min(1.0, self.ctx.settings.simulator.delay_seconds))  # let clients observe "in progress"
            job["status"], job["progress"] = "complete", "complete"
        except Exception as e:
            job["status"], job["progress"], job["failure"] = "failed", "failed", str(e)

    def manifest(self, job: dict) -> dict:
        return {"transactionTime": job["transaction_time"], "request": job["request"],
                "requiresAccessToken": "none" not in self.ctx.settings.server_auth.types,
                "output": job["output"], "error": job["error"]}

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return False
        job["cancelled"] = True
        shutil.rmtree(self.root / job_id, ignore_errors=True)
        return True


def register(svc, bulk: BulkExports) -> None:
    def op(level):
        def handler(s, rtype, rid, params, body, opctx):
            if level == "patient" and rid:
                raise FhirError(400, "Use Patient/$export (type level) or Group/{id}/$export", "not-supported")
            return bulk.kickoff(level, rid, params, body, opctx)
        return handler
    svc.register_operation("*", "$export", op("system"))
    svc.register_operation("Patient", "$export", op("patient"))
    svc.register_operation("Group", "$export", op("group"))


def build_router(get_ctx) -> APIRouter:
    router = APIRouter()

    async def _auth(request: Request):
        await run_in_threadpool(check_request, request, get_ctx())

    @router.api_route("/fhir/bulk-status/{job_id}", methods=["GET", "DELETE"])
    async def status(job_id: str, request: Request):
        ctx = get_ctx()
        try:
            await _auth(request)
        except FhirError as e:
            return JSONResponse(e.outcome(), status_code=e.status, headers=e.headers)
        bulk: BulkExports = ctx.bulk
        if request.method == "DELETE":
            ok = bulk.delete(job_id)
            return JSONResponse(operation_outcome(text="Export cancelled" if ok else "No such export"),
                                status_code=202 if ok else 404)
        job = bulk.jobs.get(job_id)
        if not job:
            return JSONResponse(operation_outcome([{"severity": "error", "code": "not-found",
                                                   "diagnostics": f"No export {job_id}"}]), status_code=404)
        if job["status"] == "failed":
            return JSONResponse(operation_outcome([{"severity": "error", "code": "exception",
                                                   "diagnostics": job.get("failure", "export failed")}]),
                                status_code=500)
        if job["status"] != "complete":
            return Response(status_code=202, headers={"X-Progress": job["progress"], "Retry-After": "1"})
        return JSONResponse(bulk.manifest(job), headers={"Expires": "Tue, 01 Jan 2030 00:00:00 GMT"})

    @router.get("/fhir/bulk-files/{job_id}/{name}")
    async def file(job_id: str, name: str, request: Request):
        ctx = get_ctx()
        try:
            await _auth(request)
        except FhirError as e:
            return JSONResponse(e.outcome(), status_code=e.status, headers=e.headers)
        path = ctx.bulk.root / job_id / name
        if "/" in name or ".." in name or not path.is_file():
            return JSONResponse(operation_outcome([{"severity": "error", "code": "not-found",
                                                   "diagnostics": "No such file"}]), status_code=404)
        return Response(path.read_bytes(), media_type="application/fhir+ndjson")

    return router
