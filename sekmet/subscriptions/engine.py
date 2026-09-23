"""R4 Subscriptions: local rest-hook engine (we notify subscribers) and remote registration + hook receiver
(peers notify us)."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, Request, Response

from ..fhir.common import FHIR_JSON, FhirError, instant, new_id, operation_outcome
from ..fhir.search import parse_criteria
from ..fhir.store import WriteEvent
from ..traffic.log import active_run, body_text, redact_headers
from . import backport as bp

log = logging.getLogger("sekmet.subscriptions")
SKIP_TYPES = {"Subscription", "Bundle", "MessageHeader", "AuditEvent", "OperationOutcome"}


class SubscriptionEngine:
    def __init__(self, ctx):
        self.ctx = ctx
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sub-notify")
        self._active: list[dict] | None = None
        self._topics: dict[str, bp.Topic] | None = None
        self._lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._heartbeat_started = False
        self.sync = False  # tests can deliver synchronously

    # ---------------- topics (backport) ----------------

    def seed_topics(self) -> None:
        """Make the built-in topics available as Basic resources (idempotent)."""
        for basic in bp.BUILTIN_TOPICS:
            if not self.store.exists("Basic", basic["id"]):
                self.store.update(basic, origin="subscription-engine")
        self._topics = None

    def topics(self) -> dict[str, "bp.Topic"]:
        with self._lock:
            if self._topics is None:
                res = self.ctx.service.search_engine.search("Basic", [("code", "SubscriptionTopic"), ("_count", "1000")])
                parsed = [bp.topic_from_basic(b) for b in res.matches]
                self._topics = {t.url: t for t in parsed if t}
            topics = dict(self._topics)
        if not topics:  # e.g. after `sekmet reset`
            self.seed_topics()
            return self.topics() if self.store.exists("Basic", bp.BUILTIN_TOPICS[0]["id"]) else {}
        return topics
    @property
    def store(self):
        return self.ctx.store

    def active(self) -> list[dict]:
        with self._lock:
            if self._active is None:
                res = self.ctx.service.search_engine.search("Subscription", [("status", "active"), ("_count", "1000")])
                self._active = res.matches
            return list(self._active)

    def invalidate(self) -> None:
        with self._lock:
            self._active = None

    # ---------------- store listener ----------------

    def on_write(self, ev: WriteEvent) -> None:
        rtype = ev.resource.get("resourceType")
        if rtype == "Basic":
            with self._lock:
                self._topics = None
        if rtype == "Subscription":
            self.invalidate()
            if ev.action != "delete" and ev.origin != "subscription-engine":
                self._activate(ev.resource)
            return
        if rtype in SKIP_TYPES or ev.action == "delete":
            return
        for sub in self.active():
            if bp.is_backport(sub):
                self._topic_event(sub, ev)
                continue
            crit_type, params = parse_criteria(sub.get("criteria", ""))
            if crit_type != rtype:
                continue
            end = sub.get("end")
            if end and end < instant():
                self._set_status(sub, "off", None)
                continue
            try:
                if not self.ctx.service.search_engine.matches(ev.resource, params):
                    continue
            except Exception as e:
                log.warning("criteria evaluation failed for Subscription/%s: %s", sub["id"], e)
                continue
            if self.sync:
                self.deliver(sub, ev.resource)
            else:
                self.pool.submit(self.deliver, sub, ev.resource)

    def _activate(self, sub: dict) -> None:
        channel = sub.get("channel", {})
        status, error = "active", None
        if sub.get("status") in ("off", "error"):
            return
        if bp.is_backport(sub):
            return self._activate_backport(sub)
        if channel.get("type") != "rest-hook":
            status, error = "error", f"Channel type '{channel.get('type')}' not supported (rest-hook only)"
        elif not channel.get("endpoint", "").startswith(("http://", "https://")):
            status, error = "error", "channel.endpoint must be an http(s) URL"
        else:
            crit_type, params = parse_criteria(sub.get("criteria", ""))
            if not crit_type:
                status, error = "error", "criteria must be of the form Type?params"
            else:
                try:
                    self.ctx.service.search_engine.build_where(crit_type, params, True, [], [])
                except Exception as e:
                    status, error = "error", f"Unsupported criteria: {e}"
        if sub.get("status") != status or error:
            self._set_status(sub, status, error)

    # ---------------- backport: activation, events, heartbeats ----------------

    def _activate_backport(self, sub: dict) -> None:
        channel = sub.get("channel", {})
        topic = self.topics().get(sub.get("criteria"))
        error = None
        if channel.get("type") != "rest-hook":
            error = f"Channel type '{channel.get('type')}' not supported (rest-hook only)"
        elif not channel.get("endpoint", "").startswith(("http://", "https://")):
            error = "channel.endpoint must be an http(s) URL"
        elif topic is None:
            error = f"Unknown SubscriptionTopic '{sub.get('criteria')}' (see Basic?code=SubscriptionTopic)"
        else:
            for f in bp.filters_of(sub):
                ftype, params = parse_criteria(f) if "?" in f else (topic.triggers[0].resource, parse_criteria("x?" + f)[1])
                if not topic.trigger_for(ftype):
                    error = f"Filter '{f}' is for {ftype}, which topic {topic.url} does not cover"
                    break
                unknown = [k for k, _ in params if k.split(":")[0] not in topic.filters and k.split(":")[0] != "_id"]
                if unknown:
                    error = f"Filter parameter(s) {unknown} not allowed by topic (canFilterBy: {list(topic.filters)})"
                    break
                try:
                    self.ctx.service.search_engine.build_where(ftype, params, True, [], [])
                except Exception as e:
                    error = f"Unsupported filter '{f}': {e}"
                    break
        if error:
            self._set_status(sub, "error", error)
            return
        # R4 backport: the server sends a handshake; the subscription becomes active only if it is accepted
        if self.sync:
            self._handshake(sub)
        else:
            self.pool.submit(self._handshake, sub)

    def _handshake(self, sub: dict) -> None:
        bundle = bp.notification_bundle(sub, self.ctx.settings.base_url.rstrip("/"), "handshake",
                                        self._events_count(sub["id"]), status="requested")
        ok = self._post(sub, bundle, "handshake")
        self._set_status(sub, "active" if ok else "error", None if ok else "Handshake delivery failed")
        if ok:
            self._ensure_heartbeat()

    def _events_count(self, sub_id: str, increment: bool = False) -> int:
        with self._event_lock:
            n = int(self.store.kv_get(f"subevents:{sub_id}") or 0)
            if increment:
                n += 1
                self.store.kv_set(f"subevents:{sub_id}", str(n))
            return n

    def _topic_event(self, sub: dict, ev: WriteEvent) -> None:
        topic = self.topics().get(sub.get("criteria"))
        if not topic:
            return
        res = ev.resource
        trig = topic.trigger_for(res["resourceType"])
        if not trig or not bp.trigger_fires(trig, ev.action, res, ev.previous):
            return
        try:
            if trig.query_current and not self.ctx.service.search_engine.matches(res, parse_criteria("x?" + trig.query_current)[1]):
                return
            for f in bp.filters_of(sub):
                ftype, params = parse_criteria(f) if "?" in f else (res["resourceType"], parse_criteria("x?" + f)[1])
                if ftype == res["resourceType"] and not self.ctx.service.search_engine.matches(res, params):
                    return
        except Exception as e:
            log.warning("filter evaluation failed for Subscription/%s: %s", sub["id"], e)
            return
        number = self._events_count(sub["id"], increment=True)
        context = [r["reference"] for key in ("subject", "patient", "beneficiary", "for")
                   for r in ([res.get(key)] if isinstance(res.get(key), dict) else []) if r.get("reference")]
        content = bp.content_of(sub)
        event = {"number": number, "timestamp": instant()}
        if content != "empty":
            event["focus"] = f"{res['resourceType']}/{res['id']}"
            event["context"] = context
        resources = [res] if content == "full-resource" else []
        bundle = bp.notification_bundle(sub, self.ctx.settings.base_url.rstrip("/"), "event-notification",
                                        number, [event], resources)
        if self.sync:
            self._deliver_event(sub, bundle)
        else:
            self.pool.submit(self._deliver_event, sub, bundle)

    def _deliver_event(self, sub: dict, bundle: dict) -> None:
        if not self._post(sub, bundle, "event-notification"):
            self._set_status(sub, "error", "Event notification delivery failed")

    def _ensure_heartbeat(self) -> None:
        with self._lock:
            if self._heartbeat_started:
                return
            self._heartbeat_started = True
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="sub-heartbeat").start()

    def _heartbeat_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                for sub in self.active():
                    period = bp.channel_int(sub, bp.EXT_HEARTBEAT) if bp.is_backport(sub) else None
                    if period and time.time() - self._last_sent.get(sub["id"], 0) >= period:
                        bundle = bp.notification_bundle(sub, self.ctx.settings.base_url.rstrip("/"), "heartbeat",
                                                        self._events_count(sub["id"]))
                        self._post(sub, bundle, "heartbeat", retries=1)
            except Exception as e:  # never let the heartbeat thread die
                log.warning("heartbeat loop: %s", e)

    def _post(self, sub: dict, bundle: dict, kind: str, retries: int | None = None) -> bool:
        channel = sub.get("channel", {})
        url = channel["endpoint"]
        headers = {"Content-Type": "application/fhir+json; charset=utf-8"}
        for h in channel.get("header", []) or []:
            name, _, value = h.partition(":")
            if name.strip():
                headers[name.strip()] = value.strip()
        content = json.dumps(bundle).encode()
        timeout = bp.channel_int(sub, bp.EXT_TIMEOUT) or 15
        for attempt in range(max(1, retries or self.ctx.settings.subscriptions.retries)):
            start = time.perf_counter()
            try:
                r = httpx.post(url, content=content, headers=headers, timeout=timeout)
                status, text, err = r.status_code, r.text, None if r.status_code < 300 else f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                status, text, err = None, None, repr(e)
            self.store.log_traffic(
                direction="outbound", peer=f"subscriber:Subscription/{sub['id']}", method="POST", url=url,
                req_headers=redact_headers(headers), req_body=body_text(content, self.ctx.settings.max_body_log),
                status=status, resp_body=body_text(text, 20000),
                duration_ms=round((time.perf_counter() - start) * 1000, 1), run_id=active_run(),
                note=f"backport {kind} (attempt {attempt + 1}){' - ' + err if err else ''}")
            if err is None:
                self._last_sent[sub["id"]] = time.time()
                return True
            time.sleep(min(2 ** attempt, 5))
        return False

    def status_bundle(self, subs: list[dict]) -> dict:
        """$status result: searchset of backport SubscriptionStatus (Parameters) resources."""
        base = self.ctx.settings.base_url.rstrip("/")
        entries = []
        for s in subs:
            p = bp.status_parameters(s, base, "query-status", self._events_count(s["id"]))
            entries.append({"fullUrl": f"urn:uuid:{p['id']}", "resource": p, "search": {"mode": "match"}})
        return {"resourceType": "Bundle", "id": new_id(), "type": "searchset", "total": len(entries),
                "entry": entries}

    def _set_status(self, sub: dict, status: str, error: str | None) -> None:
        current = self.store.read("Subscription", sub["id"]) or sub
        new = dict(current, status=status)
        if error:
            new["error"] = error
        else:
            new.pop("error", None)
        self.store.update(new, origin="subscription-engine")
        self.invalidate()

    # ---------------- delivery ----------------

    def deliver(self, sub: dict, resource: dict) -> bool:
        channel = sub.get("channel", {})
        endpoint = channel["endpoint"].rstrip("/")
        headers = {}
        for h in channel.get("header", []) or []:
            name, _, value = h.partition(":")
            if name.strip():
                headers[name.strip()] = value.strip()
        payload = channel.get("payload")
        method, url, content = "POST", endpoint, b""
        if payload:
            headers["Content-Type"] = f"{payload if 'json' in payload else FHIR_JSON}; charset=utf-8"
            content = json.dumps(resource).encode()
            if self.ctx.settings.subscriptions.notify_method == "put-resource":
                method, url = "PUT", f"{endpoint}/{resource['resourceType']}/{resource['id']}"
        retries = max(1, self.ctx.settings.subscriptions.retries)
        last_err = None
        for attempt in range(retries):
            start = time.perf_counter()
            try:
                r = httpx.request(method, url, content=content, headers=headers, timeout=15)
                status, text = r.status_code, r.text
                last_err = None if r.status_code < 300 else f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                status, text, last_err = None, None, repr(e)
            self.store.log_traffic(
                direction="outbound", peer=f"subscriber:Subscription/{sub['id']}", method=method, url=url,
                req_headers=redact_headers(headers), req_body=body_text(content, self.ctx.settings.max_body_log),
                status=status, resp_body=body_text(text, 20000),
                duration_ms=round((time.perf_counter() - start) * 1000, 1), run_id=active_run(),
                note=f"rest-hook notification for {resource['resourceType']}/{resource['id']} "
                     f"(attempt {attempt + 1}){' - ' + last_err if last_err else ''}")
            if last_err is None:
                return True
            time.sleep(min(2 ** attempt, 5))
        self._set_status(sub, "error", f"Delivery to {url} failed: {last_err}")
        return False


# ---------------- remote subscriptions (peer notifies us) ----------------

def register_remote(ctx, peer_name: str) -> list[dict]:
    peer = ctx.settings.peer(peer_name)
    client = ctx.peer_client(peer_name)
    out = []
    for spec in peer.subscribe:
        key = secrets.token_hex(6)
        endpoint = f"{ctx.settings.root_url}/hooks/{peer_name}/{key}"
        sub = {
            "resourceType": "Subscription",
            "status": "requested",
            "reason": spec.reason,
            "criteria": spec.criteria,
            "channel": {"type": "rest-hook", "endpoint": endpoint,
                        "header": [f"Authorization: Bearer {ctx.settings.subscriptions.hook_token}"]},
        }
        if spec.payload:
            sub["channel"]["payload"] = spec.payload
        r = client.create(sub)
        ctx.store.kv_set(f"hook:{peer_name}:{key}", json.dumps({"criteria": spec.criteria, "since": instant()}))
        created = r.resource if r.ok else None
        out.append({"criteria": spec.criteria, "endpoint": endpoint, "status": r.status,
                    "subscription": created, "error": None if r.ok else r.outcome_text()})
    return out


def _resources_from(body: dict | None) -> list[dict]:
    if not isinstance(body, dict):
        return []
    if body.get("resourceType") == "Bundle":
        return [e["resource"] for e in body.get("entry", []) if isinstance(e.get("resource"), dict)
                and e["resource"].get("resourceType") not in ("SubscriptionStatus", "Parameters")]
    return [body]


def build_hook_router(get_ctx) -> APIRouter:
    router = APIRouter()

    @router.api_route("/hooks/{peer}/{key}", methods=["POST", "PUT"])
    @router.api_route("/hooks/{peer}/{key}/{rtype}/{rid}", methods=["POST", "PUT"])
    async def hook(peer: str, key: str, request: Request, rtype: str | None = None, rid: str | None = None):
        ctx = get_ctx()
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            body = None
        headers = dict(request.headers)
        auth_ok = headers.get("authorization", "") == f"Bearer {ctx.settings.subscriptions.hook_token}"
        tid = request.scope.get("state", {}).get("traffic_id")
        path = request.url.path
        note = bp.parse_notification(body)
        if note is not None:  # topic-based (backport) notification
            n = _record_backport(ctx, peer, key, request.method, path, redact_headers(headers), tid, note)
            request.scope.setdefault("state", {})["traffic_note"] = (
                f"backport {note['type']} from {peer} ({n} event(s)); hook token "
                f"{'valid' if auth_ok else 'missing/invalid'}")
            return Response(status_code=200, content=json.dumps(operation_outcome(text="Notification received")),
                            media_type=FHIR_JSON)
        resources = _resources_from(body)
        if not resources:
            ctx.store.log_notification(peer=peer, hook=key, method=request.method, path=path,
                                       headers=redact_headers(headers), body=None, traffic_id=tid,
                                       resource_type=rtype, resource_id=rid)
            if _peer_cfg(ctx, peer) and _peer_cfg(ctx, peer).fetch_on_ping:
                threading.Thread(target=_fetch_on_ping, args=(ctx, peer, key), daemon=True).start()
        for res in resources:
            ctx.store.log_notification(peer=peer, hook=key, method=request.method, path=path,
                                       headers=redact_headers(headers), body=json.dumps(res),
                                       resource_type=res.get("resourceType"), resource_id=res.get("id"),
                                       traffic_id=tid)
            p = _peer_cfg(ctx, peer)
            if p and p.mirror_notifications and res.get("id"):
                try:
                    ctx.store.update(res, origin="peer")
                except Exception as e:
                    log.warning("mirror failed: %s", e)
        request.scope.setdefault("state", {})["traffic_note"] = (
            f"notification from {peer} ({len(resources)} resource(s)); hook token "
            f"{'valid' if auth_ok else 'missing/invalid'}")
        return Response(status_code=200, content=json.dumps(operation_outcome(text="Notification received")),
                        media_type=FHIR_JSON)

    return router


def _peer_cfg(ctx, peer: str):
    try:
        return ctx.settings.peer(peer)
    except KeyError:
        return None


def _record_backport(ctx, peer: str, key: str, method: str, path: str, headers: dict, tid, note: dict) -> int:
    from ..fhir.common import parse_reference
    common = dict(peer=peer, hook=key, method=method, path=path, headers=headers, traffic_id=tid,
                  kind=note["type"], topic=note["topic"], subscription=note["subscription"])
    if note["type"] != "event-notification":  # handshake, heartbeat, query-status ...
        ctx.store.log_notification(body=None, **common)
        return 0
    by_ref = {(r.get("resourceType"), r.get("id")): r for r in note["resources"]}
    events = note["events"] or [{}]
    to_fetch = []
    for ev in events:
        t, i, _ = parse_reference(ev.get("focus") or "")
        res = by_ref.get((t, i))
        if res is None and t and (_peer_cfg(ctx, peer) or None) is not None and _peer_cfg(ctx, peer).fetch_on_ping:
            to_fetch.append((t, i))
            continue
        ctx.store.log_notification(body=json.dumps(res) if res else None, resource_type=t, resource_id=i, **common)
    if to_fetch:  # id-only payload: read the focus resources from the peer
        threading.Thread(target=_fetch_focus, args=(ctx, peer, to_fetch, common), daemon=True).start()
    return len(note["events"])


def _fetch_focus(ctx, peer: str, refs: list[tuple[str, str]], common: dict) -> None:
    client = ctx.peer_client(peer)
    for t, i in refs:
        try:
            r = client.read(t, i)
            res = r.resource if r.ok else None
        except Exception as e:
            log.warning("focus fetch %s/%s from %s failed: %s", t, i, peer, e)
            res = None
        ctx.store.log_notification(body=json.dumps(res) if res else None, resource_type=t, resource_id=i,
                                   **{**common, "method": "FETCH"})


def _fetch_on_ping(ctx, peer: str, key: str) -> None:
    """Empty (id-less) notification: query the peer for what changed since the last fetch."""
    raw = ctx.store.kv_get(f"hook:{peer}:{key}")
    if not raw:
        return
    info = json.loads(raw)
    rtype, params = parse_criteria(info["criteria"])
    since = info.get("since")
    now = instant()
    try:
        client = ctx.peer_client(peer)
        found = client.search_all(rtype, [*params, ("_lastUpdated", f"ge{since}")] if since else params, max_pages=5)
    except Exception as e:
        log.warning("fetch-on-ping from %s failed: %s", peer, e)
        return
    for res in found:
        ctx.store.log_notification(peer=peer, hook=key, method="FETCH", path=f"{rtype}?(criteria)",
                                   headers=None, body=json.dumps(res), resource_type=res.get("resourceType"),
                                   resource_id=res.get("id"))
    info["since"] = now
    ctx.store.kv_set(f"hook:{peer}:{key}", json.dumps(info))


def notifications(store, since_id: int = 0, peer: str | None = None, rtype: str | None = None,
                  limit: int = 200, kind: str | None = None) -> list[dict]:
    where, args = ["id > ?"], [since_id]
    if kind:
        where.append("kind = ?"); args.append(kind)
    if peer:
        where.append("peer = ?"); args.append(peer)
    if rtype:
        where.append("resource_type = ?"); args.append(rtype)
    rows = store.query(f"SELECT * FROM notifications WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                       [*args, limit])
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["resource"] = json.loads(d["body"]) if d["body"] else None
        except ValueError:
            d["resource"] = None
        out.append(d)
    return out



def status_op(svc, rtype, rid, params, body, opctx):
    """Subscription/$status (instance) or Subscription/$status?id=a,b (type level), backport SubscriptionStatus."""
    from ..fhir.service import Result
    engine = svc.ctx_ref.subscriptions
    if rid:
        sub = svc.store.read("Subscription", rid)
        if sub is None:
            raise FhirError(404, f"Subscription/{rid} not found", "not-found")
        subs = [sub]
    else:
        ids = [i for k, v in params if k == "id" for i in v.split(",")]
        subs = svc.store.load_many([("Subscription", i) for i in ids]) if ids else \
            svc.search_engine.search("Subscription", [("_count", "1000")]).matches
    return Result(200, engine.status_bundle(subs))
