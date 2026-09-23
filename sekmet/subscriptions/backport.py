"""R4 Subscriptions Backport IG (topic-based subscriptions on FHIR R4).

Topics are R4 `Basic` resources carrying the R5 cross-version `extension-SubscriptionTopic.*` extensions (the
same encoding fhir-candle uses), so peers and users can read them and add their own. Notifications are
history Bundles whose first entry is a `Parameters` resource (backport SubscriptionStatus, kebab-case parameter
names as sent by reference implementations); both kebab and camelCase names are accepted on receipt.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from fhirpathpy import evaluate as fp_evaluate

from ..fhir.common import instant, new_id

BP = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition"
PROFILE = f"{BP}/backport-subscription"
EXT_FILTER = f"{BP}/backport-filter-criteria"
EXT_CONTENT = f"{BP}/backport-payload-content"
EXT_HEARTBEAT = f"{BP}/backport-heartbeat-period"
EXT_TIMEOUT = f"{BP}/backport-timeout"
EXT_MAX_COUNT = f"{BP}/backport-max-count"
EXT_TOPIC_CANONICAL = f"{BP}/capabilitystatement-subscriptiontopic-canonical"
ST = "http://hl7.org/fhir/5.0/StructureDefinition/extension-SubscriptionTopic"
TOPIC_CODE = {"coding": [{"system": "http://hl7.org/fhir/fhir-types", "code": "SubscriptionTopic"}]}
SEKMET_TOPIC_BASE = "http://sekmet.dev/fhir/SubscriptionTopic"


# ---------------- topics ----------------

@dataclass
class Trigger:
    resource: str
    interactions: list[str]
    fhirpath: str | None = None
    query_current: str | None = None


@dataclass
class Topic:
    url: str
    title: str
    triggers: list[Trigger]
    filters: dict[str, str] = field(default_factory=dict)  # filterParameter -> resource
    basic_id: str | None = None

    def trigger_for(self, rtype: str) -> Trigger | None:
        return next((t for t in self.triggers if t.resource == rtype), None)


def _ext(node: dict, url: str) -> list[dict]:
    return [e for e in node.get("extension", []) if e.get("url") == url]


def _val(e: dict) -> Any:
    return next((v for k, v in e.items() if k.startswith("value")), None)


def topic_from_basic(basic: dict) -> Topic | None:
    urls = _ext(basic, f"{ST}.url")
    if not urls:
        return None
    triggers, filters = [], {}
    for rt in _ext(basic, f"{ST}.resourceTrigger"):
        res = next((_val(p) for p in rt.get("extension", []) if p["url"] == "resource"), "")
        inter = [_val(p) for p in rt.get("extension", []) if p["url"] == "supportedInteraction"]
        fp = next((_val(p) for p in rt.get("extension", []) if p["url"] == "fhirPathCriteria"), None)
        qc = None
        for q in (p for p in rt.get("extension", []) if p["url"] == "queryCriteria"):
            qc = next((_val(x) for x in q.get("extension", []) if x["url"] == "current"), None)
        triggers.append(Trigger(res.rsplit("/", 1)[-1], inter or ["create", "update"], fp, qc))
    for cf in _ext(basic, f"{ST}.canFilterBy"):
        param = next((_val(p) for p in cf.get("extension", []) if p["url"] == "filterParameter"), None)
        res = next((_val(p) for p in cf.get("extension", []) if p["url"] == "resource"), "")
        if param:
            filters[param] = res.rsplit("/", 1)[-1]
    title = next((_val(e) for e in _ext(basic, f"{ST}.title")), None) or basic.get("id", "")
    return Topic(_val(urls[0]), title, triggers, filters, basic.get("id"))


def topic_basic(name: str, title: str, description: str, resource: str, fhirpath: str,
                filters: list[tuple[str, str]], interactions=("create", "update")) -> dict:
    trig = [{"url": "description", "valueMarkdown": description},
            {"url": "resource", "valueUri": f"http://hl7.org/fhir/StructureDefinition/{resource}"},
            *[{"url": "supportedInteraction", "valueCode": i} for i in interactions],
            {"url": "fhirPathCriteria", "valueString": fhirpath}]
    return {
        "resourceType": "Basic",
        "id": f"topic-{name}",
        "modifierExtension": [{"url": f"{ST}.status", "valueCode": "active"}],
        "extension": [
            {"url": f"{ST}.url", "valueUri": f"{SEKMET_TOPIC_BASE}/{name}"},
            {"url": f"{ST}.version", "valueString": "1.0.0"},
            {"url": f"{ST}.name", "valueString": name.replace("-", "_")},
            {"url": f"{ST}.title", "valueString": title},
            {"url": f"{ST}.description", "valueMarkdown": description},
            {"url": f"{ST}.resourceTrigger", "extension": trig},
            *[{"url": f"{ST}.canFilterBy", "extension": [
                {"url": "description", "valueMarkdown": f"Filter on {p}"},
                {"url": "resource", "valueUri": resource},
                {"url": "filterParameter", "valueString": p}]} for p, _ in filters],
        ],
        "code": TOPIC_CODE,
    }


def _became(status_expr: str) -> str:
    return f"(%previous.empty() or %previous.status != {status_expr}) and %current.status = {status_expr}"


BUILTIN_TOPICS = [
    topic_basic("encounter-start", "Encounter started", "An Encounter moved to in-progress", "Encounter",
                _became("'in-progress'"), [("patient", "Encounter"), ("class", "Encounter")]),
    topic_basic("encounter-complete", "Encounter completed", "An Encounter moved to finished", "Encounter",
                _became("'finished'"), [("patient", "Encounter"), ("class", "Encounter")]),
    topic_basic("diagnosticreport-final", "Report finalised", "A DiagnosticReport became final/amended/corrected",
                "DiagnosticReport",
                "(%previous.empty() or %previous.status != %current.status) and "
                "%current.status in ('final' | 'amended' | 'corrected')",
                [("patient", "DiagnosticReport"), ("category", "DiagnosticReport")]),
    topic_basic("servicerequest-new", "New order", "A ServiceRequest was created active", "ServiceRequest",
                "%previous.empty() and %current.status = 'active'",
                [("patient", "ServiceRequest"), ("category", "ServiceRequest")], interactions=("create",)),
    topic_basic("appointment-booked", "Appointment booked", "An Appointment moved to booked", "Appointment",
                _became("'booked'"), [("patient", "Appointment"), ("actor", "Appointment")]),
    topic_basic("patient-change", "Patient changed", "A Patient was created or updated", "Patient", "true",
                [("_id", "Patient"), ("identifier", "Patient")]),
]


def trigger_fires(trigger: Trigger, action: str, current: dict, previous: dict | None) -> bool:
    if action not in trigger.interactions:
        return False
    if trigger.fhirpath:
        try:
            env = {"current": current, "previous": previous or []}
            res = fp_evaluate(current, trigger.fhirpath, env)
        except Exception:
            return False
        return bool(res) and all(r is True for r in res)
    return True  # queryCriteria-only topics: evaluated by the caller against the stored resource


# ---------------- subscriptions ----------------

def is_backport(sub: dict) -> bool:
    crit = sub.get("criteria") or ""
    if crit.startswith(("http://", "https://", "urn:")) and "?" not in crit:
        return True  # criteria is a topic canonical
    return PROFILE in (sub.get("meta", {}).get("profile") or []) or bool(
        (sub.get("_criteria") or {}).get("extension")) or bool(
        (sub.get("channel", {}).get("_payload") or {}).get("extension"))


def filters_of(sub: dict) -> list[str]:
    return [_val(e) for e in (sub.get("_criteria") or {}).get("extension", []) if e.get("url") == EXT_FILTER]


def content_of(sub: dict) -> str:
    exts = (sub.get("channel", {}).get("_payload") or {}).get("extension", [])
    return next((_val(e) for e in exts if e.get("url") == EXT_CONTENT), "id-only" if sub["channel"].get("payload")
                else "empty")


def channel_int(sub: dict, url: str) -> int | None:
    v = next((_val(e) for e in sub.get("channel", {}).get("extension", []) if e.get("url") == url), None)
    return int(v) if v is not None else None


def build_subscription(topic: str, endpoint: str, filters: list[str] | None = None, content: str = "id-only",
                       headers: list[str] | None = None, heartbeat: int | None = None,
                       reason: str = "SEKMET backport subscription") -> dict:
    sub: dict[str, Any] = {
        "resourceType": "Subscription",
        "meta": {"profile": [PROFILE]},
        "status": "requested",
        "reason": reason,
        "criteria": topic,
        "channel": {"type": "rest-hook", "endpoint": endpoint, "payload": "application/fhir+json",
                    "_payload": {"extension": [{"url": EXT_CONTENT, "valueCode": content}]}},
    }
    if filters:
        sub["_criteria"] = {"extension": [{"url": EXT_FILTER, "valueString": f} for f in filters]}
    if headers:
        sub["channel"]["header"] = headers
    if heartbeat:
        sub["channel"]["extension"] = [{"url": EXT_HEARTBEAT, "valueUnsignedInt": heartbeat}]
    return sub


# ---------------- notifications ----------------

def status_parameters(sub: dict, base: str, ntype: str, events_since: int,
                      events: list[dict] | None = None, status: str | None = None) -> dict:
    params: list[dict] = [
        {"name": "subscription", "valueReference": {"reference": f"{base}/Subscription/{sub['id']}"}},
        {"name": "topic", "valueCanonical": sub.get("criteria")},
        {"name": "status", "valueCode": status or sub.get("status", "active")},
        {"name": "type", "valueCode": ntype},
        {"name": "events-since-subscription-start", "valueString": str(events_since)},
    ]
    for ev in events or []:
        parts = [{"name": "event-number", "valueString": str(ev["number"])},
                 {"name": "timestamp", "valueInstant": ev.get("timestamp") or instant()}]
        if ev.get("focus"):
            parts.append({"name": "focus", "valueReference": {"reference": f"{base}/{ev['focus']}"}})
        parts += [{"name": "additional-context", "valueReference": {"reference": f"{base}/{c}"}}
                  for c in ev.get("context", [])]
        params.append({"name": "notification-event", "part": parts})
    return {"resourceType": "Parameters", "id": new_id(), "parameter": params}


def notification_bundle(sub: dict, base: str, ntype: str, events_since: int, events: list[dict] | None = None,
                        resources: list[dict] | None = None, status: str | None = None) -> dict:
    """events: [{number, timestamp, focus: 'Type/id', context: [...]}]; resources: full-resource payload."""
    params = status_parameters(sub, base, ntype, events_since, events, status)
    entries = [{"fullUrl": f"urn:uuid:{params['id']}", "resource": params}]
    for r in resources or []:
        entries.append({"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": copy.deepcopy(r),
                        "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
                        "response": {"status": "200"}})
    return {"resourceType": "Bundle", "id": new_id(), "type": "history", "timestamp": instant(), "entry": entries}


def parse_notification(body: Any) -> dict | None:
    """Parse a backport notification Bundle (R4 Parameters or R4B SubscriptionStatus). None if not one."""
    if not isinstance(body, dict) or body.get("resourceType") != "Bundle" or body.get("type") != "history":
        return None
    entries = body.get("entry") or []
    first = (entries[0].get("resource") if entries else None) or {}
    out: dict[str, Any] = {"type": None, "subscription": None, "topic": None, "events": [], "resources": []}
    if first.get("resourceType") == "Parameters":
        for p in first.get("parameter", []):
            n = p.get("name")
            if n == "type":
                out["type"] = p.get("valueCode")
            elif n == "subscription":
                out["subscription"] = (p.get("valueReference") or {}).get("reference")
            elif n == "topic":
                out["topic"] = p.get("valueCanonical") or p.get("valueUri")
            elif n in ("notification-event", "notificationEvent"):
                ev = {}
                for part in p.get("part", []):
                    pn = part.get("name")
                    if pn in ("event-number", "eventNumber"):
                        ev["number"] = part.get("valueString") or part.get("valueInteger")
                    elif pn == "focus":
                        ev["focus"] = (part.get("valueReference") or {}).get("reference")
                out["events"].append(ev)
    elif first.get("resourceType") == "SubscriptionStatus":  # R4B
        out["type"] = first.get("type")
        out["subscription"] = (first.get("subscription") or {}).get("reference")
        out["topic"] = first.get("topic")
        out["events"] = [{"number": e.get("eventNumber"), "focus": (e.get("focus") or {}).get("reference")}
                         for e in first.get("notificationEvent", [])]
    else:
        return None
    out["resources"] = [e["resource"] for e in entries[1:] if isinstance(e.get("resource"), dict)]
    return out
