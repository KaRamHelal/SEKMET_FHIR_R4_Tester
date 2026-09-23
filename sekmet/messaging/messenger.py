"""FHIR Messaging: build/send message Bundles to peers and process inbound messages at $process-message."""
from __future__ import annotations

import copy
from typing import Any, Callable

from ..fhir.common import FhirError, instant, new_id, operation_outcome, parse_reference, walk_references
from ..fhir.service import FhirService, Result
from ..workflows.catalog import EVENTS

RESPONSE_CODE_SYSTEM = "http://hl7.org/fhir/response-code"

InboundHandler = Callable[["Messenger", dict, list[dict], dict], None]


def event_coding(event: str) -> dict:
    if event in EVENTS:
        system, code, display = EVENTS[event]
        return {"system": system, "code": code, "display": display}
    return {"system": "urn:sekmet:events", "code": event}


class Messenger:
    def __init__(self, ctx):
        self.ctx = ctx
        # event code -> handler(messenger, header, resources, bundle) for inbound customisation
        self.handlers: dict[str, InboundHandler] = {"A40": _handle_merge}

    @property
    def svc(self) -> FhirService:
        return self.ctx.service

    # ---------------- outbound ----------------

    def build(self, event: str, focus: list[dict], related: list[dict], destination: str, dest_name: str) -> dict:
        s = self.ctx.settings
        base = s.base_url.rstrip("/")
        header = {
            "resourceType": "MessageHeader",
            "id": new_id(),
            "eventCoding": event_coding(event),
            "destination": [{"name": dest_name, "endpoint": destination}],
            "sender": {"display": s.facility_name},
            "source": {"name": s.software_name, "software": s.software_name, "version": "0.1.0",
                       "endpoint": f"{base}/$process-message"},
            "focus": [{"reference": f"{base}/{f['resourceType']}/{f['id']}"} for f in focus],
        }
        seen, entries = set(), [{"fullUrl": f"urn:uuid:{header['id']}", "resource": header}]
        # include focus, related and anything they reference that we hold locally (one level)
        queue = [*focus, *related]
        for r in list(queue):
            for ref in walk_references(r):
                t, i, _ = parse_reference(ref["reference"])
                if t and (t, i) not in {(q["resourceType"], q.get("id")) for q in queue}:
                    try:
                        found = self.svc.store.read(t, i)
                    except FhirError:
                        found = None
                    if found:
                        queue.append(found)
        for r in queue:
            key = (r["resourceType"], r.get("id"))
            if key in seen or not r.get("id"):
                continue
            seen.add(key)
            entries.append({"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": r})
        return {"resourceType": "Bundle", "id": new_id(), "type": "message", "timestamp": instant(),
                "identifier": {"system": s.identifiers.message, "value": new_id()}, "entry": entries}

    def send(self, client, event: str, focus: list[dict], related: list[dict]) -> dict:
        peer = client.peer
        dest = peer.message_destination or peer.base_url
        bundle = self.build(event, focus, related, dest, client.name)
        r = client.process_message(bundle)
        ack = {"event": event, "status": r.status, "message_id": bundle["entry"][0]["resource"]["id"],
               "traffic_id": r.traffic_id, "ok": False, "detail": ""}
        body = r.resource or {}
        if r.ok and body.get("resourceType") == "Bundle" and body.get("type") == "message":
            hdr = (body.get("entry") or [{}])[0].get("resource", {})
            resp = hdr.get("response", {})
            ack["response_code"] = resp.get("code")
            ack["ok"] = resp.get("code") == "ok"
            ack["detail"] = f"response.code={resp.get('code')}"
            if resp.get("identifier") and resp["identifier"] != ack["message_id"]:
                ack["detail"] += f" (response.identifier {resp['identifier']} != request id {ack['message_id']})"
        elif r.ok:
            ack["ok"] = True
            ack["detail"] = f"HTTP {r.status} without a response message"
        else:
            ack["detail"] = f"HTTP {r.status}: {r.outcome_text()}"
        return ack

    # ---------------- inbound ----------------

    def process(self, bundle: Any, params: list[tuple[str, str]], origin: str = "message") -> Result:
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle" or bundle.get("type") != "message":
            raise FhirError(400, "$process-message requires a Bundle of type 'message'", "invalid")
        entries = bundle.get("entry") or []
        if not entries or entries[0].get("resource", {}).get("resourceType") != "MessageHeader":
            raise FhirError(400, "The first entry of a message Bundle must be a MessageHeader", "invalid")
        header = entries[0]["resource"]
        if not header.get("eventCoding") and not header.get("eventUri"):
            raise FhirError(400, "MessageHeader.event[x] is required", "required")
        if dict(params).get("async") == "true":
            note = "async=true requested; processed synchronously"
        else:
            note = None
        details = None
        code = "ok"
        try:
            resources = self._persist(bundle, origin)
            ev = (header.get("eventCoding") or {}).get("code") or header.get("eventUri")
            handler = self.handlers.get(ev)
            if handler:
                handler(self, header, resources, bundle)
            stored = copy.deepcopy(bundle)
            self.svc.store.create(stored, origin=origin)
        except FhirError as e:
            code = "fatal-error"
            details = e.outcome()
        except Exception as e:  # report any processing failure back as transient
            code = "transient-error"
            details = operation_outcome([{"severity": "error", "code": "exception", "diagnostics": str(e)}])
        return Result(200, self._response(header, code, details, note))

    def _persist(self, bundle: dict, origin: str) -> list[dict]:
        """Store every non-header entry, keeping sender ids (PUT semantics); rewrite fullUrl references."""
        entries = bundle["entry"][1:]
        mapping: dict[str, str] = {}
        prepared = []
        for e in entries:
            res = e.get("resource")
            if not isinstance(res, dict) or not res.get("resourceType"):
                continue
            res = copy.deepcopy(res)
            if not res.get("id"):
                res["id"] = new_id()
            full = e.get("fullUrl")
            if full:
                mapping[full] = f"{res['resourceType']}/{res['id']}"
            prepared.append(res)
        out = []
        with self.svc.store.transaction():
            for res in prepared:
                for r in walk_references(res):
                    if r["reference"] in mapping:
                        r["reference"] = mapping[r["reference"]]
                    else:
                        t, i, _ = parse_reference(r["reference"])
                        if t and r["reference"].startswith(("http://", "https://")):
                            r["reference"] = f"{t}/{i}"
                result = self.svc.update(res["resourceType"], res["id"], res, origin=origin)
                out.append(result.body)
        return out

    def _response(self, request_header: dict, code: str, details: dict | None, note: str | None) -> dict:
        s = self.ctx.settings
        base = s.base_url.rstrip("/")
        hdr: dict[str, Any] = {
            "resourceType": "MessageHeader",
            "id": new_id(),
            "eventCoding": request_header.get("eventCoding"),
            "destination": [{"endpoint": (request_header.get("source") or {}).get("endpoint", "unknown")}],
            "source": {"name": s.software_name, "endpoint": f"{base}/$process-message"},
            "response": {"identifier": request_header.get("id", ""), "code": code},
        }
        if hdr["eventCoding"] is None:
            hdr.pop("eventCoding")
            hdr["eventUri"] = request_header.get("eventUri")
        entries = [{"fullUrl": f"urn:uuid:{hdr['id']}", "resource": hdr}]
        if details or note:
            oo = details or operation_outcome(text=note)
            if note and details:
                oo["issue"].append({"severity": "information", "code": "informational", "diagnostics": note})
            hdr["response"]["details"] = {"reference": f"urn:uuid:{oo['id']}"}
            entries.append({"fullUrl": f"urn:uuid:{oo['id']}", "resource": oo})
        return {"resourceType": "Bundle", "id": new_id(), "type": "message", "timestamp": instant(), "entry": entries}


def _handle_merge(m: Messenger, header: dict, resources: list[dict], bundle: dict) -> None:
    """A40: mark patients linked 'replaced-by' as inactive (already persisted as sent)."""
    for r in resources:
        if r["resourceType"] == "Patient" and any(l.get("type") == "replaced-by" for l in r.get("link", [])):
            if r.get("active") is not False:
                r = dict(r, active=False)
                m.svc.update("Patient", r["id"], r, origin="message")


def register(svc: FhirService, messenger: Messenger) -> None:
    svc.register_operation("*", "$process-message",
                           lambda s, rtype, rid, params, body, ctx: messenger.process(
                               body, params, "message" if ctx.get("origin", "external") == "external" else ctx["origin"]))
