"""Workflow targets: the same hospital workflow code can write to the local store, to a peer over REST,
or locally while emitting FHIR messages to a peer."""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from ..client.peer import PeerClient, PeerError
from ..fhir.bundle import process_bundle
from ..fhir.common import FhirError, parse_reference, walk_references


class WorkflowError(Exception):
    pass


@dataclass
class TxEntry:
    resource: dict
    method: str = "POST"
    url: str | None = None
    full_url: str = field(default_factory=lambda: f"urn:uuid:{uuid.uuid4()}")
    if_none_exist: str | None = None


class Tx:
    """Collects entries for a transaction; `ref()` returns the urn:uuid reference to use in other entries."""

    def __init__(self):
        self.entries: list[TxEntry] = []

    def add(self, resource: dict, if_none_exist: str | None = None) -> dict:
        e = TxEntry(resource, "POST", resource["resourceType"], if_none_exist=if_none_exist)
        self.entries.append(e)
        return {"reference": e.full_url}

    def put(self, resource: dict) -> dict:
        e = TxEntry(resource, "PUT", f"{resource['resourceType']}/{resource['id']}",
                    full_url=f"{resource['resourceType']}/{resource['id']}")
        self.entries.append(e)
        return {"reference": f"{resource['resourceType']}/{resource['id']}"}

    def bundle(self) -> dict:
        entries = []
        for e in self.entries:
            req: dict[str, Any] = {"method": e.method, "url": e.url}
            if e.if_none_exist:
                req["ifNoneExist"] = e.if_none_exist
            entry: dict[str, Any] = {"resource": e.resource, "request": req}
            if e.full_url.startswith("urn:"):
                entry["fullUrl"] = e.full_url
            entries.append(entry)
        return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


class Target:
    name = "target"
    kind = "abstract"

    def create(self, resource: dict, if_none_exist: str | None = None) -> dict: raise NotImplementedError
    def update(self, resource: dict) -> dict: raise NotImplementedError
    def read(self, rtype: str, rid: str) -> dict: raise NotImplementedError
    def search(self, rtype: str, params: list[tuple[str, str]] | dict) -> list[dict]: raise NotImplementedError
    def commit(self, tx: Tx) -> list[dict]: raise NotImplementedError

    def emit(self, event: str, focus: list[dict], related: list[dict] | None = None) -> None:
        """Called by workflows after each business event; only the messaging target acts on it."""

    def ensure(self, resource: dict, query: str) -> dict:
        found = self.search(resource["resourceType"], parse_qsl(query))
        return found[0] if found else self.create(resource)

    def resolve(self, reference: str | dict) -> dict:
        r = reference["reference"] if isinstance(reference, dict) else reference
        t, i, _ = parse_reference(r)
        if not t:
            raise WorkflowError(f"Cannot resolve reference {r}")
        return self.read(t, i)

    def describe(self) -> str:
        return f"{self.kind}:{self.name}"


class LocalTarget(Target):
    kind = "local"

    def __init__(self, svc, origin: str = "internal"):
        self.svc = svc
        self.origin = origin
        self.name = "local"

    def _check(self, res):
        if not res.ok:
            raise WorkflowError(f"Local write failed: {res.body}")
        return res.body

    def create(self, resource, if_none_exist=None):
        body = self._check(self.svc.create(resource["resourceType"], resource, if_none_exist=if_none_exist,
                                           origin=self.origin))
        return body

    def update(self, resource):
        return self._check(self.svc.update(resource["resourceType"], resource["id"], resource, origin=self.origin))

    def read(self, rtype, rid):
        try:
            return self._check(self.svc.read(rtype, rid))
        except FhirError as e:
            raise WorkflowError(f"Local read {rtype}/{rid}: {e.message}")

    def search(self, rtype, params):
        items = list(params.items()) if isinstance(params, dict) else list(params)
        res = self.svc.search(rtype, [*items, ("_count", "200")])
        return [e["resource"] for e in res.body.get("entry", []) if e.get("search", {}).get("mode") == "match"]

    def commit(self, tx: Tx) -> list[dict]:
        try:
            res = process_bundle(self.svc, tx.bundle(), origin=self.origin)
        except FhirError as e:
            raise WorkflowError(f"Local transaction failed: {e.message}")
        return [e.get("resource") for e in res.body["entry"]]


class PeerTarget(Target):
    kind = "peer"

    def __init__(self, client: PeerClient):
        self.client = client
        self.name = client.name

    def create(self, resource, if_none_exist=None):
        r = self.client.create(resource, if_none_exist)
        if not r.ok:
            raise WorkflowError(f"Peer {self.name} create {resource['resourceType']} failed: HTTP {r.status} "
                                f"{r.outcome_text()}")
        return self.client.fetch_resource(r, resource["resourceType"])

    def update(self, resource):
        r = self.client.update(resource)
        if not r.ok:
            raise WorkflowError(f"Peer {self.name} update {resource['resourceType']}/{resource['id']} failed: "
                                f"HTTP {r.status} {r.outcome_text()}")
        return self.client.fetch_resource(r, resource["resourceType"])

    def read(self, rtype, rid):
        r = self.client.read(rtype, rid)
        if not r.ok:
            raise WorkflowError(f"Peer {self.name} read {rtype}/{rid} failed: HTTP {r.status} {r.outcome_text()}")
        return r.resource

    def search(self, rtype, params):
        r = self.client.search(rtype, params)
        if not r.ok:
            raise WorkflowError(f"Peer {self.name} search {rtype} failed: HTTP {r.status} {r.outcome_text()}")
        return r.entries()

    def commit(self, tx: Tx) -> list[dict]:
        if self.client.peer.use_transactions:
            r = self.client.transaction(tx.bundle())
            if not r.ok:
                raise WorkflowError(f"Peer {self.name} transaction failed: HTTP {r.status} {r.outcome_text()}")
            out = []
            for req_entry, resp_entry in zip(tx.entries, (r.resource or {}).get("entry", [])):
                res = resp_entry.get("resource")
                if not res or res.get("resourceType") != req_entry.resource["resourceType"]:
                    loc = resp_entry.get("response", {}).get("location", "")
                    t, i, _ = parse_reference(loc)
                    res = self.read(t, i) if t else None
                out.append(res)
            return out
        # sequential fallback: resolve urn:uuid references client-side
        mapping: dict[str, str] = {}
        out = []
        for e in tx.entries:
            res = copy.deepcopy(e.resource)
            for r in walk_references(res):
                if r["reference"] in mapping:
                    r["reference"] = mapping[r["reference"]]
            if e.method == "PUT":
                written = self.update(res)
            else:
                written = self.create(res, e.if_none_exist)
            mapping[e.full_url] = f"{written['resourceType']}/{written['id']}"
            out.append(written)
        return out


class MessagingTarget(LocalTarget):
    """Writes locally (we are the source of truth) and sends a FHIR message to the peer per business event."""
    kind = "messaging"

    def __init__(self, svc, client: PeerClient, messenger):
        super().__init__(svc)
        self.client = client
        self.messenger = messenger
        self.name = client.name
        self.acks: list[dict] = []

    def emit(self, event, focus, related=None):
        ack = self.messenger.send(self.client, event, focus, related or [])
        self.acks.append(ack)
        if not ack.get("ok"):
            raise WorkflowError(f"Message {event} to {self.name} not accepted: {ack.get('detail')}")


def make_target(ctx, spec: str | None) -> Target:
    """spec: 'local' | '<peer>' | 'rest:<peer>' | 'messaging:<peer>' (peer mode decides for bare names)."""
    spec = spec or "local"
    if spec == "local":
        return LocalTarget(ctx.service)
    mode, _, name = spec.partition(":")
    if not name:
        name, mode = mode, None
    peer = ctx.settings.peer(name)
    mode = mode or peer.mode
    client = ctx.peer_client(name)
    if mode == "messaging":
        return MessagingTarget(ctx.service, client, ctx.messenger)
    return PeerTarget(client)


__all__ = ["Target", "LocalTarget", "PeerTarget", "MessagingTarget", "Tx", "WorkflowError", "make_target", "PeerError"]
