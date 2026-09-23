# Operations guide

Everything needed to install, configure and run SEKMET against a system under test.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config/settings.example.yaml settings.yaml     # git-ignored; never commit it
pytest -q                                         # full self-test incl. loopback scenarios (~10 s)
```

Settings are read from `$SEKMET_CONFIG`, else `./settings.yaml`. Env overrides: `SEKMET_BASE_URL`, `SEKMET_PORT`,
`SEKMET_DB`, `SEKMET_PUBLIC_URL`.

## Everyday commands

| Command | What it does |
|---|---|
| `sekmet serve` | FHIR server at `base_url` and console at `/ui` |
| `sekmet peers list` | configured peers (`self` = loopback, always present) |
| `sekmet peers test <peer>` | **read-only**: token (if OAuth) + CapabilityStatement + resources our flows need |
| `sekmet scenario list` | scenarios from `./scenarios/` and the built-in library |
| `sekmet scenario run <id…\|all> --peer <peer> [--tag t]` | runs scenarios; reports go to `reports/<time>-<peer>/` (HTML, JSON, JUnit) |
| `sekmet workflow list` / `workflow run <key> -t <target> -a k=v` | a single hospital workflow step |
| `sekmet seed --count N -t <target>` | master data + N patients (every other one admitted) |
| `sekmet subscriptions register <peer>` | creates the `peers.<peer>.subscribe` Subscriptions on the peer |
| `sekmet keys generate` | key pair for SMART Backend Services; public JWKS at `/.well-known/jwks.json` |
| `sekmet validate file.json [--conformance]` | structural validation (+ HL7 validator jar) |
| `sekmet validator download` | fetches `validator_cli.jar` into `tools/` |
| `sekmet reset --yes` / `sekmet reindex` | wipe the local DB / rebuild the search index |

`scenario run` starts an embedded server when none answers at `base_url` (needed for the loopback peer, hooks and
simulator). Exit code 0 means every scenario ended passed, warning or skipped. It's 1 otherwise, which suits CI.

## Recommended procedure for a new system under test

1. Add the peer to `settings.yaml` (see below). Put secrets in `keys/<peer>.secret`, never in YAML you share.
2. `sekmet peers test <peer>`: confirm the token, FHIR version and `missing_for_flows`.
3. `sekmet scenario run conformance_smoke --peer <peer>`: the basic REST behaviour.
4. Run the flow scenarios one at a time, reading each report before the next.
5. Triage every failure: is it a **SEKMET bug** (fix it and add a test) or **peer behaviour** (record it and don't
   adapt to it)? Record the round in `docs/testing-log.md` (public servers only).

Scenarios write synthetic data only (Faker names, random MRNs). Still, run write scenarios only against test
environments you're allowed to write to.

## Result statuses

| Status | Meaning |
|---|---|
| `passed` | every check held |
| `warning` | a `level: should` check failed: a spec SHOULD or best practice, not a hard violation |
| `failed` | a SHALL-level expectation didn't hold; later steps are skipped unless `continue_on_failure` |
| `error` | the step itself crashed (usually a SEKMET bug: investigate) |
| `skipped` | a requirement wasn't met: capability not advertised, role not played, no `public_url`, or a previous failure. The reason is in the message. |

## Configuration reference (`settings.yaml`)

**Server**: `base_url`, `host`, `port`, `public_url` (externally reachable root; required for peers to call
`/hooks`), `data_dir`, `db_path`, `facility_name`.

**`server_auth`** (inbound, what callers of SEKMET must send): `types` (any of `none basic bearer smart`), `users`
(basic), `tokens` (static bearer), `smart_clients[]` (`client_id`, `jwks` | `jwks_url` | `public_key_path`,
`client_secret`, `scopes`), `token_lifetime`, `enforce_scopes`. `/fhir/metadata` and `.well-known` stay open.

**`server_behaviour`**: spec-allowed variations SEKMET's server can show, to test HIS clients:
- `absolute_references`: answer with absolute literal references (storage stays relative).

**`validation`**: `inbound` (`strict` = reject invalid with 400, `warn`, `off`), `validator_jar`, `igs[]`,
`tx_server`, `java`.

**`simulator`**: `enabled`, `delay_seconds`, `auto[]` (resource types to react to), `results_to` (`local` or a
peer name). It reacts only to writes from outside SEKMET.

**`subscriptions`**: `notify_method` (`put-resource` = R4 `PUT {endpoint}/{type}/{id}`, or `post-endpoint`),
`retries`, `hook_token` (generated and persisted when empty).

**`identifiers`**: identifier system URIs used for generated data (MRN, visit, orders, …).

**`peers.<name>`**:

| Key | Notes |
|---|---|
| `base_url` | FHIR base |
| `auth.type` | `none`, `basic` (`username`, `password`), `bearer` (`token`), `smart` (`client_id`, `private_key_path`, `alg`, `kid`, `jku`, `scope`, `token_url`), `client_credentials` (`client_id` + `client_secret` \| `client_secret_env` \| `client_secret_file`, `client_auth_method`, `scope`, `token_url`) |
| `auth.token_url` | discovered from `.well-known/smart-configuration` or CapabilityStatement when empty |
| `mode` | `rest` (write to the peer's API) or `messaging` (write locally and send FHIR messages to `$process-message`) |
| `messaging_endpoint`, `message_destination` | default `{base_url}/$process-message` / `base_url` |
| `roles[]` | actors the peer plays: `lab-filler imaging-filler payer scheduler pharmacy`. Steps needing an actor skip otherwise. `self` has all of them while the simulator is on. |
| `use_transactions` | multi-resource steps as one `transaction` Bundle; `false` creates them in order and resolves `urn:uuid` client-side |
| `prefer_return` | `Prefer: return=` sent on writes |
| `headers` | extra headers on every request |
| `subscribe[]`, `fetch_on_ping`, `mirror_notifications` | subscriptions SEKMET registers on the peer, and what it does with notifications |
| `timeout`, `verify_tls` | HTTP settings |

## Where to look

- **Console `/ui`**: resources, workflow forms, scenario runs, traffic (every request and response in both
  directions; `Authorization`, secrets and tokens are redacted), peers, subscriptions, simulator, validator.
- **Reports**: `reports/<time>-<peer>/report.html`. Each step lists its checks and the traffic ids involved.
- **Traffic for one run**: `/ui/traffic?run=<run id>`.

## Public repository rules

The repo is public. `settings.yaml`, `keys/`, `*.secret`, `*.pem`, `.env*`, `data/` (captured traffic) and
`reports/` are git-ignored, and `tests/test_repo_hygiene.py` fails if any are tracked or if a tracked file looks
like it holds a key, JWT or secret. Keep customer names, private endpoints and findings about private systems out
of committed files.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Auth failed … client_credentials auth requires client_id and a secret` | set `client_id` and a secret source; check `keys/<peer>.secret` exists |
| Every search assertion fails on a server | the server may omit `entry.search.mode`; library assertions use `entry.resource.ofType(X)` for this reason |
| `412 … duplicating existing resource` on a create | the server de-duplicates identical content; make payloads unique with `${uid}` |
| Subscription scenario skipped | set `public_url` to an address the peer can reach |
| Workflow crashes on references | the peer may return absolute references; use `ref_type()` / `same_ref()` in workflow code, never `startswith("Type/")` |
| Loopback results show duplicate resources | a scenario ran with the simulator on while driving both sides; only filler/payer scenarios set `simulator: true` |
