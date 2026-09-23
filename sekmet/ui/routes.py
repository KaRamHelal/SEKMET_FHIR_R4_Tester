"""Web console (Jinja2 + optional htmx progressive enhancement)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..client.peer import PeerError
from ..fhir.common import FhirError, display_of
from ..fhir.validation import has_errors, validate
from ..scenarios.runner import ScenarioRunner, list_runs, list_scenarios, load_run
from ..subscriptions.engine import notifications, register_remote
from ..traffic.log import traffic_detail, traffic_rows
from ..workflows.registry import REGISTRY, run_workflow
from ..workflows.target import WorkflowError

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["pretty"] = lambda v: _pretty(v)
templates.env.filters["display"] = lambda r: display_of(r) or ""

_running: dict[str, threading.Thread] = {}

# workflows offered on a resource detail page, by resource type -> [(workflow key, param name)]
CONTEXT_ACTIONS = {
    "Patient": [("adt.admit", "patient"), ("adt.outpatient_visit", "patient"), ("adt.update_patient", "patient"),
                ("orders.lab_order", "patient"), ("orders.imaging_order", "patient"), ("scheduling.book", "patient"),
                ("meds.prescribe", "patient"), ("clinical.condition", "patient"), ("clinical.allergy", "patient"),
                ("clinical.vitals", "patient"), ("billing.coverage", "patient")],
    "Encounter": [("adt.transfer", "encounter"), ("adt.discharge", "encounter"), ("adt.cancel_encounter", "encounter"),
                  ("adt.start_encounter", "encounter")],
    "ServiceRequest": [("orders.accept", "service_request"), ("orders.collect_specimen", "service_request"),
                       ("orders.lab_result", "service_request"), ("orders.imaging_study", "service_request"),
                       ("orders.imaging_report", "service_request"), ("orders.cancel", "service_request")],
    "Appointment": [("scheduling.respond", "appointment"), ("scheduling.check_in", "appointment"),
                    ("scheduling.complete", "appointment"), ("scheduling.cancel", "appointment"),
                    ("scheduling.reschedule", "appointment")],
    "MedicationRequest": [("meds.dispense", "medication_request"), ("meds.administer", "medication_request"),
                          ("meds.stop", "medication_request")],
    "Claim": [("billing.adjudicate", "claim")],
}


def _pretty(v) -> str:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return v
    return json.dumps(v, indent=2, ensure_ascii=False, default=str)


def peer_summary(ctx, name: str) -> dict:
    client = ctx.peer_client(name)
    peer = ctx.settings.peer(name)
    out: dict = {"peer": name, "base_url": peer.base_url, "auth": client.auth.describe(), "mode": peer.mode}
    if peer.auth.type == "smart":
        try:
            client.auth.invalidate()
            client.auth.headers()
            out["token"] = getattr(client.auth, "last_token_response", None)
        except Exception as e:
            out["token_error"] = str(e)
    r = client.capabilities()
    out["metadata_status"] = r.status
    cs = r.resource or {}
    if cs.get("resourceType") != "CapabilityStatement":
        out["error"] = f"metadata did not return a CapabilityStatement (HTTP {r.status}): {r.outcome_text()}"
        return out
    rest = next((x for x in cs.get("rest", []) if x.get("mode") == "server"), {})
    out.update({
        "fhirVersion": cs.get("fhirVersion"),
        "software": " ".join(filter(None, [cs.get("software", {}).get("name"), cs.get("software", {}).get("version")])),
        "formats": cs.get("format"),
        "security": [c.get("code") for s in rest.get("security", {}).get("service", []) for c in s.get("coding", [])],
        "system_interactions": [i.get("code") for i in rest.get("interaction", [])],
        "operations": [o.get("name") for o in rest.get("operation", [])],
        "messaging": bool(cs.get("messaging")),
        "resources": {res["type"]: {"interactions": [i["code"] for i in res.get("interaction", [])],
                                    "search_params": len(res.get("searchParam", [])),
                                    "conditional_create": res.get("conditionalCreate"),
                                    "versioning": res.get("versioning")}
                      for res in rest.get("resource", [])},
    })
    wanted = ["Patient", "Encounter", "ServiceRequest", "Task", "Observation", "DiagnosticReport", "ImagingStudy",
              "Appointment", "Slot", "Schedule", "MedicationRequest", "MedicationDispense", "Condition",
              "AllergyIntolerance", "Coverage", "Claim", "ClaimResponse", "Subscription"]
    out["missing_for_flows"] = [w for w in wanted if w not in out["resources"]]
    return out


def build_router(get_ctx) -> APIRouter:
    router = APIRouter(prefix="/ui", include_in_schema=False)

    def page(request: Request, name: str, **kw):
        ctx = get_ctx()
        return templates.TemplateResponse(request, name, {"settings": ctx.settings, "nav": name.split(".")[0], **kw})

    @router.get("", response_class=HTMLResponse)
    def dashboard(request: Request):
        ctx = get_ctx()
        return page(request, "dashboard.html", counts=ctx.store.counts(), traffic=traffic_rows(ctx.store, 12),
                    runs=list_runs(ctx.store, 8), sim=ctx.simulator.history[:10],
                    peers=[(n, ctx.settings.peer(n)) for n in ctx.settings.peer_names()],
                    notes=notifications(ctx.store, limit=8))

    # ---------------- resources ----------------

    @router.get("/resources", response_class=HTMLResponse)
    def resources(request: Request, type: str = "Patient", q: str = ""):
        ctx = get_ctx()
        params = parse_qsl(q, keep_blank_values=True) + [("_count", "50"), ("_sort", "-_lastUpdated")]
        error, bundle = None, {}
        try:
            bundle = ctx.service.search(type, params).body
        except FhirError as e:
            error = e.message
        rows = [e["resource"] for e in bundle.get("entry", []) if e.get("search", {}).get("mode") == "match"]
        return page(request, "resources.html", rtype=type, q=q, rows=rows, total=bundle.get("total", 0), error=error,
                    counts=ctx.store.counts())

    @router.get("/resources/{rtype}/{rid}", response_class=HTMLResponse)
    def resource_detail(request: Request, rtype: str, rid: str):
        ctx = get_ctx()
        try:
            res = ctx.service.read(rtype, rid).body
        except FhirError as e:
            return page(request, "message.html", title="Not available", message=e.message)
        _, hist = ctx.store.history(rtype, rid, limit=50)
        refs = ctx.service.search_engine.compartment_keys(rtype, rid)
        actions = [(REGISTRY[k], p) for k, p in CONTEXT_ACTIONS.get(rtype, []) if k in REGISTRY]
        return page(request, "resource.html", res=res, rtype=rtype, rid=rid, history=hist, refs=refs,
                    actions=actions, peers=ctx.settings.peer_names())

    # ---------------- workflows ----------------

    @router.get("/workflows", response_class=HTMLResponse)
    def workflows(request: Request, key: str | None = None, prefill: str = ""):
        ctx = get_ctx()
        groups: dict[str, list] = {}
        for w in sorted(REGISTRY.values(), key=lambda w: w.key):
            groups.setdefault(w.group, []).append(w)
        pre = dict(parse_qsl(prefill))
        return page(request, "workflows.html", groups=groups, selected=key, prefill=pre,
                    peers=ctx.settings.peer_names())

    @router.post("/workflows/{key}", response_class=HTMLResponse)
    async def run_wf(request: Request, key: str):
        from starlette.concurrency import run_in_threadpool
        ctx = get_ctx()
        form = dict(await request.form())
        target = form.pop("_target", "local")
        args = {k: v for k, v in form.items() if v not in ("", None)}
        wf = REGISTRY.get(key)
        for p in (wf.params if wf else []):
            if p.kind == "bool":
                args[p.name] = p.name in form
        try:
            result = await run_in_threadpool(run_workflow, ctx, key, target, args)
            error = None
        except (WorkflowError, PeerError, FhirError, KeyError, ValueError) as e:
            result, error = None, getattr(e, "message", None) or str(e)
        tmpl = "partials/wf_result.html" if request.headers.get("hx-request") else "wf_result_page.html"
        return page(request, tmpl, key=key, result=result, error=error, target=target)

    # ---------------- traffic ----------------

    @router.get("/traffic", response_class=HTMLResponse)
    def traffic(request: Request, direction: str = "", status: str = "", run: str = "", q: str = "", page_no: int = 0):
        ctx = get_ctx()
        rows = traffic_rows(ctx.store, 100, page_no * 100, direction or None, run or None, status or None, q or None)
        return page(request, "traffic.html", rows=rows, direction=direction, status=status, run=run, q=q,
                    page_no=page_no)

    @router.get("/traffic/{tid}", response_class=HTMLResponse)
    def traffic_item(request: Request, tid: int):
        d = traffic_detail(get_ctx().store, tid)
        if not d:
            return page(request, "message.html", title="Not found", message=f"No traffic entry {tid}")
        return page(request, "traffic_detail.html", t=d)

    # ---------------- scenarios ----------------

    @router.get("/scenarios", response_class=HTMLResponse)
    def scenarios(request: Request):
        ctx = get_ctx()
        return page(request, "scenarios.html", scenarios=list_scenarios(), runs=list_runs(ctx.store, 30),
                    peers=ctx.settings.peer_names(), running=[k for k, t in _running.items() if t.is_alive()])

    @router.post("/scenarios/run")
    def scenario_run(scenario: str = Form(...), peer: str = Form("self")):
        ctx = get_ctx()
        ids = [s["id"] for s in list_scenarios()] if scenario == "__all__" else [scenario]
        label = f"{scenario}@{peer}"

        def work():
            runner = ScenarioRunner(ctx, peer)
            for sid in ids:
                runner.run(sid)

        t = threading.Thread(target=work, daemon=True, name=label)
        _running[label] = t
        t.start()
        return RedirectResponse("/ui/scenarios", status_code=303)

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str):
        r = load_run(get_ctx().store, run_id)
        if not r:
            return page(request, "message.html", title="Not found", message=f"No run {run_id}")
        return page(request, "run.html", r=r)

    # ---------------- peers / subscriptions / validation ----------------

    @router.get("/peers", response_class=HTMLResponse)
    def peers(request: Request):
        ctx = get_ctx()
        return page(request, "peers.html", peers=[(n, ctx.settings.peer(n)) for n in ctx.settings.peer_names()])

    @router.post("/peers/{name}/test", response_class=HTMLResponse)
    def peer_test(request: Request, name: str):
        ctx = get_ctx()
        try:
            info, error = peer_summary(ctx, name), None
        except (PeerError, KeyError, Exception) as e:
            info, error = None, str(e)
        tmpl = "partials/peer_test.html" if request.headers.get("hx-request") else "peer_test_page.html"
        return page(request, tmpl, info=info, error=error, name=name)

    @router.get("/subscriptions", response_class=HTMLResponse)
    def subscriptions(request: Request):
        ctx = get_ctx()
        subs = ctx.service.search("Subscription", [("_count", "100")]).body.get("entry", [])
        return page(request, "subscriptions.html", subs=[e["resource"] for e in subs if "resource" in e
                                                         and e["resource"]["resourceType"] == "Subscription"],
                    notes=notifications(ctx.store, limit=100), peers=list(ctx.settings.peers))

    @router.post("/subscriptions/register", response_class=HTMLResponse)
    def subscriptions_register(request: Request, peer: str = Form(...)):
        ctx = get_ctx()
        try:
            out, error = register_remote(ctx, peer), None
        except Exception as e:
            out, error = None, str(e)
        return page(request, "message.html", title=f"Register subscriptions on {peer}",
                    message=error or "Done", data=out)

    @router.get("/validate", response_class=HTMLResponse)
    def validate_form(request: Request):
        return page(request, "validate.html", body="", oo=None)

    @router.post("/validate", response_class=HTMLResponse)
    def validate_post(request: Request, body: str = Form(...), conformance: bool = Form(False)):
        ctx = get_ctx()
        try:
            oo = validate(json.loads(body), ctx.settings, conformance=conformance)
        except json.JSONDecodeError as e:
            oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "structure",
                                                                "diagnostics": f"Invalid JSON: {e}"}]}
        return page(request, "validate.html", body=body, oo=oo, ok=not has_errors(oo.get("issue", [])))

    @router.get("/simulator", response_class=HTMLResponse)
    def simulator(request: Request):
        ctx = get_ctx()
        return page(request, "simulator.html", sim=ctx.simulator.history, cfg=ctx.settings.simulator)

    @router.post("/simulator/toggle")
    def simulator_toggle():
        ctx = get_ctx()
        ctx.settings.simulator.enabled = not ctx.settings.simulator.enabled
        return RedirectResponse("/ui/simulator", status_code=303)

    return router


def mount_static(app) -> None:
    app.mount("/ui/static", StaticFiles(directory=str(HERE / "static")), name="static")
