"""Settings loaded from a YAML file (SEKMET_CONFIG, default ./settings.yaml) with env overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class PeerAuth(BaseModel):
    type: Literal["none", "basic", "bearer", "smart", "client_credentials"] = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    # SMART Backend Services (client_credentials + private_key_jwt)
    client_id: str | None = None
    private_key_path: str | None = None
    kid: str | None = None
    alg: Literal["RS384", "ES384", "RS256", "ES256"] = "RS384"
    token_url: str | None = None  # discovered from .well-known/smart-configuration when empty
    scope: str = "system/*.read system/*.write"
    jku: str | None = None
    # OAuth2 client_credentials with a shared secret (e.g. Keycloak confidential client)
    client_secret: str | None = None
    client_secret_env: str | None = None  # read the secret from this environment variable instead
    client_auth_method: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic"


class PeerSubscription(BaseModel):
    criteria: str
    payload: str | None = "application/fhir+json"
    reason: str = "SEKMET tester subscription"


class Peer(BaseModel):
    base_url: str
    auth: PeerAuth = Field(default_factory=PeerAuth)
    headers: dict[str, str] = Field(default_factory=dict)
    mode: Literal["rest", "messaging"] = "rest"
    messaging_endpoint: str | None = None  # default: {base_url}/$process-message
    message_destination: str | None = None  # MessageHeader.destination.endpoint, default: base_url
    subscribe: list[PeerSubscription] = Field(default_factory=list)
    fetch_on_ping: bool = True  # empty rest-hook notification -> search peer for changes
    mirror_notifications: bool = False  # store notified resources into the local store
    use_transactions: bool = True
    timeout: float = 30.0
    verify_tls: bool = True
    prefer_return: Literal["representation", "minimal", "OperationOutcome", ""] = "representation"


class SmartClient(BaseModel):
    client_id: str
    jwks: dict | None = None
    jwks_url: str | None = None
    public_key_path: str | None = None
    client_secret: str | None = None  # allows client_secret_basic for convenience
    scopes: str = "system/*.*"


class ServerAuth(BaseModel):
    types: list[Literal["none", "basic", "bearer", "smart"]] = Field(default_factory=lambda: ["none"])
    users: dict[str, str] = Field(default_factory=dict)  # basic: username -> password
    tokens: list[str] = Field(default_factory=list)  # static bearer tokens
    smart_clients: list[SmartClient] = Field(default_factory=list)
    token_lifetime: int = 3600
    enforce_scopes: bool = True


class ValidationSettings(BaseModel):
    inbound: Literal["strict", "warn", "off"] = "strict"
    validator_jar: str | None = None
    igs: list[str] = Field(default_factory=list)
    tx_server: str | None = "n/a"  # "n/a" disables terminology checks in the HL7 validator
    java: str = "java"


class Identifiers(BaseModel):
    mrn: str = "urn:oid:1.2.3.4.5.1"
    visit: str = "urn:oid:1.2.3.4.5.2"
    placer_order: str = "urn:oid:1.2.3.4.5.3"
    filler_order: str = "urn:oid:1.2.3.4.5.4"
    practitioner: str = "urn:oid:1.2.3.4.5.5"
    organization: str = "urn:oid:1.2.3.4.5.6"
    location: str = "urn:oid:1.2.3.4.5.7"
    appointment: str = "urn:oid:1.2.3.4.5.8"
    prescription: str = "urn:oid:1.2.3.4.5.9"
    claim: str = "urn:oid:1.2.3.4.5.10"
    accession: str = "urn:oid:1.2.3.4.5.11"
    member: str = "urn:oid:1.2.3.4.5.12"
    message: str = "urn:oid:1.2.3.4.5.13"
    generic: str = "urn:oid:1.2.3.4.5.99"


class SimulatorSettings(BaseModel):
    enabled: bool = True
    delay_seconds: float = 2.0
    auto: list[str] = Field(
        default_factory=lambda: ["ServiceRequest", "Task", "Appointment", "MedicationRequest", "Claim"]
    )
    results_to: str = "local"  # "local" or a peer name to push results/responses to


class SubscriptionSettings(BaseModel):
    # R4 rest-hook with payload: PUT {endpoint}/{type}/{id} ("put-resource") or POST resource to endpoint
    notify_method: Literal["put-resource", "post-endpoint"] = "put-resource"
    retries: int = 3
    hook_token: str | None = None  # bearer token peers send on rest-hook notifications; generated + persisted if empty


class Settings(BaseModel):
    base_url: str = "http://localhost:8090/fhir"
    host: str = "0.0.0.0"
    port: int = 8090
    public_url: str | None = None  # externally reachable root (used for hook endpoints); default derived
    data_dir: str = "data"
    db_path: str | None = None
    facility_name: str = "SEKMET General Hospital"
    software_name: str = "SEKMET FHIR R4 Tester"
    server_auth: ServerAuth = Field(default_factory=ServerAuth)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    identifiers: Identifiers = Field(default_factory=Identifiers)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)
    subscriptions: SubscriptionSettings = Field(default_factory=SubscriptionSettings)
    own_private_key_path: str = "keys/sekmet_private.pem"
    peers: dict[str, Peer] = Field(default_factory=dict)
    max_body_log: int = 2_000_000

    @property
    def root_url(self) -> str:
        """Server root (base_url without the trailing /fhir)."""
        if self.public_url:
            return self.public_url.rstrip("/")
        b = self.base_url.rstrip("/")
        return b[: -len("/fhir")] if b.endswith("/fhir") else b

    @property
    def database(self) -> str:
        return self.db_path or str(Path(self.data_dir) / "sekmet.db")

    def peer(self, name: str) -> Peer:
        if name == "self" and "self" not in self.peers:
            return Peer(base_url=self.base_url.rstrip("/"), auth=_self_auth(self))
        if name not in self.peers:
            raise KeyError(f"Unknown peer '{name}'. Known: {', '.join(self.peer_names())}")
        return self.peers[name]

    def peer_names(self) -> list[str]:
        names = list(self.peers)
        return names if "self" in names else ["self", *names]


def _self_auth(s: Settings) -> PeerAuth:
    """Loopback peer uses whatever inbound auth we accept."""
    a = s.server_auth
    if "none" in a.types:
        return PeerAuth()
    if "bearer" in a.types and a.tokens:
        return PeerAuth(type="bearer", token=a.tokens[0])
    if "basic" in a.types and a.users:
        u, p = next(iter(a.users.items()))
        return PeerAuth(type="basic", username=u, password=p)
    return PeerAuth()


_settings: Settings | None = None


def load_settings(path: str | None = None) -> Settings:
    global _settings
    path = path or os.environ.get("SEKMET_CONFIG", "settings.yaml")
    data: dict = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text()) or {}
    for key, env in (("base_url", "SEKMET_BASE_URL"), ("db_path", "SEKMET_DB"), ("public_url", "SEKMET_PUBLIC_URL")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    if os.environ.get("SEKMET_PORT"):
        data["port"] = int(os.environ["SEKMET_PORT"])
    _settings = Settings.model_validate(data)
    return _settings


def get_settings() -> Settings:
    return _settings or load_settings()


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
