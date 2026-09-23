"""Outbound auth providers used when calling a peer: none, basic, static bearer, SMART Backend Services."""
from __future__ import annotations

import base64
import threading
import time
import uuid

import httpx
import jwt

from ..config import Peer
from .keys import key_alg, load_private_key, public_jwk


class AuthError(Exception):
    pass


class AuthProvider:
    def headers(self) -> dict[str, str]:
        return {}

    def invalidate(self) -> None:
        pass

    def describe(self) -> str:
        return "none"


class BasicAuth(AuthProvider):
    def __init__(self, user: str, password: str):
        self.value = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def headers(self):
        return {"Authorization": self.value}

    def describe(self):
        return "basic"


class BearerAuth(AuthProvider):
    def __init__(self, token: str):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def describe(self):
        return "bearer (static)"


class SmartBackendAuth(AuthProvider):
    """client_credentials grant with a private_key_jwt client assertion (SMART Backend Services)."""

    def __init__(self, peer: Peer, log=None):
        a = peer.auth
        if not a.client_id or not a.private_key_path:
            raise AuthError("SMART auth requires client_id and private_key_path")
        self.peer = peer
        self.cfg = a
        self.log = log  # callable(request, response, elapsed_ms, note) for the traffic log
        self._token: str | None = None
        self._exp = 0.0
        self._token_url = a.token_url
        self._lock = threading.Lock()
        self.last_token_response: dict | None = None

    def describe(self):
        return f"SMART backend ({self.cfg.client_id})"

    def token_url(self, client: httpx.Client) -> str:
        if self._token_url:
            return self._token_url
        base = self.peer.base_url.rstrip("/")
        try:
            r = client.get(f"{base}/.well-known/smart-configuration", headers={"Accept": "application/json"})
            if self.log:
                self.log(r, note="SMART discovery")
            if r.status_code == 200 and r.json().get("token_endpoint"):
                self._token_url = r.json()["token_endpoint"]
                return self._token_url
        except (httpx.HTTPError, ValueError):
            pass
        r = client.get(f"{base}/metadata", headers={"Accept": "application/fhir+json"})
        for rest in r.json().get("rest", []):
            for ext in rest.get("security", {}).get("extension", []):
                if ext.get("url", "").endswith("oauth-uris"):
                    for sub in ext.get("extension", []):
                        if sub.get("url") == "token":
                            self._token_url = sub["valueUri"]
                            return self._token_url
        raise AuthError(f"Could not discover token endpoint for {base}")

    def assertion(self, token_url: str) -> str:
        key = load_private_key(self.cfg.private_key_path)
        alg = self.cfg.alg or key_alg(key)
        kid = self.cfg.kid or public_jwk(key, alg)["kid"]
        now = int(time.time())
        claims = {"iss": self.cfg.client_id, "sub": self.cfg.client_id, "aud": token_url,
                  "exp": now + 300, "iat": now, "jti": str(uuid.uuid4())}
        headers = {"kid": kid, "typ": "JWT"}
        if self.cfg.jku:
            headers["jku"] = self.cfg.jku
        return jwt.encode(claims, key, algorithm=alg, headers=headers)

    def fetch_token(self) -> str:
        with httpx.Client(timeout=self.peer.timeout, verify=self.peer.verify_tls) as client:
            url = self.token_url(client)
            data = {
                "grant_type": "client_credentials",
                "scope": self.cfg.scope,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self.assertion(url),
            }
            r = client.post(url, data=data, headers={"Accept": "application/json"})
            if self.log:
                self.log(r, note="SMART token request")
        if r.status_code != 200:
            raise AuthError(f"Token request failed: HTTP {r.status_code} {r.text[:500]}")
        body = r.json()
        self.last_token_response = {k: v for k, v in body.items() if k != "access_token"}
        self._token = body["access_token"]
        self._exp = time.time() + int(body.get("expires_in", 300)) - 30
        return self._token

    def headers(self):
        with self._lock:
            if not self._token or time.time() >= self._exp:
                self.fetch_token()
            return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self):
        with self._lock:
            self._token = None


def provider_for(peer: Peer, log=None) -> AuthProvider:
    a = peer.auth
    if a.type == "basic":
        return BasicAuth(a.username or "", a.password or "")
    if a.type == "bearer":
        return BearerAuth(a.token or "")
    if a.type == "smart":
        return SmartBackendAuth(peer, log)
    return AuthProvider()
