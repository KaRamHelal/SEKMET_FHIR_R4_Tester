"""Orders & results: lab and radiology ServiceRequest -> Task -> Specimen/ImagingStudy -> Observation/DiagnosticReport."""
from __future__ import annotations

import base64
import copy
import random
import uuid

from ..fhir.common import codeable, instant, ref
from . import catalog as C
from .registry import Param, WF, workflow
from .target import Tx, WorkflowError

LAB_CATEGORY = codeable(C.SNOMED, "108252007", "Laboratory procedure")
IMAGING_CATEGORY = codeable(C.SNOMED, "363679005", "Imaging")
TASK_CODE = "http://hl7.org/fhir/CodeSystem/task-code"


def _order(wf: WF, patient, encounter, code: str, display: str, category: dict, priority: str, performer: dict,
           kind: str) -> dict:
    p = wf.get("Patient", patient)
    e = wf.get("Encounter", encounter) if encounter else None
    requisition = wf.number("REQ")
    placer = wf.number("PO")
    tx = Tx()
    sr = {
        "resourceType": "ServiceRequest",
        "identifier": [wf.ident(wf.ids.placer_order, placer, "PLAC")],
        "requisition": wf.ident(wf.ids.placer_order, requisition),
        "status": "active",
        "intent": "order",
        "category": [category],
        "priority": priority,
        "code": codeable(C.LOINC, code, display),
        "subject": ref(p),
        "authoredOn": instant(),
        "requester": ref(wf.practitioner("attending")),
        "performer": [ref(performer)],
    }
    if e:
        sr["encounter"] = ref(e)
    sr_ref = tx.add(sr)
    task = {
        "resourceType": "Task",
        "identifier": [wf.ident(wf.ids.placer_order, f"{placer}-T")],
        "groupIdentifier": wf.ident(wf.ids.placer_order, requisition),
        "basedOn": [sr_ref],
        "status": "requested",
        "intent": "order",
        "priority": priority,
        "code": codeable(TASK_CODE, "fulfill", "Fulfill the focal request"),
        "focus": sr_ref,
        "for": ref(p),
        "authoredOn": instant(),
        "lastModified": instant(),
        "requester": ref(wf.practitioner("attending")),
        "owner": ref(performer),
    }
    if e:
        task["encounter"] = ref(e)
    tx.add(task)
    sr, task = wf.t.commit(tx)
    wf.emit(kind, [sr], [p, task] + ([e] if e else []))
    return {"service_request": sr, "task": task, "patient": p, "placer_order_number": placer}


@workflow("orders.lab_order", "Place lab order (ServiceRequest + Task)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("test", "Test / panel", "choice", "58410-2", list(C.LAB_TESTS)),
    Param("priority", "Priority", "choice", "routine", ["routine", "urgent", "asap", "stat"]),
])
def lab_order(wf: WF, patient, encounter=None, test="58410-2", priority="routine"):
    display, _ = C.LAB_TESTS[test]
    return _order(wf, patient, encounter, test, display, LAB_CATEGORY, priority, wf.org("lab"), "order")


@workflow("orders.imaging_order", "Place imaging order (ServiceRequest + Task)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("procedure", "Procedure", "choice", "36643-5", list(C.IMAGING)),
    Param("priority", "Priority", "choice", "routine", ["routine", "urgent", "asap", "stat"]),
])
def imaging_order(wf: WF, patient, encounter=None, procedure="36643-5", priority="routine"):
    display = C.IMAGING[procedure][0]
    return _order(wf, patient, encounter, procedure, display, IMAGING_CATEGORY, priority, wf.org("radiology"),
                  "imaging-order")


def _task_for(wf: WF, sr: dict) -> dict | None:
    tasks = wf.t.search("Task", [("based-on", f"ServiceRequest/{sr['id']}")])
    if not tasks:
        tasks = wf.t.search("Task", [("focus", f"ServiceRequest/{sr['id']}")])
    return tasks[0] if tasks else None


def _set_task(wf: WF, task: dict | None, status: str, business: str | None = None, output: list | None = None):
    if not task:
        return None
    t = copy.deepcopy(task)
    t["status"] = status
    t["lastModified"] = instant()
    if business:
        t["businessStatus"] = {"text": business}
    if status == "in-progress":
        t.setdefault("executionPeriod", {})["start"] = instant()
    if status in ("completed", "cancelled", "failed"):
        t.setdefault("executionPeriod", {}).setdefault("start", instant())
        t["executionPeriod"]["end"] = instant()
    if output:
        t["output"] = output
    return t


@workflow("orders.accept", "Filler accepts order (Task accepted)", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
])
def accept(wf: WF, service_request):
    sr = wf.get("ServiceRequest", service_request)
    task = _set_task(wf, _task_for(wf, sr), "accepted", "Order received by filler")
    if task:
        filler = wf.number("FO")
        task["identifier"] = [*task.get("identifier", []), wf.ident(wf.ids.filler_order, filler, "FILL")]
        task = wf.t.update(task)
    return {"service_request": sr, "task": task}


@workflow("orders.collect_specimen", "Collect specimen", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
])
def collect_specimen(wf: WF, service_request):
    sr = wf.get("ServiceRequest", service_request)
    spec = {
        "resourceType": "Specimen",
        "identifier": [wf.ident(wf.ids.accession, wf.number("S"))],
        "accessionIdentifier": wf.ident(wf.ids.accession, wf.number("ACC")),
        "status": "available",
        "type": codeable("http://terminology.hl7.org/CodeSystem/v2-0487", "BLD", "Whole blood"),
        "subject": sr["subject"],
        "receivedTime": instant(),
        "request": [ref(sr)],
        "collection": {"collector": ref(wf.practitioner("nurse")), "collectedDateTime": instant(),
                       "bodySite": codeable(C.SNOMED, "368208006", "Left upper arm structure")},
    }
    spec = wf.t.create(spec)
    task = _set_task(wf, _task_for(wf, sr), "in-progress", "Specimen collected")
    if task:
        task = wf.t.update(task)
    return {"specimen": spec, "task": task, "service_request": sr}


def _value(low: float, high: float, decimals: int, abnormal_rate: float = 0.2) -> tuple[float, str]:
    span = high - low
    if random.random() < abnormal_rate:
        v = random.choice([low - span * random.uniform(0.05, 0.3), high + span * random.uniform(0.05, 0.3)])
    else:
        v = random.uniform(low, high)
    v = round(max(v, 0), decimals)
    flag = "L" if v < low else "H" if v > high else "N"
    return (int(v) if decimals == 0 else v), flag


FLAGS = {"L": "Low", "H": "High", "N": "Normal"}


@workflow("orders.lab_result", "Report lab results (Observations + DiagnosticReport)", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
    Param("status", "Report status", "choice", "final", ["preliminary", "final", "amended", "corrected"]),
    Param("abnormal_rate", "Probability of abnormal value", "float", 0.2),
])
def lab_result(wf: WF, service_request, status="final", abnormal_rate=0.2):
    sr = wf.get("ServiceRequest", service_request)
    code = next((c["code"] for c in sr.get("code", {}).get("coding", []) if c.get("system") == C.LOINC), None)
    if code not in C.LAB_TESTS:
        raise WorkflowError(f"ServiceRequest code {code} is not a known lab test ({', '.join(C.LAB_TESTS)})")
    display, members = C.LAB_TESTS[code]
    specimens = wf.t.search("Specimen", [("subject", sr["subject"]["reference"])])
    specimen = next((s for s in specimens if any(r.get("reference") == f"ServiceRequest/{sr['id']}"
                                                 for r in s.get("request", []))), None)
    now = instant()
    tx = Tx()
    obs_refs = []
    for loinc, disp, unit, low, high, dec in members:
        val, flag = _value(low, high, dec, abnormal_rate)
        obs = {
            "resourceType": "Observation",
            "identifier": [wf.ident(wf.ids.filler_order, wf.number("OBS"))],
            "basedOn": [ref(sr)],
            "status": status,
            "category": [codeable(C.OBS_CAT, "laboratory", "Laboratory")],
            "code": codeable(C.LOINC, loinc, disp),
            "subject": sr["subject"],
            "effectiveDateTime": now,
            "issued": now,
            "performer": [ref(wf.org("lab"))],
            "valueQuantity": {"value": val, "unit": unit, "system": C.UCUM, "code": unit},
            "interpretation": [codeable(C.INTERP, flag, FLAGS[flag])],
            "referenceRange": [{"low": {"value": low, "unit": unit, "system": C.UCUM, "code": unit},
                                "high": {"value": high, "unit": unit, "system": C.UCUM, "code": unit}}],
        }
        if sr.get("encounter"):
            obs["encounter"] = sr["encounter"]
        if specimen:
            obs["specimen"] = ref(specimen)
        obs_refs.append(tx.add(obs))
    dr = {
        "resourceType": "DiagnosticReport",
        "identifier": [wf.ident(wf.ids.filler_order, wf.number("DR"))],
        "basedOn": [ref(sr)],
        "status": status,
        "category": [codeable(C.V2_0074, "LAB", "Laboratory")],
        "code": codeable(C.LOINC, code, display),
        "subject": sr["subject"],
        "effectiveDateTime": now,
        "issued": now,
        "performer": [ref(wf.org("lab"))],
        "resultsInterpreter": [ref(wf.practitioner("lab-tech"))],
        "result": obs_refs,
    }
    if sr.get("encounter"):
        dr["encounter"] = sr["encounter"]
    if specimen:
        dr["specimen"] = [ref(specimen)]
    dr_ref = tx.add(dr)
    task = _task_for(wf, sr)
    final = status in ("final", "amended", "corrected")
    if task:
        t = _set_task(wf, task, "completed" if final else "in-progress", "Results available",
                      [{"type": {"text": "DiagnosticReport"}, "valueReference": dr_ref}])
        tx.put(t)
    if final:
        sr2 = copy.deepcopy(sr)
        sr2["status"] = "completed"
        tx.put(sr2)
    written = wf.t.commit(tx)
    obs_list = written[: len(members)]
    report = written[len(members)]
    wf.emit("result", [report], [*obs_list, sr] + ([specimen] if specimen else []))
    return {"diagnostic_report": report, "observations": obs_list, "service_request": sr}


@workflow("orders.imaging_study", "Perform imaging (ImagingStudy)", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
])
def imaging_study(wf: WF, service_request):
    sr = wf.get("ServiceRequest", service_request)
    code = next((c["code"] for c in sr.get("code", {}).get("coding", []) if c.get("code") in C.IMAGING), None)
    if not code:
        raise WorkflowError("ServiceRequest code is not a known imaging procedure")
    display, modality, site, site_display = C.IMAGING[code]
    uid = lambda: f"2.25.{uuid.uuid4().int}"  # noqa: E731 - DICOM UUID-derived UID
    now = instant()
    study = {
        "resourceType": "ImagingStudy",
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{uid()}"},
                       wf.ident(wf.ids.accession, wf.number("ACC"), "ACSN")],
        "status": "available",
        "modality": [{"system": C.DICOM, "code": modality}],
        "subject": sr["subject"],
        "started": now,
        "basedOn": [ref(sr)],
        "referrer": sr.get("requester"),
        "numberOfSeries": 1,
        "numberOfInstances": 2,
        "procedureCode": [codeable(C.LOINC, code, display)],
        "location": ref(wf.location("radiology-room")),
        "description": display,
        "series": [{
            "uid": uid(), "number": 1, "modality": {"system": C.DICOM, "code": modality},
            "description": display, "numberOfInstances": 2,
            "bodySite": {"system": C.SNOMED, "code": site, "display": site_display},
            "started": now,
            "instance": [{"uid": uid(), "sopClass": {"system": "urn:ietf:rfc:3986",
                                                    "code": "urn:oid:1.2.840.10008.5.1.4.1.1.1.1"}, "number": i}
                         for i in (1, 2)],
        }],
    }
    if sr.get("encounter"):
        study["encounter"] = sr["encounter"]
    study = wf.t.create(study)
    task = _set_task(wf, _task_for(wf, sr), "in-progress", "Images acquired")
    if task:
        task = wf.t.update(task)
    return {"imaging_study": study, "task": task, "service_request": sr}


@workflow("orders.imaging_report", "Report imaging (DiagnosticReport RAD)", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
    Param("conclusion", "Conclusion", default="No acute cardiopulmonary abnormality."),
])
def imaging_report(wf: WF, service_request, conclusion="No acute cardiopulmonary abnormality."):
    sr = wf.get("ServiceRequest", service_request)
    code = next((c for c in sr.get("code", {}).get("coding", [])), {})
    studies = wf.t.search("ImagingStudy", [("basedon", f"ServiceRequest/{sr['id']}")])
    now = instant()
    text = f"EXAM: {code.get('display')}\nFINDINGS: Unremarkable.\nIMPRESSION: {conclusion}\n"
    tx = Tx()
    dr = {
        "resourceType": "DiagnosticReport",
        "identifier": [wf.ident(wf.ids.filler_order, wf.number("RAD"))],
        "basedOn": [ref(sr)],
        "status": "final",
        "category": [codeable(C.V2_0074, "RAD", "Radiology")],
        "code": codeable(C.LOINC, code.get("code", "18748-4"), code.get("display", "Diagnostic imaging study")),
        "subject": sr["subject"],
        "effectiveDateTime": now,
        "issued": now,
        "performer": [ref(wf.org("radiology"))],
        "resultsInterpreter": [ref(wf.practitioner("radiologist"))],
        "conclusion": conclusion,
        "presentedForm": [{"contentType": "text/plain", "language": "en",
                           "data": base64.b64encode(text.encode()).decode(), "title": "Radiology report",
                           "creation": now}],
    }
    if sr.get("encounter"):
        dr["encounter"] = sr["encounter"]
    if studies:
        dr["imagingStudy"] = [ref(s) for s in studies]
    dr_ref = tx.add(dr)
    task = _task_for(wf, sr)
    if task:
        tx.put(_set_task(wf, task, "completed", "Report final",
                         [{"type": {"text": "DiagnosticReport"}, "valueReference": dr_ref}]))
    sr2 = copy.deepcopy(sr)
    sr2["status"] = "completed"
    tx.put(sr2)
    written = wf.t.commit(tx)
    wf.emit("result", [written[0]], [sr, *studies])
    return {"diagnostic_report": written[0], "service_request": sr}


@workflow("orders.cancel", "Cancel order (ServiceRequest revoked, Task cancelled)", [
    Param("service_request", "ServiceRequest", "ref", required=True, ref_type="ServiceRequest"),
    Param("reason", "Reason", default="Ordered in error"),
])
def cancel(wf: WF, service_request, reason="Ordered in error"):
    sr = copy.deepcopy(wf.get("ServiceRequest", service_request))
    if sr.get("status") in ("completed", "revoked", "entered-in-error"):
        raise WorkflowError(f"ServiceRequest is already {sr.get('status')}")
    sr["status"] = "revoked"
    sr.setdefault("note", []).append({"text": f"Cancelled: {reason}", "time": instant()})
    sr = wf.t.update(sr)
    task = _set_task(wf, _task_for(wf, sr), "cancelled", reason)
    if task:
        task["statusReason"] = {"text": reason}
        task = wf.t.update(task)
    wf.emit("order-cancel", [sr], [task] if task else [])
    return {"service_request": sr, "task": task}


@workflow("orders.full_lab_cycle", "Lab order -> accept -> specimen -> result", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("test", "Test / panel", "choice", "51990-0", list(C.LAB_TESTS)),
])
def full_lab_cycle(wf: WF, patient, encounter=None, test="51990-0"):
    o = lab_order(wf, patient, encounter, test)
    sr = o["service_request"]
    accept(wf, sr)
    collect_specimen(wf, sr)
    r = lab_result(wf, sr)
    return {**o, **r}


def order_code(sr: dict) -> str | None:
    for c in sr.get("code", {}).get("coding", []):
        if c.get("code") in C.LAB_TESTS or c.get("code") in C.IMAGING:
            return c["code"]
    return None


def is_imaging(sr: dict) -> bool:
    cats = [c.get("code") for cc in sr.get("category", []) for c in cc.get("coding", [])]
    return "363679005" in cats or order_code(sr) in C.IMAGING

