"""PeerClient: FHIR R4 REST client for a peer HIS, with auth, traffic logging and helpful response parsing."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..auth.outbound import AuthError, provider_for
from ..config import Peer
from ..fhir.common import FHIR_JSON, FhirError, parse_reference
from ..fhir.xml import FHIR_XML, from_xml, to_xml
from ..traffic.log import active_run, body_text, redact_headers


class PeerError(Exception):
    def __init__(self, message: str, response: "PeerResponse | None" = None):
        super().__init__(message)
        self.response = response


@dataclass
class PeerResponse:
    status: int
    headers: dict[str, str]
    body: Any
    text: str
    elapsed_ms: float
    method: str
    url: str
    traffic_id: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def resource(self) -> dict | None:
        return self.body if isinstance(self.body, dict) else None

    @property
    def location(self) -> tuple[str | None, str | None, str | None]:
        loc = self.headers.get("location") or self.headers.get("content-location")
        return parse_reference(loc) if loc else (None, None, None)

    def outcome_text(self) -> str:
        b = self.resource
        if b and b.get("resourceType") == "OperationOutcome":
            return "; ".join(f"{i.get('severity')}: {i.get('diagnostics') or i.get('details', {}).get('text') or i.get('code')}"
                             for i in b.get("issue", []))
        return self.text[:300]

    def raise_for_status(self) -> "PeerResponse":
        if not self.ok:
            raise PeerError(f"{self.method} {self.url} -> HTTP {self.status}: {self.outcome_text()}", self)
        return self

    def entries(self) -> list[dict]:
        b = self.resource or {}
        return [e["resource"] for e in b.get("entry", []) if "resource" in e and
                e.get("search", {}).get("mode", "match") == "match"]


class PeerClient:
    def __init__(self, name: str, peer: Peer, store=None, max_body_log: int = 2_000_000):
        self.name = name
        self.peer = peer
        self.base = peer.base_url.rstrip("/")
        self.store = store
        self.max_body_log = max_body_log
        self.auth = provider_for(peer, log=self._log_raw)
        self.http = httpx.Client(timeout=peer.timeout, verify=peer.verify_tls, follow_redirects=False)
        self.extra_headers: dict[str, str] = {}  # e.g. loopback simulator control set by the scenario runner

    def close(self):
        self.http.close()

    # ---------------- core ----------------

    def _log_raw(self, r: httpx.Response, note: str | None = None) -> int | None:
        if not self.store:
            return None
        req = r.request
        return self.store.log_traffic(
            direction="outbound", peer=self.name, method=req.method, url=str(req.url),
            req_headers=redact_headers(dict(req.headers)),
            req_body=body_text(_redact_form(req.content), self.max_body_log),
            status=r.status_code, resp_headers=dict(r.headers),
            resp_body=body_text(_redact_token(r.text), self.max_body_log),
            duration_ms=round(r.elapsed.total_seconds() * 1000, 1) if r.elapsed else None,
            correlation_id=req.headers.get("x-request-id"), run_id=active_run(), note=note)

    def request(self, method: str, path: str, *, params: dict | list | None = None, body: Any = None,
                headers: dict[str, str] | None = None, content_type: str = FHIR_JSON, note: str | None = None,
                raw: bytes | None = None, absolute: bool = False) -> PeerResponse:
        url = path if absolute or path.startswith(("http://", "https://")) else f"{self.base}/{path.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        xml = self.peer.format == "xml"
        h = {"Accept": FHIR_XML if xml else FHIR_JSON, "X-Request-Id": str(uuid.uuid4()), **self.peer.headers,
             **self.extra_headers, **(headers or {})}
        content = raw
        if body is not None and raw is None:
            xml_body = None
            if xml and content_type == FHIR_JSON and isinstance(body, dict):
                try:
                    xml_body = to_xml(body)
                except FhirError:  # e.g. a deliberately invalid probe: not expressible through the models
                    note = "; ".join(x for x in (note, "body not expressible as FHIR XML; sent as JSON") if x)
            if xml_body is not None:
                content = xml_body
                h.setdefault("Content-Type", f"{FHIR_XML}; charset=utf-8")
            else:
                content = json.dumps(body).encode()
                h.setdefault("Content-Type", f"{content_type}; charset=utf-8" if "json" in content_type else content_type)
        if method in ("POST", "PUT", "PATCH") and self.peer.prefer_return and "Prefer" not in h:
            h["Prefer"] = f"return={self.peer.prefer_return}"
        for attempt in (1, 2):
            try:
                h.update(self.auth.headers())
            except (AuthError, httpx.HTTPError) as e:
                raise PeerError(f"Auth failed for peer {self.name}: {e}")
            start = time.perf_counter()
            try:
                r = self.http.request(method, url, content=content, headers=h)
            except httpx.HTTPError as e:
                if self.store:
                    self.store.log_traffic(direction="outbound", peer=self.name, method=method, url=url,
                                           req_headers=redact_headers(h),
                                           req_body=body_text(content, self.max_body_log), status=None,
                                           duration_ms=round((time.perf_counter() - start) * 1000, 1),
                                           run_id=active_run(), note=f"transport error: {e!r}")
                raise PeerError(f"{method} {url} failed: {e!r}")
            if r.status_code == 401 and attempt == 1 and self.peer.auth.type in ("smart", "client_credentials"):
                self._log_raw(r, note="401 - refreshing token and retrying")
                self.auth.invalidate()
                continue
            break
        parsed, note_extra = None, None
        ctype = r.headers.get("content-type", "").lower()
        if r.content:
            if "xml" in ctype and "html" not in ctype:
                try:
                    parsed = from_xml(r.content)
                except FhirError as e:
                    note_extra = f"response XML not parseable: {e.message}"
            else:
                try:
                    parsed = r.json()
                except ValueError:
                    parsed = None
        if xml and r.content and "json" in ctype:
            note_extra = "asked for XML (Accept: application/fhir+xml) but the peer answered JSON"
        note = "; ".join(x for x in (note, note_extra) if x) or None
        resp = PeerResponse(r.status_code, {k.lower(): v for k, v in r.headers.items()}, parsed, r.text,
                            round(r.elapsed.total_seconds() * 1000, 1), method, url)
        resp.traffic_id = self._log_raw(r, note=note)
        return resp

    # ---------------- FHIR interactions ----------------

    def capabilities(self) -> PeerResponse:
        return self.request("GET", "metadata")

    def read(self, rtype: str, rid: str, **kw) -> PeerResponse:
        return self.request("GET", f"{rtype}/{rid}", **kw)

    def vread(self, rtype: str, rid: str, vid: str) -> PeerResponse:
        return self.request("GET", f"{rtype}/{rid}/_history/{vid}")

    def search(self, rtype: str, params: dict | list | None = None, post: bool = False) -> PeerResponse:
        if post:
            return self.request("POST", f"{rtype}/_search", raw=urlencode(params or {}, doseq=True).encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self.request("GET", rtype, params=params)

    def search_all(self, rtype: str, params: dict | list | None = None, max_pages: int = 20) -> list[dict]:
        resp = self.search(rtype, params).raise_for_status()
        out = resp.entries()
        for _ in range(max_pages - 1):
            nxt = next((l["url"] for l in (resp.resource or {}).get("link", []) if l.get("relation") == "next"), None)
            if not nxt:
                break
            resp = self.request("GET", nxt, absolute=True).raise_for_status()
            out += resp.entries()
        return out

    def create(self, resource: dict, if_none_exist: str | None = None) -> PeerResponse:
        h = {"If-None-Exist": if_none_exist} if if_none_exist else None
        body = {k: v for k, v in resource.items() if k != "id"}
        return self.request("POST", resource["resourceType"], body=body, headers=h)

    def update(self, resource: dict, if_match: str | None = None) -> PeerResponse:
        h = {"If-Match": if_match} if if_match else None
        return self.request("PUT", f"{resource['resourceType']}/{resource['id']}", body=resource, headers=h)

    def conditional_update(self, resource: dict, query: str) -> PeerResponse:
        body = {k: v for k, v in resource.items() if k != "id"}
        return self.request("PUT", f"{resource['resourceType']}?{query}", body=body)

    def patch(self, rtype: str, rid: str, ops: list[dict]) -> PeerResponse:
        return self.request("PATCH", f"{rtype}/{rid}", body=ops, content_type="application/json-patch+json")

    def delete(self, rtype: str, rid: str) -> PeerResponse:
        return self.request("DELETE", f"{rtype}/{rid}")

    def history(self, rtype: str, rid: str | None = None) -> PeerResponse:
        return self.request("GET", f"{rtype}/{rid}/_history" if rid else f"{rtype}/_history")

    def transaction(self, bundle: dict) -> PeerResponse:
        return self.request("POST", "", body=bundle)

    def operation(self, path: str, body: Any = None, params: dict | None = None, method: str = "POST") -> PeerResponse:
        return self.request(method, path, body=body, params=params)

    def process_message(self, bundle: dict) -> PeerResponse:
        endpoint = self.peer.messaging_endpoint or f"{self.base}/$process-message"
        return self.request("POST", endpoint, body=bundle, absolute=True, note="FHIR message")

    def fetch_resource(self, resp: PeerResponse, rtype: str) -> dict:
        """Get the written resource even if the peer answered with return=minimal / OperationOutcome."""
        if resp.resource and resp.resource.get("resourceType") == rtype:
            return resp.resource
        t, i, v = resp.location
        if t and i:
            return self.read(t, i).raise_for_status().resource
        raise PeerError(f"Peer returned no {rtype} body and no Location header", resp)


def _redact_form(content: bytes | None) -> bytes | None:
    if content and (b"client_assertion=" in content or b"client_secret=" in content):
        return b"&".join(p if not p.startswith((b"client_assertion=", b"client_secret=")) else
                         p.split(b"=")[0] + b"=***redacted***" for p in content.split(b"&"))
    return content


def _redact_token(text: str) -> str:
    if '"access_token"' in text:
        try:
            d = json.loads(text)
            d["access_token"] = "***redacted***"
            return json.dumps(d)
        except ValueError:
            pass
    return text
