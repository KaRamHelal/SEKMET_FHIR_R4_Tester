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
| `sekmet load run <scenario> -p <peer> -u 10 -d 60 [--ramp s] [--max-p95 ms] [--max-error-rate %] [--max-iteration-failure %]` | load test: scenario as user journey, per-endpoint percentiles, correctness under concurrency; exit 1 on a failed gate |
| `sekmet testscript run <files\|dirs> -p <peer> [-p <peer2>] [-f fixtures/] [--var k=v]` | executes FHIR TestScripts (JSON or XML); writes reports plus one `TestReport-*.json` per script |
| `sekmet testscript export <run id> [--out f.json]` | turns a recorded run into a replayable TestScript (requests, fixtures, id variables, response-code asserts) |
| `sekmet scenario run … --var name=value` | overrides a scenario variable (e.g. another peer's topic canonical) |
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

**`log_inbound`**: record every inbound request/response in the traffic log (default true).

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
| `format` | `json` (default) or `xml`: the wire format SEKMET uses with this peer (bodies and `Accept`) |
| `headers` | extra headers on every request |
| `subscribe[]`, `fetch_on_ping`, `mirror_notifications` | subscriptions SEKMET registers on the peer, and what it does with notifications |
| `timeout`, `verify_tls` | HTTP settings |

## Where to look

- **Console `/ui`**: resources, workflow forms, scenario runs, traffic (every request and response in both
  directions; `Authorization`, secrets and tokens are redacted), peers, subscriptions, simulator, validator.
- **Reports**: `reports/<time>-<peer>/report.html`. Each step lists its checks and the traffic ids involved.
- **Traffic for one run**: `/ui/traffic?run=<run id>`.

## Load and concurrency testing

`sekmet load run <scenario> --peer <peer> --users N --duration S` runs any scenario as a **user journey**, with N
virtual users in parallel. Scenario assertions still apply, so the report shows **correctness under concurrency**
(failed journeys, grouped by first failing step) next to performance.
- **Metrics:** requests, throughput, 5xx/transport error rate, overall and per-endpoint p50/p90/p95/p99/max, per
  endpoint template (`GET Patient/{id}`, `POST Encounter`, `GET Encounter?search`…), and a per-second timeline.
  `reports/<time>-load-<peer>/load.html` has throughput and p95 charts plus tables; `load.json` has everything.
- **Options:** `--iterations` (total journeys), `--ramp` (spread user start), `--think-ms`, `--var`,
  `--log-traffic` (per-request logging is off by default under load).
- **Gates (CI):** `--max-error-rate`, `--max-p95`, `--max-iteration-failure`. Any breach exits with code 1.
- **Safety:** non-local peers are refused unless you pass `--i-own-this-system`. Only load systems you're
  authorised to stress, and never shared public test servers.
- Master data is created once before the run (warm-up), so users don't race to create the same organizations.
- For SEKMET's own server under heavy load, `log_inbound: false` skips the inbound traffic log (about 10% faster).

## FHIR TestScript

- **Run:** `sekmet testscript run scripts/ -p hapi -f fixtures/`. Fixtures resolve from `#contained`, fixture
  directories (`Type/id` → `type-id.json|xml`), the script's own directory, or `--fixture-base` (default
  `https://hl7.org/fhir/R4`; pass `""` to stay offline). Multi-system scripts map `destination` n to the n-th `-p`.
  Manual-input variables (`hint`) come from `--var`.
- **Supported:** operation types read, vread, create, update(Create), delete, search, history, transaction/batch,
  patch, capabilities, validate, or explicit `method`/`url`. Per-operation `accept`/`contentType` json|xml,
  `requestHeader`, `targetId`, `sourceId`, `responseId`, autocreate/autodelete. Asserts: response, responseCode,
  resource, contentType, headerField, FHIRPath `expression`, XPath `path` (evaluated on the XML rendering),
  compareToSource*, value + all operators, minimumId, navigationLinks, validateProfileId (structural), requestURL,
  requestMethod, warningOnly (→ `warning`). A failed setup skips the tests; teardown always runs.
- **TestReport:** each run also writes a FHIR `TestReport` (setup, tests, teardown with pass/fail/warning/skip).
- **Export and replay:** `sekmet testscript export <run id>` records a run as a TestScript. Bodies become contained
  fixtures, server-assigned ids become variables (from the create response, or its `Location` when the body is
  empty), and conditional headers are kept. Replays pass on a fresh server. Replaying on the **same**
  content-deduplicating server (e.g. HAPI) hits 412 on byte-identical creates; use a clean server or tenant.
- **Environment preconditions** are not SEKMET failures. Examples: a script that reads a "known" `Patient/example`
  needs it on the server first; fixtures that reference `Organization/1` fail on servers enforcing referential
  integrity; shared public servers may refuse deletes of referenced resources (409).

## Formats (JSON and XML)

- **Server:** SEKMET accepts and returns `application/fhir+json` and `application/fhir+xml`. `_format` wins over
  `Accept`; JSON wins when a client accepts both. Unsupported formats get 406. XML is parsed with entity
  resolution and network access disabled (no XXE).
- **Client:** `peers.<name>.format: xml` makes SEKMET send XML bodies and ask for XML responses. The traffic note
  flags a peer that answers JSON anyway. Deliberately invalid probes can't be expressed as XML, so they're sent as
  JSON, and the note says so.
- XML goes through the R4B structure models (element order and cardinality need the definitions). R4-only elements
  outside R4B would be rejected in XML; use JSON for those.

## Subscriptions

SEKMET supports both R4 styles, as a server and as a subscriber:

- **Classic rest-hook**: `criteria` is a search (`Encounter?patient=…`). Notifications are the resource
  (`PUT {endpoint}/{type}/{id}` by default), or an empty POST when there's no payload.
- **Topic-based (Subscriptions Backport IG)**: `criteria` is a topic canonical, filters go in
  `backport-filter-criteria`, and content is `empty` / `id-only` / `full-resource`. SEKMET sends a
  **handshake** (the subscription only becomes `active` if it's accepted), then **event-notification** history
  Bundles (a `Parameters` status entry, plus resources for `full-resource`) and optional **heartbeats**
  (`backport-heartbeat-period`). `Subscription/{id}/$status` returns the status.
- **Topics** are R4 `Basic` resources with the R5 `extension-SubscriptionTopic.*` extensions, listed at
  `GET /fhir/Basic?code=SubscriptionTopic` and advertised in the CapabilityStatement. Built-in topics
  (`http://sekmet.dev/fhir/SubscriptionTopic/…`): `encounter-start`, `encounter-complete`,
  `diagnosticreport-final`, `servicerequest-new`, `appointment-booked`, `patient-change`. To add a topic, POST a
  `Basic` in the same format; triggers use `fhirPathCriteria` with `%previous` / `%current`.
- **Receiving:** peers notify `{public_url}/hooks/<peer>/<key>`. Handshakes and heartbeats are recorded with their
  `kind`. For `id-only` events SEKMET reads the focus resource from the peer (`fetch_on_ping`). Everything is
  visible in `/ui/subscriptions` and usable in `wait_for: {notification: {kind: …}}`.

## Serving over HTTPS

```bash
sekmet keys tls-cert localhost 127.0.0.1 --out keys/tls    # self-signed, testing only
```
Set `tls_certfile: keys/tls.crt`, `tls_keyfile: keys/tls.key` and an `https://` `base_url`, then `sekmet serve`.
Clients must trust `keys/tls.crt`. Use a real certificate when a HIS has to call SEKMET across a network.

## Conformance-testing SEKMET itself with Inferno

This is the lightest setup: one rootless Podman container (Docker also works), with no compose stack, web UI or
Redis. Ruby 3.3.6 runs inside the container, so the host's Ruby version doesn't matter.

```bash
scripts/inferno.sh setup                       # clones the SMART App Launch kit into .inferno/, installs gems once
```

Run SEKMET with `server_auth.types: [smart]` and a `smart_clients` entry whose `jwks` holds the **public**
(`key_ops: verify`) keys from `.inferno/smart-app-launch-test-kit/lib/smart_app_launch/smart_jwks.json`.
Serve it over HTTPS (see above), then:

```bash
SEKMET_CA=keys/tls.crt scripts/inferno.sh run execute --suite smart_stu2_2 --groups 3 --outputter plain \
  --inputs "url:https://localhost:8090/fhir" \
  'backend_services_smart_auth_info:{"auth_type":"backend_services","use_discovery":"true","client_id":"inferno","requested_scopes":"system/*.rs","encryption_algorithm":"ES384"}'
```

Expected result: everything passes except `3.1.02`. That test requires `authorization_code` and PKCE (a full
user-facing SMART App Launch server), which SEKMET doesn't implement by design. `--outputter json` gives
machine-readable results. See [testing-log.md](testing-log.md) for the latest run.

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
