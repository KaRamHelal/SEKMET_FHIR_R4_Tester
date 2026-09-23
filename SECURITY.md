# Security policy

## Intended use

SEKMET is a **test tool**. It generates synthetic data and is meant for test environments.

- Do **not** run write scenarios or load tests against production systems or systems holding real patient data.
- Do **not** expose a SEKMET instance to the internet without authentication (`server_auth`) and TLS
  (`tls_certfile`/`tls_keyfile`). Its FHIR server stores whatever clients send it.
- Load mode refuses non-local targets unless you pass `--i-own-this-system`. Only use that for systems you're
  authorised to stress.

## Secrets

- Keep credentials out of the repository: use `client_secret_file` (e.g. `keys/<peer>.secret`) or
  `client_secret_env`. `settings.yaml`, `keys/`, `*.secret`, `*.pem`, `.env*`, `data/` and `reports/` are
  git-ignored.
- The traffic log redacts `Authorization` headers, client secrets, client assertions and access tokens, but it
  stores request and response **bodies**, which may contain whatever the system under test returns. Treat `data/`
  and `reports/` as sensitive when testing systems that hold real data.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a
public issue. Include affected versions, reproduction steps and impact. We'll acknowledge within a few days.
