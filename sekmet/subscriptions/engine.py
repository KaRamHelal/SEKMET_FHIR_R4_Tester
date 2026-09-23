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

from ..fhir.common import FHIR_JSON, instant, operation_outcome
from ..fhir.search import parse_criteria
from ..fhir.store import WriteEvent
from ..traffic.log import active_run, body_text, redact_headers

log = logging.getLogger("sekmet.subscriptions")
SKIP_TYPES = {"Subscription", "Bundle", "MessageHeader", "AuditEvent", "OperationOutcome"}


class SubscriptionEngine:
    def __init__(self, ctx):
        self.ctx = ctx
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sub-notify")
        self._active: list[dict] | None = None
        self._lock = threading.Lock()
        self.sync = False  # tests can deliver synchronously

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
        if rtype == "Subscription":
            self.invalidate()
            if ev.action != "delete" and ev.origin != "subscription-engine":
                self._activate(ev.resource)
            return
        if rtype in SKIP_TYPES or ev.action == "delete":
            return
        for sub in self.active():
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
        resources = _resources_from(body)
        path = request.url.path
        if not resources:
            ctx.store.log_notification(peer=peer, hook=key, method=request.method, path=path,
                                       headers=redact_headers(headers), body=None, traffic_id=tid,
                                       resource_type=rtype, resource_id=rid)
            if ctx.settings.peers.get(peer) and ctx.settings.peers[peer].fetch_on_ping:
                threading.Thread(target=_fetch_on_ping, args=(ctx, peer, key), daemon=True).start()
        for res in resources:
            ctx.store.log_notification(peer=peer, hook=key, method=request.method, path=path,
                                       headers=redact_headers(headers), body=json.dumps(res),
                                       resource_type=res.get("resourceType"), resource_id=res.get("id"),
                                       traffic_id=tid)
            p = ctx.settings.peers.get(peer)
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
                  limit: int = 200) -> list[dict]:
    where, args = ["id > ?"], [since_id]
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

