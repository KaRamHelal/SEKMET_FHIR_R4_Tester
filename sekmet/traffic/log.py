"""Traffic log: records every inbound request (ASGI middleware) and outbound call (PeerClient).

The active scenario run id is process-global so inbound calls made by a peer during a run are
attributed to it as well.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

REDACT = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
LOGGED_PREFIXES = ("/fhir", "/auth", "/hooks", "/.well-known")

_active_run: str | None = None


def set_active_run(run_id: str | None) -> None:
    global _active_run
    _active_run = run_id


def active_run() -> str | None:
    return _active_run


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in REDACT:
            scheme = v.split(" ", 1)[0] if " " in v else ""
            out[k] = f"{scheme} ***redacted***".strip()
        else:
            out[k] = v
    return out


def body_text(body: bytes | str | None, limit: int) -> str | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if len(body) > limit:
        return body[:limit] + f"\n...[truncated {len(body) - limit} chars]"
    return body


class TrafficMiddleware:
    """Pure ASGI middleware capturing request/response bodies for logged paths."""

    def __init__(self, app, get_ctx):
        self.app = app
        self.get_ctx = get_ctx

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(LOGGED_PREFIXES):
            return await self.app(scope, receive, send)
        ctx = self.get_ctx()
        start = time.perf_counter()
        req_chunks: list[bytes] = []
        resp: dict[str, Any] = {"status": None, "headers": [], "body": []}
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        corr = headers.get("x-request-id") or headers.get("x-correlation-id") or str(uuid.uuid4())

        async def recv():
            msg = await receive()
            if msg["type"] == "http.request":
                req_chunks.append(msg.get("body", b""))
            return msg

        async def snd(msg):
            if msg["type"] == "http.response.start":
                resp["status"] = msg["status"]
                msg.setdefault("headers", [])
                msg["headers"] = [*msg["headers"], (b"x-request-id", corr.encode())]
                resp["headers"] = msg["headers"]
            elif msg["type"] == "http.response.body":
                resp["body"].append(msg.get("body", b""))
            await send(msg)

        scope.setdefault("state", {})["correlation_id"] = corr
        try:
            await self.app(scope, recv, snd)
        finally:
            qs = scope.get("query_string", b"").decode()
            url = scope["path"] + (f"?{qs}" if qs else "")
            limit = ctx.settings.max_body_log
            try:
                tid = ctx.store.log_traffic(
                    direction="inbound", peer=headers.get("user-agent", "")[:120], method=scope["method"], url=url,
                    req_headers=redact_headers(headers), req_body=body_text(b"".join(req_chunks), limit),
                    status=resp["status"],
                    resp_headers={k.decode("latin-1"): v.decode("latin-1") for k, v in resp["headers"]},
                    resp_body=body_text(b"".join(resp["body"]), limit),
                    duration_ms=round((time.perf_counter() - start) * 1000, 1), correlation_id=corr,
                    run_id=active_run(), note=scope["state"].get("traffic_note"))
                scope["state"]["traffic_id"] = tid
            except Exception:  # never fail a request because logging failed
                pass


def traffic_rows(store, limit: int = 100, offset: int = 0, direction: str | None = None, run_id: str | None = None,
                 status: str | None = None, q: str | None = None) -> list[dict]:
    where, args = [], []
    if direction:
        where.append("direction=?"); args.append(direction)
    if run_id:
        where.append("run_id=?"); args.append(run_id)
    if status == "error":
        where.append("(status IS NULL OR status>=400)")
    elif status == "ok":
        where.append("status<400")
    if q:
        where.append("(url LIKE ? OR peer LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = store.query(f"SELECT id, ts, direction, peer, method, url, status, duration_ms, run_id, note FROM traffic "
                       f"{w} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset])
    return [dict(r) for r in rows]


def traffic_detail(store, tid: int) -> dict | None:
    rows = store.query("SELECT * FROM traffic WHERE id=?", (tid,))
    if not rows:
        return None
    d = dict(rows[0])
    for k in ("req_headers", "resp_headers"):
        try:
            d[k] = json.loads(d[k]) if d[k] else {}
        except (TypeError, json.JSONDecodeError):
            pass
    return d
