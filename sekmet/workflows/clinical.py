"""Medications (prescribe/dispense/administer/stop) and clinical documentation (conditions, allergies,
procedures, vital signs)."""
from __future__ import annotations

import copy
import random

from ..fhir.common import codeable, instant, ref
from . import catalog as C
from .registry import Param, WF, workflow
from .target import Tx, WorkflowError


def _enc_ref(wf: WF, encounter) -> dict | None:
    return ref(wf.get("Encounter", encounter)) if encounter else None


@workflow("meds.prescribe", "Prescribe medication (MedicationRequest)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("medication", "Medication (RxNorm)", "choice", "197361", list(C.MEDICATIONS)),
    Param("days", "Days supply", "int", 30),
    Param("inpatient", "Inpatient order", "bool", False),
])
def prescribe(wf: WF, patient, encounter=None, medication="197361", days=30, inpatient=False):
    p = wf.get("Patient", patient)
    display, dose, unit, freq, route, route_display = C.MEDICATIONS[medication]
    mr = {
        "resourceType": "MedicationRequest",
        "identifier": [wf.ident(wf.ids.prescription, wf.number("RX"), "PLAC")],
        "status": "active",
        "intent": "order",
        "category": [codeable(C.MED_REQ_CAT, "inpatient" if inpatient else "outpatient",
                              "Inpatient" if inpatient else "Outpatient")],
        "priority": "routine",
        "medicationCodeableConcept": codeable(C.RXNORM, medication, display),
        "subject": ref(p),
        "authoredOn": instant(),
        "requester": ref(wf.practitioner("attending")),
        "dosageInstruction": [{
            "sequence": 1,
            "text": f"{dose:g} {unit} by mouth {freq} time(s) daily",
            "timing": {"repeat": {"frequency": freq, "period": 1, "periodUnit": "d"}},
            "route": codeable(C.SNOMED, route, route_display),
            "doseAndRate": [{"type": codeable("http://terminology.hl7.org/CodeSystem/dose-rate-type", "ordered",
                                              "Ordered"),
                             "doseQuantity": {"value": dose, "unit": unit,
                                              "system": "http://terminology.hl7.org/CodeSystem/v3-orderableDrugForm",
                                              "code": "TAB"}}],
        }],
        "dispenseRequest": {
            "validityPeriod": {"start": instant()[:10]},
            "numberOfRepeatsAllowed": 0 if inpatient else 2,
            "quantity": {"value": dose * freq * days, "unit": unit},
            "expectedSupplyDuration": {"value": days, "unit": "days", "system": C.UCUM, "code": "d"},
            "performer": ref(wf.org("pharmacy")),
        },
    }
    enc = _enc_ref(wf, encounter)
    if enc:
        mr["encounter"] = enc
    mr = wf.t.create(mr)
    wf.emit("prescription", [mr], [p])
    return {"medication_request": mr, "patient": p}


@workflow("meds.dispense", "Dispense medication (MedicationDispense)", [
    Param("medication_request", "MedicationRequest", "ref", required=True, ref_type="MedicationRequest"),
])
def dispense(wf: WF, medication_request):
    mr = wf.get("MedicationRequest", medication_request)
    if mr.get("status") != "active":
        raise WorkflowError(f"MedicationRequest status is {mr.get('status')}, expected active")
    dr = mr.get("dispenseRequest", {})
    md = {
        "resourceType": "MedicationDispense",
        "identifier": [wf.ident(wf.ids.prescription, wf.number("DSP"))],
        "status": "completed",
        "medicationCodeableConcept": mr.get("medicationCodeableConcept"),
        "subject": mr["subject"],
        "performer": [{"function": codeable("http://terminology.hl7.org/CodeSystem/medicationdispense-performer-function",
                                            "finalchecker", "Final Checker"),
                       "actor": ref(wf.practitioner("pharmacist"))}],
        "location": ref(wf.location("ward-4a")),
        "authorizingPrescription": [ref(mr)],
        "type": codeable("http://terminology.hl7.org/CodeSystem/v3-ActCode", "FF", "First Fill"),
        "quantity": dr.get("quantity"),
        "daysSupply": dr.get("expectedSupplyDuration"),
        "whenPrepared": instant(),
        "whenHandedOver": instant(),
        "dosageInstruction": mr.get("dosageInstruction"),
    }
    if mr.get("encounter"):
        md["context"] = mr["encounter"]
    md = {k: v for k, v in md.items() if v is not None}
    md = wf.t.create(md)
    wf.emit("dispense", [md], [mr])
    return {"medication_dispense": md, "medication_request": mr}


@workflow("meds.administer", "Administer medication (MedicationAdministration)", [
    Param("medication_request", "MedicationRequest", "ref", required=True, ref_type="MedicationRequest"),
])
def administer(wf: WF, medication_request):
    mr = wf.get("MedicationRequest", medication_request)
    di = (mr.get("dosageInstruction") or [{}])[0]
    dose = (di.get("doseAndRate") or [{}])[0].get("doseQuantity")
    ma = {
        "resourceType": "MedicationAdministration",
        "identifier": [wf.ident(wf.ids.prescription, wf.number("MAR"))],
        "status": "completed",
        "medicationCodeableConcept": mr.get("medicationCodeableConcept"),
        "subject": mr["subject"],
        "effectiveDateTime": instant(),
        "performer": [{"actor": ref(wf.practitioner("nurse"))}],
        "request": ref(mr),
        "dosage": {"text": di.get("text"), "route": di.get("route"), "dose": dose},
    }
    if mr.get("encounter"):
        ma["context"] = mr["encounter"]
    ma["dosage"] = {k: v for k, v in ma["dosage"].items() if v}
    ma = wf.t.create(ma)
    wf.emit("administration", [ma], [mr])
    return {"medication_administration": ma, "medication_request": mr}


@workflow("meds.stop", "Stop / discontinue medication", [
    Param("medication_request", "MedicationRequest", "ref", required=True, ref_type="MedicationRequest"),
    Param("reason", "Reason", default="Therapy completed"),
])
def stop(wf: WF, medication_request, reason="Therapy completed"):
    mr = copy.deepcopy(wf.get("MedicationRequest", medication_request))
    mr["status"] = "stopped"
    mr["statusReason"] = {"text": reason}
    mr = wf.t.update(mr)
    wf.emit("prescription", [mr])
    return {"medication_request": mr}


@workflow("clinical.condition", "Record condition / diagnosis", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("code", "Condition (SNOMED)", "choice", "38341003", list(C.CONDITIONS)),
    Param("category", "Category", "choice", "encounter-diagnosis", ["encounter-diagnosis", "problem-list-item"]),
])
def condition(wf: WF, patient, encounter=None, code="38341003", category="encounter-diagnosis"):
    p = wf.get("Patient", patient)
    c = {
        "resourceType": "Condition",
        "clinicalStatus": codeable(C.COND_CLINICAL, "active", "Active"),
        "verificationStatus": codeable(C.COND_VER, "confirmed", "Confirmed"),
        "category": [codeable(C.COND_CAT, category)],
        "code": codeable(C.SNOMED, code, C.CONDITIONS[code]),
        "subject": ref(p),
        "onsetDateTime": instant(),
        "recordedDate": instant(),
        "recorder": ref(wf.practitioner("attending")),
    }
    enc = _enc_ref(wf, encounter)
    if enc:
        c["encounter"] = enc
    c = wf.t.create(c)
    wf.emit("clinical-update", [c], [p])
    return {"condition": c}


@workflow("clinical.allergy", "Record allergy", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("code", "Allergy (SNOMED)", "choice", "91936005", list(C.ALLERGIES)),
    Param("criticality", "Criticality", "choice", "high", ["low", "high", "unable-to-assess"]),
])
def allergy(wf: WF, patient, code="91936005", criticality="high"):
    p = wf.get("Patient", patient)
    display, category = C.ALLERGIES[code]
    a = {
        "resourceType": "AllergyIntolerance",
        "clinicalStatus": codeable(C.ALLERGY_CLINICAL, "active", "Active"),
        "verificationStatus": codeable(C.ALLERGY_VER, "confirmed", "Confirmed"),
        "type": "allergy",
        "category": [category],
        "criticality": criticality,
        "code": codeable(C.SNOMED, code, display),
        "patient": ref(p),
        "recordedDate": instant(),
        "recorder": ref(wf.practitioner("attending")),
        "reaction": [{"manifestation": [codeable(C.SNOMED, "271807003", "Eruption of skin")], "severity": "moderate"}],
    }
    a = wf.t.create(a)
    wf.emit("clinical-update", [a], [p])
    return {"allergy": a}


@workflow("clinical.procedure", "Record procedure", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("code", "Procedure (SNOMED)", "choice", "80146002", list(C.PROCEDURES)),
])
def procedure(wf: WF, patient, encounter=None, code="80146002"):
    p = wf.get("Patient", patient)
    pr = {
        "resourceType": "Procedure",
        "status": "completed",
        "code": codeable(C.SNOMED, code, C.PROCEDURES[code]),
        "subject": ref(p),
        "performedDateTime": instant(),
        "performer": [{"actor": ref(wf.practitioner("surgeon"))}],
        "location": ref(wf.location("ward-5b")),
    }
    enc = _enc_ref(wf, encounter)
    if enc:
        pr["encounter"] = enc
    pr = wf.t.create(pr)
    wf.emit("clinical-update", [pr], [p])
    return {"procedure": pr}


@workflow("clinical.vitals", "Record vital signs", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
])
def vitals(wf: WF, patient, encounter=None):
    p = wf.get("Patient", patient)
    enc = _enc_ref(wf, encounter)
    now = instant()
    cat = [codeable(C.OBS_CAT, "vital-signs", "Vital Signs")]
    tx = Tx()
    for code, (display, unit, low, high, dec) in C.VITALS.items():
        v = round(random.uniform(low, high), dec)
        o = {"resourceType": "Observation", "status": "final", "category": cat,
             "code": codeable(C.LOINC, code, display), "subject": ref(p), "effectiveDateTime": now,
             "performer": [ref(wf.practitioner("nurse"))],
             "valueQuantity": {"value": int(v) if dec == 0 else v, "unit": unit, "system": C.UCUM, "code": unit}}
        if enc:
            o["encounter"] = enc
        tx.add(o)
    bp = {"resourceType": "Observation", "status": "final", "category": cat,
          "code": codeable(C.LOINC, "85354-9", "Blood pressure panel with all children optional"),
          "subject": ref(p), "effectiveDateTime": now, "performer": [ref(wf.practitioner("nurse"))],
          "component": [
              {"code": codeable(C.LOINC, "8480-6", "Systolic blood pressure"),
               "valueQuantity": {"value": random.randint(105, 140), "unit": "mm[Hg]", "system": C.UCUM, "code": "mm[Hg]"}},
              {"code": codeable(C.LOINC, "8462-4", "Diastolic blood pressure"),
               "valueQuantity": {"value": random.randint(65, 90), "unit": "mm[Hg]", "system": C.UCUM, "code": "mm[Hg]"}},
          ]}
    if enc:
        bp["encounter"] = enc
    tx.add(bp)
    obs = wf.t.commit(tx)
    wf.emit("result", obs[:1], obs[1:])
    return {"observations": obs}
