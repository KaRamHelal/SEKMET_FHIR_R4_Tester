"""Application context: one store/service/engine set per running app (tests can create several)."""
from __future__ import annotations

import secrets
import threading

from .client.peer import PeerClient
from .config import Settings
from .fhir.service import FhirService
from .fhir.store import Store


class AppContext:
    def __init__(self, settings: Settings):
        from .messaging.messenger import Messenger, register as register_messaging
        from .subscriptions.engine import SubscriptionEngine
        from .workflows import load_all
        from .workflows.simulator import Simulator, claim_submit_op

        load_all()
        self.settings = settings
        self.store = Store(settings.database)
        self.service = FhirService(self.store, settings)
        self.service.ctx_ref = self  # lets operations reach engines
        self.messenger = Messenger(self)
        register_messaging(self.service, self.messenger)
        self.service.register_operation("Claim", "$submit", claim_submit_op)
        self.subscriptions = SubscriptionEngine(self)
        self.subscriptions.seed_topics()
        from .subscriptions.engine import status_op
        self.service.register_operation("Subscription", "$status", status_op)
        from .bulk.server import BulkExports, register as register_bulk
        self.bulk = BulkExports(self)
        register_bulk(self.service, self.bulk)
        self.simulator = Simulator(self)
        self.store.on_write(self.subscriptions.on_write)
        self.store.on_write(self.simulator.on_write)
        self._clients: dict[str, PeerClient] = {}
        self._lock = threading.Lock()
        if not settings.subscriptions.hook_token:
            tok = self.store.kv_get("hook_token") or secrets.token_urlsafe(16)
            self.store.kv_set("hook_token", tok)
            settings.subscriptions.hook_token = tok

    def peer_client(self, name: str) -> PeerClient:
        with self._lock:
            if name not in self._clients:
                self._clients[name] = PeerClient(name, self.settings.peer(name), self.store,
                                                 self.settings.max_body_log)
            return self._clients[name]

    def reset_clients(self) -> None:
        with self._lock:
            for c in self._clients.values():
                c.close()
            self._clients.clear()
