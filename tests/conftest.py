import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from sekmet.config import Settings
from sekmet.main import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_settings(tmp_path, port: int = 8099, **kw) -> Settings:
    data = {"base_url": f"http://127.0.0.1:{port}/fhir", "port": port, "host": "127.0.0.1",
            "db_path": str(tmp_path / "sekmet.db"), "simulator": {"delay_seconds": 0.2}, **kw}
    return Settings.model_validate(data)


@pytest.fixture
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        c.ctx = app.state.ctx
        yield c


def start_server(settings: Settings):
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"
    return app, server


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A real HTTP server (needed for loopback peer calls, rest-hook delivery and messaging)."""
    tmp = tmp_path_factory.mktemp("live")
    port = free_port()
    app, server = start_server(make_settings(tmp, port))
    yield app.state.ctx
    server.should_exit = True
