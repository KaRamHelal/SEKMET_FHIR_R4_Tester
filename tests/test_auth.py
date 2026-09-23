import base64
import time

import jwt
import pytest

from sekmet.auth.keys import generate_private_key, public_jwk, save_private_key
from sekmet.auth.server import scope_allows
from sekmet.client.peer import PeerClient
from sekmet.config import Peer, PeerAuth

from .conftest import free_port, make_settings, start_server


def test_scope_matching():
    assert scope_allows(["system/*.read"], "Patient", "rs")
    assert not scope_allows(["system/*.read"], "Patient", "c")
    assert scope_allows(["system/Patient.cruds"], "Patient", "u")
    assert not scope_allows(["system/Patient.rs"], "Observation", "r")
    assert scope_allows(["system/*.*"], "*", "c")


def test_basic_and_bearer(tmp_path):
    from fastapi.testclient import TestClient
    from sekmet.main import create_app
    s = make_settings(tmp_path, server_auth={"types": ["basic", "bearer"], "users": {"u": "p"}, "tokens": ["tok"]})
    c = TestClient(create_app(s))
    assert c.get("/fhir/metadata").status_code == 200  # always open
    assert c.get("/fhir/Patient").status_code == 401
    good = base64.b64encode(b"u:p").decode()
    assert c.get("/fhir/Patient", headers={"Authorization": f"Basic {good}"}).status_code == 200
    bad = base64.b64encode(b"u:x").decode()
    assert c.get("/fhir/Patient", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert c.get("/fhir/Patient", headers={"Authorization": "Bearer tok"}).status_code == 200


@pytest.fixture(scope="module")
def smart_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("smart")
    key = generate_private_key("RS384")
    key_path = str(tmp / "client.pem")
    save_private_key(key, key_path)
    port = free_port()
    s = make_settings(tmp, port, server_auth={
        "types": ["smart"],
        "smart_clients": [
            {"client_id": "writer", "jwks": {"keys": [public_jwk(key)]}, "scopes": "system/*.read system/*.write"},
            {"client_id": "reader", "jwks": {"keys": [public_jwk(key)]}, "scopes": "system/*.read"},
        ]})
    app, server = start_server(s)
    yield s, key_path, key
    server.should_exit = True


def test_smart_backend_services_end_to_end(smart_server):
    s, key_path, _ = smart_server
    peer = Peer(base_url=s.base_url, auth=PeerAuth(type="smart", client_id="writer", private_key_path=key_path))
    client = PeerClient("smart", peer)
    r = client.create({"resourceType": "Patient", "name": [{"family": "Smart"}]})
    assert r.status == 201, r.text
    assert client.auth.last_token_response["scope"] == "system/*.read system/*.write"
    assert client.read("Patient", r.resource["id"]).ok


def test_smart_scope_enforced(smart_server):
    s, key_path, _ = smart_server
    peer = Peer(base_url=s.base_url, auth=PeerAuth(type="smart", client_id="reader", private_key_path=key_path,
                                                   scope="system/*.read"))
    client = PeerClient("reader", peer)
    assert client.search("Patient").ok
    assert client.create({"resourceType": "Patient"}).status == 403


def test_smart_token_endpoint_rejections(smart_server):
    import httpx
    s, _, key = smart_server
    token_url = f"{s.root_url}/auth/token"

    def assertion(**over):
        claims = {"iss": "writer", "sub": "writer", "aud": token_url, "exp": int(time.time()) + 60, "jti": str(time.time()), **over}
        return jwt.encode(claims, key, algorithm="RS384", headers={"kid": public_jwk(key)["kid"]})

    def post(a, **extra):
        return httpx.post(token_url, data={"grant_type": "client_credentials", "scope": "system/*.read",
                                           "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                                           "client_assertion": a, **extra})

    a = assertion()
    assert post(a).status_code == 200
    assert post(a).json()["error"] == "invalid_client"  # jti replay
    assert post(assertion(aud="http://wrong")).status_code == 401
    assert post(assertion(iss="nobody", sub="nobody")).status_code == 401
    assert post(assertion(exp=int(time.time()) + 3600)).status_code == 401
    other = generate_private_key("RS384")
    forged = jwt.encode({"iss": "writer", "sub": "writer", "aud": token_url, "exp": int(time.time()) + 60, "jti": "z"},
                        other, algorithm="RS384")
    assert post(forged).status_code == 401
    assert httpx.post(token_url, data={"grant_type": "password"}).status_code == 400
    conf = httpx.get(f"{s.base_url}/.well-known/smart-configuration").json()
    assert conf["token_endpoint"] == token_url


def test_client_credentials_secret(tmp_path, monkeypatch):
    port = free_port()
    s = make_settings(tmp_path, port, server_auth={"types": ["smart"], "smart_clients": [
        {"client_id": "svc", "client_secret": "s3cret", "scopes": "system/*.read"}]})
    app, server = start_server(s)
    try:
        monkeypatch.setenv("TEST_SECRET", "s3cret")
        for method in ("client_secret_basic", "client_secret_post"):
            peer = Peer(base_url=s.base_url, auth=PeerAuth(type="client_credentials", client_id="svc",
                                                           client_secret_env="TEST_SECRET", scope="system/*.read",
                                                           client_auth_method=method))
            client = PeerClient("cc", peer, app.state.ctx.store)
            assert client.search("Patient").ok
            assert client.auth.last_token_response["scope"] == "system/*.read"
        rows = app.state.ctx.store.query("SELECT req_headers, req_body FROM traffic WHERE note LIKE '%client_credentials%'")
        assert rows and all("s3cret" not in (r["req_headers"] or "") + (r["req_body"] or "") for r in rows)
        secret_file = tmp_path / "svc.secret"
        secret_file.write_text("s3cret\n")
        peer = Peer(base_url=s.base_url, auth=PeerAuth(type="client_credentials", client_id="svc",
                                                       client_secret_file=str(secret_file), scope="system/*.read"))
        assert PeerClient("file", peer).search("Patient").ok
        bad = Peer(base_url=s.base_url, auth=PeerAuth(type="client_credentials", client_id="svc", client_secret="nope"))
        from sekmet.client.peer import PeerError
        with pytest.raises(PeerError):
            PeerClient("bad", bad).search("Patient")
    finally:
        server.should_exit = True
