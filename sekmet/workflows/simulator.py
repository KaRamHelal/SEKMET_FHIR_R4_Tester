"""Simulator: when a peer (real HIS) writes orders/appointments/prescriptions/claims to us, play the filler
(lab, RIS, scheduler, pharmacy, payer) and produce the downstream resources after a delay."""
from __future__ import annotations

import copy
import logging
import threading

from ..fhir.common import FhirError, instant, parse_reference, walk_references
from ..fhir.service import Result
from ..fhir.store import WriteEvent
from . import billing, orders, scheduling, clinical
from .registry import WF
from .target import LocalTarget, PeerTarget, Tx, WorkflowError

log = logging.getLogger("sekmet.simulator")
TRIGGER_ORIGINS = {"external", "message"}


class Simulator:
    def __init__(self, ctx):
        self.ctx = ctx
        self.history: list[dict] = []  # recent simulator actions for the UI
        self.timers: list[threading.Timer] = []
        self._claimed: set[str] = set()  # work keys already scheduled (dedupes SR + Task for one order)
        self._lock = threading.Lock()

    @property
    def cfg(self):
        return self.ctx.settings.simulator

    def _record(self, action: str, subject: str, detail: str = "", ok: bool = True):
        self.history.insert(0, {"ts": instant(), "action": action, "subject": subject, "detail": detail, "ok": ok})
        del self.history[200:]

    def on_write(self, ev: WriteEvent) -> None:
        if not self.cfg.enabled or ev.origin not in TRIGGER_ORIGINS or ev.action == "delete":
            return
        res = ev.resource
        rtype = res["resourceType"]
        if rtype not in self.cfg.auto:
            return
        handler = {
            "ServiceRequest": self._service_request,
            "Task": self._task,
            "Appointment": self._appointment,
            "MedicationRequest": self._medication_request,
            "Claim": self._claim,
        }.get(rtype)
        key = handler(res, dry=True) if handler else None
        if not key:
            return
        with self._lock:
            if key in self._claimed:
                return
            self._claimed.add(key)
        t = threading.Timer(self.cfg.delay_seconds, self._run, args=(handler, res))
        t.daemon = True
        self.timers = [x for x in self.timers if x.is_alive()] + [t]
        t.start()

    def _run(self, handler, res):
        subject = f"{res['resourceType']}/{res['id']}"
        try:
            detail = handler(res, dry=False)
            self._record(handler.__name__.strip("_"), subject, detail or "")
        except Exception as e:
            log.exception("simulator failed on %s", subject)
            self._record(handler.__name__.strip("_"), subject, str(e), ok=False)

    def wf(self) -> WF:
        return WF(self.ctx, LocalTarget(self.ctx.service, origin="simulator"))

    # ---------------- handlers (dry=True: "would I act?") ----------------

    def _service_request(self, sr: dict, dry: bool):
        if sr.get("status") != "active" or sr.get("intent") not in ("order", "original-order", "filler-order"):
            return False
        if not orders.order_code(sr):
            return False
        if dry:
            return f"ServiceRequest/{sr['id']}"
        return self._fulfil(sr)

    def _task(self, task: dict, dry: bool):
        if task.get("status") not in ("requested", "received") or not task.get("focus"):
            return False
        t, i, _ = parse_reference(task["focus"].get("reference", ""))
        if t != "ServiceRequest":
            return False
        try:
            sr = self.ctx.store.read(t, i)
        except FhirError:
            sr = None
        if not sr or not orders.order_code(sr):
            return False
        if dry:
            return f"ServiceRequest/{sr['id']}"
        return self._fulfil(sr)

    def _fulfil(self, sr: dict) -> str:
        wf = self.wf()
        sr = self.ctx.store.read("ServiceRequest", sr["id"])
        if sr.get("status") != "active":
            return f"skipped: ServiceRequest status {sr.get('status')}"
        if not orders._task_for(wf, sr):  # placer sent only a ServiceRequest: create the filler Task
            wf.t.create({"resourceType": "Task", "status": "requested", "intent": "order",
                         "basedOn": [{"reference": f"ServiceRequest/{sr['id']}"}],
                         "focus": {"reference": f"ServiceRequest/{sr['id']}"}, "for": sr.get("subject"),
                         "code": {"coding": [{"system": orders.TASK_CODE, "code": "fulfill"}]},
                         "authoredOn": instant(), "owner": {"display": "SEKMET simulated filler"}})
        orders.accept(wf, sr)
        if orders.is_imaging(sr):
            orders.imaging_study(wf, sr)
            out = orders.imaging_report(wf, sr)
            written = [out["diagnostic_report"]]
        else:
            orders.collect_specimen(wf, sr)
            out = orders.lab_result(wf, sr)
            written = [*out["observations"], out["diagnostic_report"]]
        pushed = self._push(written)
        return f"resulted {out['diagnostic_report']['resourceType']}/{out['diagnostic_report']['id']}{pushed}"

    def _push(self, resources: list[dict]) -> str:
        """Optionally POST the produced resources to a peer as a transaction (urn:uuid-linked)."""
        peer = self.cfg.results_to
        if not peer or peer == "local":
            return ""
        tx = Tx()
        urns: dict[str, dict] = {}
        for r in resources:
            body = copy.deepcopy(r)
            body.pop("meta", None)
            local_ref = f"{r['resourceType']}/{r['id']}"
            for ref_dict in walk_references(body):
                if ref_dict["reference"] in urns:
                    ref_dict["reference"] = urns[ref_dict["reference"]]["reference"]
            urns[local_ref] = tx.add(body)
        try:
            PeerTarget(self.ctx.peer_client(peer)).commit(tx)
            return f"; pushed {len(resources)} resource(s) to {peer}"
        except WorkflowError as e:
            return f"; push to {peer} FAILED: {e}"

    def _appointment(self, appt: dict, dry: bool):
        if appt.get("status") not in ("proposed", "pending"):
            return False
        if dry:
            return f"Appointment/{appt['id']}/v{appt.get('meta', {}).get('versionId')}"
        wf = self.wf()
        has_prac = any(p.get("actor", {}).get("reference", "").startswith("Practitioner/")
                       for p in appt.get("participant", []))
        if has_prac:
            out = scheduling.respond(wf, appt["id"], "accepted")
            return f"accepted -> {out['appointment']['status']}"
        a = copy.deepcopy(self.ctx.store.read("Appointment", appt["id"]))
        a["status"] = "booked"
        for p in a.get("participant", []):
            p["status"] = "accepted"
        wf.t.update(a)
        return "booked"

    def _medication_request(self, mr: dict, dry: bool):
        if mr.get("status") != "active" or mr.get("intent") not in ("order", "original-order", "instance-order"):
            return False
        if dry:
            return f"MedicationRequest/{mr['id']}"
        out = clinical.dispense(self.wf(), mr["id"])
        return f"dispensed MedicationDispense/{out['medication_dispense']['id']}"

    def _claim(self, claim: dict, dry: bool):
        if claim.get("status") != "active":
            return False
        if dry:
            return f"Claim/{claim['id']}"
        cr = billing.adjudicate_resource(claim)
        cr = self.ctx.store.create(cr, origin="simulator")
        return f"adjudicated ClaimResponse/{cr['id']}"


def claim_submit_op(svc, rtype, rid, params, body, ctx) -> Result:
    """Claim/$submit: accept a Claim (or Bundle whose first entry is a Claim) and return a ClaimResponse."""
    claim = body
    if isinstance(body, dict) and body.get("resourceType") == "Bundle":
        claim = next((e["resource"] for e in body.get("entry", []) if e.get("resource", {}).get("resourceType") == "Claim"), None)
    if not isinstance(claim, dict) or claim.get("resourceType") != "Claim":
        raise FhirError(400, "Claim/$submit requires a Claim resource", "invalid")
    # stored with origin "simulator" so the async Claim handler does not adjudicate it a second time
    stored = svc.create("Claim", claim, origin="simulator").body
    cr = billing.adjudicate_resource(stored)
    cr = svc.store.create(cr, origin="simulator")
    return Result(200, cr)

