"""ADT / registration: patient register/update/merge, admit/transfer/discharge, outpatient visits."""
from __future__ import annotations

import copy

from ..fhir.common import codeable, first_identifier, instant, ref
from ..seed import generator
from . import catalog as C
from .registry import LOCATIONS, Param, WF, workflow
from .target import WorkflowError

BEDS = [k for k, v in LOCATIONS.items() if v[2] == "bd"]


@workflow("adt.facility", "Ensure facility master data",
          description="Organizations, wards/rooms/beds, practitioners and roles (idempotent via identifiers).")
def facility(wf: WF):
    return wf.facility()


@workflow("adt.register_patient", "Register patient (A04)", [
    Param("mrn", "MRN (blank = generated)"),
    Param("family", "Family name"),
    Param("given", "Given name"),
    Param("gender", "Gender", "choice", None, ["", "male", "female", "other", "unknown"]),
    Param("birth_date", "Birth date (YYYY-MM-DD)"),
    Param("seed", "Random seed", "int"),
])
def register_patient(wf: WF, mrn=None, family=None, given=None, gender=None, birth_date=None, seed=None):
    generator.seed(seed)
    p = generator.patient_resource(wf.ids.mrn, mrn, gender or None)
    if family or given:
        n = p["name"][0]
        n["family"] = family or n["family"]
        n["given"] = [given] if given else n["given"]
        n["text"] = f"{n['given'][0]} {n['family']}"
    if birth_date:
        p["birthDate"] = birth_date
    p["managingOrganization"] = ref(wf.org("hospital"))
    p["generalPractitioner"] = [ref(wf.practitioner("attending"))]
    mrn_value = p["identifier"][0]["value"]
    patient = wf.t.create(p, if_none_exist=f"identifier={wf.ids.mrn}|{mrn_value}")
    wf.emit("patient-register", [patient])
    return {"patient": patient, "mrn": mrn_value}


@workflow("adt.update_patient", "Update patient demographics (A08)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("phone", "New phone (blank = random)"),
    Param("address_line", "New address line (blank = random)"),
])
def update_patient(wf: WF, patient, phone=None, address_line=None):
    p = copy.deepcopy(wf.get("Patient", patient))
    tel = [t for t in p.get("telecom", []) if t.get("system") != "phone"]
    p["telecom"] = [{"system": "phone", "value": phone or generator.phone(), "use": "mobile"}, *tel]
    addr = (p.get("address") or [{}])[0]
    addr["line"] = [address_line or generator.street()]
    p["address"] = [addr, *p.get("address", [])[1:]]
    updated = wf.t.update(p)
    wf.emit("patient-update", [updated])
    return {"patient": updated}


@workflow("adt.merge_patients", "Merge patients (A40, Patient.link)", [
    Param("survivor", "Surviving patient", "ref", required=True, ref_type="Patient"),
    Param("merged", "Patient to retire", "ref", required=True, ref_type="Patient"),
])
def merge_patients(wf: WF, survivor, merged):
    """R4 has no standard $merge: retire the duplicate (active=false, link replaced-by) and link the survivor."""
    s = copy.deepcopy(wf.get("Patient", survivor))
    m = copy.deepcopy(wf.get("Patient", merged))
    if s["id"] == m["id"]:
        raise WorkflowError("Cannot merge a patient into itself")
    m["active"] = False
    m["link"] = [{"other": ref(s), "type": "replaced-by"}]
    s["link"] = [*[l for l in s.get("link", []) if l.get("other", {}).get("reference") != f"Patient/{m['id']}"],
                 {"other": ref(m), "type": "replaces"}]
    old_mrn = first_identifier(m, wf.ids.mrn)
    if old_mrn and old_mrn not in s.get("identifier", []):
        s["identifier"] = [*s.get("identifier", []), {**old_mrn, "use": "old"}]
    m = wf.t.update(m)
    s = wf.t.update(s)
    wf.emit("patient-merge", [s], [m])
    return {"survivor": s, "merged": m}


def _encounter(wf: WF, patient: dict, cls: str, cls_display: str, status: str, location: dict | None,
               practitioner: dict, reason: str | None) -> dict:
    enc = {
        "resourceType": "Encounter",
        "identifier": [wf.ident(wf.ids.visit, wf.number("V"), "VN")],
        "status": status,
        "statusHistory": [{"status": status, "period": {"start": instant()}}],
        "class": {"system": C.V3_ACT, "code": cls, "display": cls_display},
        "type": [codeable(C.SNOMED, "32485007" if cls == "IMP" else "185349003",
                          "Hospital admission" if cls == "IMP" else "Encounter for check up")],
        "priority": codeable("http://terminology.hl7.org/CodeSystem/v3-ActPriority", "R", "routine"),
        "subject": ref(patient),
        "participant": [{
            "type": [codeable(C.PARTICIPATION, "ATND", "attender")],
            "period": {"start": instant()},
            "individual": ref(practitioner),
        }],
        "period": {"start": instant()},
        "serviceProvider": ref(wf.org("hospital")),
    }
    if reason:
        code = next((k for k, v in C.CONDITIONS.items() if k == reason or v.lower() == reason.lower()), None)
        enc["reasonCode"] = [codeable(C.SNOMED, code, C.CONDITIONS[code]) if code else {"text": reason}]
    if location:
        enc["location"] = [{"location": ref(location), "status": "active" if status == "in-progress" else "planned",
                            "physicalType": location.get("physicalType"), "period": {"start": instant()}}]
    if cls == "IMP":
        enc["hospitalization"] = {
            "preAdmissionIdentifier": wf.ident(wf.ids.visit, wf.number("PA")),
            "admitSource": codeable(C.ADMIT_SOURCE, "emd", "From accident/emergency department"),
        }
    return enc


@workflow("adt.admit", "Admit inpatient (A01)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("bed", "Bed", "choice", "bed-401-1", BEDS),
    Param("reason", "Reason (SNOMED code or text)", default="233604007"),
    Param("planned", "Pre-admit only (status=planned)", "bool", False),
])
def admit(wf: WF, patient, bed="bed-401-1", reason=None, planned=False):
    p = wf.get("Patient", patient)
    enc = _encounter(wf, p, "IMP", "inpatient encounter", "planned" if planned else "in-progress",
                     wf.location(bed), wf.practitioner("attending"), reason)
    enc = wf.t.create(enc)
    wf.emit("admit", [enc], [p])
    return {"encounter": enc, "patient": p}


@workflow("adt.start_encounter", "Start planned encounter (arrive)", [
    Param("encounter", "Encounter", "ref", required=True, ref_type="Encounter"),
])
def start_encounter(wf: WF, encounter):
    e = copy.deepcopy(wf.get("Encounter", encounter))
    e["status"] = "in-progress"
    e.setdefault("statusHistory", []).append({"status": "in-progress", "period": {"start": instant()}})
    for loc in e.get("location", []):
        loc["status"] = "active"
    e = wf.t.update(e)
    wf.emit("admit", [e])
    return {"encounter": e}


@workflow("adt.transfer", "Transfer patient (A02)", [
    Param("encounter", "Encounter", "ref", required=True, ref_type="Encounter"),
    Param("bed", "New bed", "choice", "bed-502-1", BEDS),
])
def transfer(wf: WF, encounter, bed="bed-502-1"):
    e = copy.deepcopy(wf.get("Encounter", encounter))
    if e.get("status") not in ("in-progress", "arrived", "onleave"):
        raise WorkflowError(f"Cannot transfer an encounter with status {e.get('status')}")
    now = instant()
    for loc in e.get("location", []):
        if loc.get("status") == "active":
            loc["status"] = "completed"
            loc.setdefault("period", {})["end"] = now
    new_loc = wf.location(bed)
    e.setdefault("location", []).append({"location": ref(new_loc), "status": "active",
                                         "physicalType": new_loc.get("physicalType"), "period": {"start": now}})
    e = wf.t.update(e)
    wf.emit("transfer", [e])
    return {"encounter": e}


@workflow("adt.discharge", "Discharge patient (A03)", [
    Param("encounter", "Encounter", "ref", required=True, ref_type="Encounter"),
    Param("disposition", "Discharge disposition", "choice", "home",
          ["home", "alt-home", "other-hcf", "hosp", "long", "aadvice", "exp", "psy", "rehab", "snf", "oth"]),
])
def discharge(wf: WF, encounter, disposition="home"):
    e = copy.deepcopy(wf.get("Encounter", encounter))
    if e.get("status") in ("finished", "cancelled", "entered-in-error"):
        raise WorkflowError(f"Encounter already {e.get('status')}")
    now = instant()
    for sh in e.get("statusHistory", []):
        sh.setdefault("period", {}).setdefault("end", now)
    e["status"] = "finished"
    e.setdefault("statusHistory", []).append({"status": "finished", "period": {"start": now}})
    e.setdefault("period", {})["end"] = now
    for loc in e.get("location", []):
        if loc.get("status") in ("active", "planned"):
            loc["status"] = "completed"
            loc.setdefault("period", {})["end"] = now
    for part in e.get("participant", []):
        part.setdefault("period", {})["end"] = now
    if e.get("class", {}).get("code") == "IMP":
        e.setdefault("hospitalization", {})["dischargeDisposition"] = codeable(C.DISCHARGE, disposition)
    e = wf.t.update(e)
    wf.emit("discharge", [e])
    return {"encounter": e}


@workflow("adt.cancel_encounter", "Cancel admit/visit (A11)", [
    Param("encounter", "Encounter", "ref", required=True, ref_type="Encounter"),
])
def cancel_encounter(wf: WF, encounter):
    e = copy.deepcopy(wf.get("Encounter", encounter))
    e["status"] = "cancelled"
    e.setdefault("statusHistory", []).append({"status": "cancelled", "period": {"start": instant()}})
    e = wf.t.update(e)
    wf.emit("cancel-admit", [e])
    return {"encounter": e}


@workflow("adt.outpatient_visit", "Start outpatient visit (A04)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("reason", "Reason (SNOMED code or text)", default="Routine check up"),
])
def outpatient_visit(wf: WF, patient, reason=None):
    p = wf.get("Patient", patient)
    enc = _encounter(wf, p, "AMB", "ambulatory", "in-progress", wf.location("opd-1"), wf.practitioner("attending"),
                     reason)
    enc = wf.t.create(enc)
    wf.emit("patient-register", [enc], [p])
    return {"encounter": enc, "patient": p}
