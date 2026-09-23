"""Inbound auth: guard for /fhir (none/basic/bearer/SMART) and a SMART Backend Services token endpoint."""
from __future__ import annotations

import base64
import hmac
import secrets
import threading
import time
from pathlib import Path

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..fhir.common import R4_RESOURCE_TYPES, FhirError
from .keys import jwk_to_key, load_private_key, load_public_key, public_jwk

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ALGS = ["RS384", "ES384", "RS256", "ES256"]
PERM = {"GET": "rs", "HEAD": "rs", "POST": "c", "PUT": "u", "PATCH": "u", "DELETE": "d"}
V1 = {"read": "rs", "write": "cud", "*": "cruds"}

_jti_seen: dict[str, float] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def server_secret(store) -> str:
    s = store.kv_get("jwt_secret")
    if not s:
        s = secrets.token_urlsafe(48)
        store.kv_set("jwt_secret", s)
    return s


def scope_allows(scopes: list[str], rtype: str, perm: str) -> bool:
    """SMART v1 (system/Patient.read) and v2 (system/Patient.rs) scopes."""
    for sc in scopes:
        ctx, _, rest = sc.partition("/")
        if ctx not in ("system", "user", "patient") or "." not in rest:
            continue
        t, _, p = rest.partition(".")
        p = p.split("?", 1)[0]
        granted = V1.get(p, p)
        if t in ("*", rtype) and all(ch in granted for ch in perm):
            return True
    return False


def _required(request: Request) -> tuple[str, str]:
    path = request.url.path.split("/fhir", 1)[-1].strip("/")
    first = path.split("/")[0] if path else ""
    rtype = first if first in R4_RESOURCE_TYPES else "*"
    perm = PERM.get(request.method, "r")
    if request.method == "POST" and (path.endswith("_search") or "$" in path):
        perm = "s" if path.endswith("_search") else "c"
        if "$validate" in path or "$everything" in path:
            perm = "r"
    return rtype, perm


def check_request(request: Request, ctx) -> None:
    """Raise FhirError(401/403) if the request is not authorised by any configured scheme."""
    cfg = ctx.settings.server_auth
    if "none" in cfg.types:
        return
    header = request.headers.get("authorization", "")
    scheme, _, cred = header.partition(" ")
    scheme = scheme.lower()
    challenge = {"WWW-Authenticate": 'Bearer realm="sekmet"' if "basic" not in cfg.types else 'Basic realm="sekmet"'}
    if not header:
        raise FhirError(401, "Authentication required", "login", headers=challenge)
    if scheme == "basic" and "basic" in cfg.types:
        try:
            user, _, pw = base64.b64decode(cred).decode().partition(":")
        except Exception:
            raise FhirError(401, "Malformed Basic credentials", "login", headers=challenge)
        if user in cfg.users and hmac.compare_digest(cfg.users[user], pw):
            request.state.principal = f"basic:{user}"
            return
        raise FhirError(401, "Invalid Basic credentials", "login", headers=challenge)
    if scheme == "bearer":
        if "bearer" in cfg.types and any(hmac.compare_digest(cred, t) for t in cfg.tokens):
            request.state.principal = "bearer:static"
            return
        if "smart" in cfg.types:
            try:
                claims = jwt.decode(cred, server_secret(ctx.store), algorithms=["HS256"], audience=ctx.settings.base_url)
            except jwt.ExpiredSignatureError:
                raise FhirError(401, "Access token expired", "expired", headers=challenge)
            except jwt.PyJWTError as e:
                raise FhirError(401, f"Invalid access token: {e}", "login", headers=challenge)
            request.state.principal = f"smart:{claims.get('client_id')}"
            if cfg.enforce_scopes:
                rtype, perm = _required(request)
                scopes = claims.get("scope", "").split()
                if not scope_allows(scopes, rtype, perm):
                    raise FhirError(403, f"Token scopes {scopes} do not permit '{perm}' on {rtype}", "forbidden")
            return
    raise FhirError(401, f"Unsupported or invalid Authorization scheme '{scheme}'", "login", headers=challenge)


# ---------------- SMART token endpoint ----------------

def _client_keys(client) -> list:
    if client.jwks:
        return [jwk_to_key(k) for k in client.jwks.get("keys", [])]
    if client.public_key_path:
        return [load_public_key(client.public_key_path)]
    if client.jwks_url:
        with _lock:
            cached = _jwks_cache.get(client.jwks_url)
        if not cached or cached[0] < time.time():
            doc = httpx.get(client.jwks_url, timeout=10).json()
            cached = (time.time() + 300, doc)
            with _lock:
                _jwks_cache[client.jwks_url] = cached
        return [jwk_to_key(k) for k in cached[1].get("keys", [])]
    return []


def _oauth_error(status: int, error: str, desc: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def build_router(get_ctx) -> APIRouter:
    router = APIRouter()

    def smart_config():
        s = get_ctx().settings
        return {
            "issuer": s.root_url,
            "token_endpoint": f"{s.root_url}/auth/token",
            "token_endpoint_auth_methods_supported": ["private_key_jwt", "client_secret_basic", "client_secret_post"],
            "token_endpoint_auth_signing_alg_values_supported": ALGS,
            "grant_types_supported": ["client_credentials"],
            "scopes_supported": ["system/*.read", "system/*.write", "system/*.*", "system/*.rs", "system/*.cruds"],
            "capabilities": ["client-confidential-asymmetric", "client-confidential-symmetric", "permission-v1",
                             "permission-v2"],
            "jwks_uri": f"{s.root_url}/.well-known/jwks.json",
        }

    router.add_api_route("/fhir/.well-known/smart-configuration", smart_config, methods=["GET"])
    router.add_api_route("/.well-known/smart-configuration", smart_config, methods=["GET"])

    @router.get("/.well-known/jwks.json")
    def jwks():
        """Public key(s) this tester signs outbound SMART client assertions with (register at the peer)."""
        s = get_ctx().settings
        keys = []
        paths = {s.own_private_key_path, *(p.auth.private_key_path for p in s.peers.values() if p.auth.private_key_path)}
        for p in paths:
            if p and Path(p).exists():
                jwk = public_jwk(load_private_key(p))
                if jwk not in keys:
                    keys.append(jwk)
        return {"keys": keys}

    @router.post("/auth/token")
    async def token(request: Request):
        ctx = get_ctx()
        cfg = ctx.settings.server_auth
        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            return _oauth_error(400, "unsupported_grant_type", "Only client_credentials is supported")
        client = None
        auth = request.headers.get("authorization", "")
        if form.get("client_assertion"):
            if form.get("client_assertion_type") != ASSERTION_TYPE:
                return _oauth_error(400, "invalid_request", "client_assertion_type must be jwt-bearer")
            assertion = form["client_assertion"]
            try:
                unverified = jwt.decode(assertion, options={"verify_signature": False})
            except jwt.PyJWTError as e:
                return _oauth_error(400, "invalid_request", f"Malformed client_assertion: {e}")
            client = next((c for c in cfg.smart_clients if c.client_id == unverified.get("iss")), None)
            if client is None:
                return _oauth_error(401, "invalid_client", f"Unknown client_id '{unverified.get('iss')}'")
            token_url = f"{ctx.settings.root_url}/auth/token"
            try:
                keys = _client_keys(client)
            except Exception as e:
                return _oauth_error(401, "invalid_client", f"Could not load client keys: {e}")
            claims, last_err = None, "no keys registered"
            for key in keys:
                try:
                    claims = jwt.decode(assertion, key, algorithms=ALGS, audience=token_url,
                                        options={"require": ["exp", "iss", "sub", "aud", "jti"]})
                    break
                except jwt.PyJWTError as e:
                    last_err = str(e)
            if claims is None:
                return _oauth_error(401, "invalid_client", f"client_assertion verification failed: {last_err}")
            if claims["iss"] != claims["sub"]:
                return _oauth_error(401, "invalid_client", "iss and sub must both equal client_id")
            if claims["exp"] > time.time() + 300:
                return _oauth_error(401, "invalid_client", "exp must be no more than 5 minutes in the future")
            with _lock:
                for k, v in list(_jti_seen.items()):
                    if v < time.time():
                        _jti_seen.pop(k, None)
                if claims["jti"] in _jti_seen:
                    return _oauth_error(401, "invalid_client", "jti has already been used")
                _jti_seen[claims["jti"]] = claims["exp"]
        elif auth.lower().startswith("basic ") or form.get("client_secret"):
            if auth.lower().startswith("basic "):
                cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            else:
                cid, secret = form.get("client_id", ""), form.get("client_secret", "")
            client = next((c for c in cfg.smart_clients if c.client_id == cid), None)
            if not client or not client.client_secret or not hmac.compare_digest(client.client_secret, secret):
                return _oauth_error(401, "invalid_client", "Invalid client credentials")
        else:
            return _oauth_error(400, "invalid_request", "client_assertion or client credentials required")
        requested = (form.get("scope") or client.scopes).split()
        allowed = client.scopes.split()
        granted = [s for s in requested if any(_scope_covers(a, s) for a in allowed)]
        if not granted:
            return _oauth_error(400, "invalid_scope", f"None of the requested scopes are allowed: {requested}")
        now = int(time.time())
        access = jwt.encode({"iss": ctx.settings.root_url, "aud": ctx.settings.base_url, "sub": client.client_id,
                             "client_id": client.client_id, "scope": " ".join(granted), "iat": now,
                             "exp": now + cfg.token_lifetime, "jti": secrets.token_hex(8)},
                            server_secret(ctx.store), algorithm="HS256")
        return JSONResponse({"access_token": access, "token_type": "bearer", "expires_in": cfg.token_lifetime,
                             "scope": " ".join(granted)}, headers={"Cache-Control": "no-store"})

    return router


def _scope_covers(allowed: str, requested: str) -> bool:
    if allowed == requested:
        return True
    actx, _, arest = allowed.partition("/")
    rctx, _, rrest = requested.partition("/")
    if actx != rctx or "." not in arest or "." not in rrest:
        return False
    at, _, ap = arest.partition(".")
    rt, _, rp = rrest.partition(".")
    return at in ("*", rt) and all(ch in V1.get(ap, ap) for ch in V1.get(rp, rp))
