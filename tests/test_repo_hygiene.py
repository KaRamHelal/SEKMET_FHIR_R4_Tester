"""Guard for a public repo: no secrets, private keys, local settings or captured traffic may be tracked."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PATHS = re.compile(r"(^|/)(settings\.yaml|\.env(\..*)?|.*\.(secret|pem|key|db))$|^(keys|data|reports)/")
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    # credential-style keys followed by a long value mixing letters and digits (placeholders don't)
    re.compile(r"\b(client_secret|password|passwd|access_token|api_key|secret_key)\b\s*[:=]\s*['\"]?"
               r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{16,}"),
]


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [f for f in out.splitlines() if f]


def test_no_forbidden_files_tracked():
    bad = [f for f in tracked_files() if FORBIDDEN_PATHS.search(f)]
    assert not bad, f"these must not be committed to the public repo: {bad}"


def test_no_secrets_in_tracked_files():
    hits = []
    for f in tracked_files():
        if f.startswith("tests/test_repo_hygiene"):
            continue
        try:
            text = (ROOT / f).read_text(errors="ignore")
        except (IsADirectoryError, FileNotFoundError):
            continue
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{f}: {m.group(0)[:40]}...")
    assert not hits, "possible secrets in tracked files:\n" + "\n".join(hits)
