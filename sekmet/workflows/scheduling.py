"""Scheduling: Schedule/Slot generation, Appointment request/book/respond/cancel/reschedule, check-in."""
from __future__ import annotations

import copy
from datetime import datetime, time as dtime, timedelta, timezone

from ..fhir.common import codeable, instant, parse_reference, ref, ref_type
from . import catalog as C
from .registry import PRACTITIONERS, Param, WF, workflow
from .target import Tx, WorkflowError

SERVICE_TYPES = {"124": "General Practice", "57": "Immunization", "165": "Radiology", "221": "Surgery - General"}


def _actor(appt: dict, rtype: str, required: bool = True) -> dict | None:
    actor = next((p["actor"] for p in appt.get("participant", []) if ref_type(p.get("actor")) == rtype), None)
    if actor is None and required:
        raise WorkflowError(f"Appointment/{appt.get('id')} has no {rtype} participant")
    return actor


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@workflow("scheduling.create_schedule", "Create schedule + free slots", [
    Param("practitioner", "Practitioner", "choice", "attending", list(PRACTITIONERS)),
    Param("service_type", "Service type", "choice", "124", list(SERVICE_TYPES)),
    Param("days", "Days ahead", "int", 3),
    Param("slot_minutes", "Slot length (min)", "int", 30),
    Param("start_hour", "Clinic start hour (UTC)", "int", 9),
    Param("end_hour", "Clinic end hour (UTC)", "int", 12),
])
def create_schedule(wf: WF, practitioner="attending", service_type="124", days=3, slot_minutes=30, start_hour=9,
                    end_hour=12):
    prac = wf.practitioner(practitioner)
    loc = wf.location("opd-1")
    today = datetime.now(timezone.utc).date()
    horizon_start = datetime.combine(today + timedelta(days=1), dtime(0), tzinfo=timezone.utc)
    horizon_end = horizon_start + timedelta(days=days)
    stype = codeable(C.SERVICE_TYPE, service_type, SERVICE_TYPES[service_type])
    tx = Tx()
    schedule = {
        "resourceType": "Schedule",
        "identifier": [wf.ident(wf.ids.appointment, wf.number("SCH"))],
        "active": True,
        "serviceType": [stype],
        "actor": [ref(prac), ref(loc)],
        "planningHorizon": {"start": _iso(horizon_start), "end": _iso(horizon_end)},
        "comment": f"Clinic for {prac.get('name', [{}])[0].get('family', '')}",
    }
    sched_ref = tx.add(schedule)
    n = 0
    for d in range(days):
        day = today + timedelta(days=1 + d)
        t = datetime.combine(day, dtime(start_hour), tzinfo=timezone.utc)
        end = datetime.combine(day, dtime(end_hour), tzinfo=timezone.utc)
        while t + timedelta(minutes=slot_minutes) <= end:
            tx.add({"resourceType": "Slot", "schedule": sched_ref, "status": "free", "serviceType": [stype],
                    "start": _iso(t), "end": _iso(t + timedelta(minutes=slot_minutes))})
            t += timedelta(minutes=slot_minutes)
            n += 1
    written = wf.t.commit(tx)
    return {"schedule": written[0], "slots": written[1:], "slot_count": n}


@workflow("scheduling.find_slots", "Find free slots", [
    Param("schedule", "Schedule", "ref", ref_type="Schedule"),
    Param("service_type", "Service type", "choice", "", ["", *SERVICE_TYPES]),
])
def find_slots(wf: WF, schedule=None, service_type=None):
    params = [("status", "free"), ("start", f"ge{_iso(datetime.now(timezone.utc))}"), ("_sort", "start")]
    if schedule:
        params.append(("schedule", f"Schedule/{wf.get('Schedule', schedule)['id']}"))
    if service_type:
        params.append(("service-type", service_type))
    slots = wf.t.search("Slot", params)
    return {"slots": slots, "first_slot": slots[0] if slots else None, "count": len(slots)}


def _pick_slot(wf: WF, slot) -> dict:
    if slot:
        return wf.get("Slot", slot)
    found = find_slots(wf)["first_slot"]
    if not found:
        raise WorkflowError("No free slot available; run scheduling.create_schedule first")
    return found


def _schedule_actors(wf: WF, slot: dict) -> tuple[dict | None, dict | None]:
    sched = wf.resolve(slot["schedule"])
    prac = loc = None
    for a in sched.get("actor", []):
        t, _, _ = parse_reference(a.get("reference", ""))
        if t == "Practitioner" and not prac:
            prac = a
        elif t == "Location" and not loc:
            loc = a
    return prac, loc


def _set_slot(wf: WF, slot: dict, status: str) -> dict:
    s = copy.deepcopy(slot)
    s["status"] = status
    return wf.t.update(s)


@workflow("scheduling.book", "Book appointment (S12)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("slot", "Slot (blank = first free)", "ref", ref_type="Slot"),
    Param("reason", "Reason", default="Follow-up visit"),
    Param("status", "Initial status", "choice", "booked", ["booked", "proposed", "pending"]),
])
def book(wf: WF, patient, slot=None, reason="Follow-up visit", status="booked"):
    p = wf.get("Patient", patient)
    s = _pick_slot(wf, slot)
    if s.get("status") != "free":
        raise WorkflowError(f"Slot {s['id']} is {s.get('status')}")
    prac, loc = _schedule_actors(wf, s)
    booked = status == "booked"
    appt = {
        "resourceType": "Appointment",
        "identifier": [wf.ident(wf.ids.appointment, wf.number("APT"))],
        "status": status,
        "serviceType": s.get("serviceType", []),
        "appointmentType": codeable("http://terminology.hl7.org/CodeSystem/v2-0276", "FOLLOWUP",
                                    "A follow up visit from a previous appointment"),
        "reasonCode": [{"text": reason}],
        "description": reason,
        "start": s["start"],
        "end": s["end"],
        "slot": [ref(s)],
        "created": instant()[:10],
        "participant": [{"actor": ref(p), "required": "required", "status": "accepted"}],
    }
    if prac:
        appt["participant"].append({"actor": prac, "required": "required",
                                    "status": "accepted" if booked else "needs-action",
                                    "type": [codeable(C.PARTICIPATION, "ATND", "attender")]})
    if loc:
        appt["participant"].append({"actor": loc, "required": "required", "status": "accepted"})
    tx = Tx()
    tx.add(appt)
    if booked:
        s2 = copy.deepcopy(s)
        s2["status"] = "busy"
        tx.put(s2)
    written = wf.t.commit(tx)
    appt = written[0]
    wf.emit("appointment-book", [appt], [p])
    return {"appointment": appt, "slot": written[1] if booked else s, "patient": p}


@workflow("scheduling.respond", "Practitioner responds (AppointmentResponse)", [
    Param("appointment", "Appointment", "ref", required=True, ref_type="Appointment"),
    Param("participant_status", "Response", "choice", "accepted", ["accepted", "declined", "tentative"]),
])
def respond(wf: WF, appointment, participant_status="accepted"):
    a = copy.deepcopy(wf.get("Appointment", appointment))
    actor = next((p["actor"] for p in a.get("participant", [])
                  if ref_type(p.get("actor")) == "Practitioner"), None)
    if not actor:
        raise WorkflowError("Appointment has no practitioner participant")
    resp = wf.t.create({"resourceType": "AppointmentResponse", "appointment": ref(a), "actor": actor,
                        "participantStatus": participant_status, "comment": "Response from SEKMET"})
    for p in a["participant"]:
        if p.get("actor") == actor:
            p["status"] = participant_status
    if participant_status == "declined":
        a["status"] = "cancelled"
        a["cancelationReason"] = {"text": "Declined by practitioner"}
    elif all(p.get("status") == "accepted" for p in a["participant"]):
        a["status"] = "booked"
        for sref in a.get("slot", []):
            slot = wf.resolve(sref)
            if slot.get("status") == "free":
                _set_slot(wf, slot, "busy")
    a = wf.t.update(a)
    wf.emit("appointment-book", [a], [resp])
    return {"appointment_response": resp, "appointment": a}


@workflow("scheduling.cancel", "Cancel appointment (S15)", [
    Param("appointment", "Appointment", "ref", required=True, ref_type="Appointment"),
    Param("reason", "Reason", default="Patient request"),
])
def cancel(wf: WF, appointment, reason="Patient request"):
    a = copy.deepcopy(wf.get("Appointment", appointment))
    if a.get("status") in ("cancelled", "fulfilled", "noshow"):
        raise WorkflowError(f"Appointment already {a.get('status')}")
    a["status"] = "cancelled"
    a["cancelationReason"] = codeable("http://terminology.hl7.org/CodeSystem/appointment-cancellation-reason",
                                      "pat", "Patient", reason)
    a = wf.t.update(a)
    for sref in a.get("slot", []):
        slot = wf.resolve(sref)
        if slot.get("status") == "busy":
            _set_slot(wf, slot, "free")
    wf.emit("appointment-cancel", [a])
    return {"appointment": a}


@workflow("scheduling.reschedule", "Reschedule appointment (S13)", [
    Param("appointment", "Appointment", "ref", required=True, ref_type="Appointment"),
    Param("slot", "New slot (blank = next free)", "ref", ref_type="Slot"),
])
def reschedule(wf: WF, appointment, slot=None):
    old = wf.get("Appointment", appointment)
    patient = _actor(old, "Patient")
    cancel(wf, old, "Rescheduled")
    new = book(wf, patient, slot, (old.get("reasonCode") or [{}])[0].get("text", "Rescheduled visit"))
    wf.events[-1] = "appointment-reschedule"
    return {"cancelled": wf.get("Appointment", old["id"]), **new}


@workflow("scheduling.check_in", "Check in (Appointment arrived + Encounter)", [
    Param("appointment", "Appointment", "ref", required=True, ref_type="Appointment"),
])
def check_in(wf: WF, appointment):
    a = copy.deepcopy(wf.get("Appointment", appointment))
    if a.get("status") != "booked":
        raise WorkflowError(f"Only booked appointments can be checked in (status={a.get('status')})")
    a["status"] = "arrived"
    for p in a["participant"]:
        if ref_type(p.get("actor")) == "Patient":
            p["status"] = "accepted"
    a = wf.t.update(a)
    patient = _actor(a, "Patient")
    prac = _actor(a, "Practitioner", required=False)
    enc = {
        "resourceType": "Encounter",
        "identifier": [wf.ident(wf.ids.visit, wf.number("V"), "VN")],
        "status": "arrived",
        "class": {"system": C.V3_ACT, "code": "AMB", "display": "ambulatory"},
        "subject": patient,
        "appointment": [ref(a)],
        "period": {"start": instant()},
        "serviceProvider": ref(wf.org("hospital")),
        "location": [{"location": ref(wf.location("opd-1")), "status": "active"}],
    }
    if prac:
        enc["participant"] = [{"type": [codeable(C.PARTICIPATION, "ATND", "attender")], "individual": prac}]
    enc = wf.t.create(enc)
    wf.emit("admit", [enc], [a])
    return {"appointment": a, "encounter": enc}


@workflow("scheduling.complete", "Complete visit (Appointment fulfilled, Encounter finished)", [
    Param("appointment", "Appointment", "ref", required=True, ref_type="Appointment"),
])
def complete(wf: WF, appointment):
    a = copy.deepcopy(wf.get("Appointment", appointment))
    a["status"] = "fulfilled"
    a = wf.t.update(a)
    encs = wf.t.search("Encounter", [("appointment", f"Appointment/{a['id']}")])
    enc = None
    if encs:
        enc = copy.deepcopy(encs[0])
        enc["status"] = "finished"
        enc.setdefault("period", {})["end"] = instant()
        enc = wf.t.update(enc)
    wf.emit("discharge", [enc or a])
    return {"appointment": a, "encounter": enc}
