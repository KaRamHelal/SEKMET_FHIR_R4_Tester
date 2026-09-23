# Testing log: what testing SEKMET against real servers taught us

Each round runs the scenario library against one independent FHIR implementation. Findings are split into
**SEKMET fixes** (bugs or gaps in the tester, fixed in code with a test) and **peer behaviour** (reported, never
adapted to). Only public test servers are recorded here. How to run a round: [operations.md](operations.md).

## Peer behaviour matrix

| Behaviour | candle | HAPI 8.13 | Firely 6.9 | Spark | WildFHIR 0.7.1 |
|---|---|---|---|---|---|
| history / vread | ✗ | ✓ | ✓ | ✓ | ✓ |
| stale `If-Match` → 409/412 | 500 | ✓ | ✓ | ✓ | ✓ |
| rejects unknown elements | ✗ | ✗ | ✓ | ✓ | ✗ |
| `next` paging link | ✗ | ✓ | ✓ | ✓ | ✓ |
| `entry.search.mode` present | ✓ | ✓ | ✓ | ✗ | ✓ |
| references returned | relative | relative | **absolute** | relative | relative |
| `urn:uuid` resolved in arrays in transactions | ✗ (`Task.basedOn`) | ✓ | ✓ | ✓ | ✓ |
| de-duplicates identical creates | – | ✓ (412) | – | – | – |
| classic R4 Subscriptions | ✗ (backport only) | ✓ | – | – | – |
| topic-based (Backport) Subscriptions | ✓ (handshake, id-only, `$status`) | – | – | – | – |
| FHIR XML both ways | – | ✓ | ✓ | ✓ | – |
| Bulk `$export` (Group) | – | ✓ (Retry-After 120 s) | ✓ | – | – |
| `$process-message` | ✗ | ✗ | ✗ | ✗ | ✗ |

✓ = spec-expected behaviour, ✗ = deviation or missing, – = not observed.

## Round 0: fhir-candle (HL7, local, 2026-09-23)
Peer behaviour: no history/vread; 500 (not 409/412) on stale If-Match; transaction leaves `urn:uuid`
unresolved in `Task.basedOn[]` (resolved in `Task.focus`); no `next` paging link; accepts unknown elements;
classic R4 Subscriptions rejected (backport only); no `$process-message`.
SEKMET fixes:
- Capability-aware skipping (`requires: {resource, interaction, operation, messaging}`) separates "not
  supported" from "broken".
- Scenario-level `continue_on_failure` so independent conformance checks all report.

## Round 1: HAPI FHIR public, 8.13.9 (hapi.fhir.org/baseR4)
Result: 9 passed, 1 with a warning, 2 skipped. No failures that point at SEKMET.
Peer behaviour: accepts unknown elements (201); de-duplicates byte-identical creates (412 HAPI-2840); no messaging.
SEKMET fixes:
- **Check levels:** `level: should` steps report `warning` instead of failing. Spec SHOULDs and common leniency
  are visible, but they don't fail a run.
- **Peer roles:** `peers.<name>.roles` (lab-filler, imaging-filler, payer, scheduler, pharmacy). Steps needing an
  actor (`requires: {role: payer}`) skip on plain FHIR stores instead of timing out. The loopback peer
  gets all roles while the simulator is on.
- **Callback reachability:** `requires: {public_url: true}` skips rest-hook tests when the peer can't reach
  SEKMET, rather than making it call its own localhost.
- **Unique payloads:** every probe body carries `${uid}`, because content-deduplicating servers otherwise answer
  a different question (412 duplicate) than the one being tested.

## Round 2: Firely Server public, 6.9.1 (server.fire.ly/r4)
Result: 11/12 on first run, 12/12 after the fix (payer, lab-filler, messaging and callback steps skipped by design).
Peer behaviour: answers with **absolute literal references** (`https://server.fire.ly/r4/Patient/…`), which
FHIR allows.
SEKMET fixes:
- **Bug:** scheduling workflows found participants with `reference.startswith("Patient/")` and crashed
  (`StopIteration`) on absolute references. Every reference check now uses `ref_type()` / `same_ref()`, which
  parse the relative, absolute and `_history` forms. Regression test added.
- **New server behaviour:** `server_behaviour.absolute_references: true` makes SEKMET's own server answer with
  absolute references (storage stays relative). A HIS client can then be tested against this allowed variation,
  and the whole library passes on loopback with it on.

## Round 3: Spark public (spark.incendi.no/fhir)
Result: 5/12 on first run, 12/12 after the fix (1 warning).
Peer behaviour: searchset entries have **no `search.mode`**. That's allowed in R4 (0..1).
SEKMET fixes:
- **Bug (scenarios):** every search assertion used `entry.where(search.mode = 'match')`, which isn't guaranteed.
  All library assertions now count `entry.resource.ofType(X)`. A separate `level: should` check reports a missing
  `search.mode`.
- **Bug (runner):** a `wait_for` `fhirpath` written beside `search:` was silently ignored, so
  `lab_order_peer_fills` accepted a preliminary report. Both placements now apply. Regression test added.
- **Docs:** added [operations.md](operations.md) and [scenarios.md](scenarios.md), with portability rules distilled
  from rounds 0 to 3.

## Round 4: WildFHIR public, 0.7.1 (wildfhir.wildfhir.org/r4)
Result: 12/12 (1 warning), with no SEKMET changes needed. The fixes from rounds 1 to 3 held on a fifth
implementation.
Peer behaviour: accepts unknown elements (201); otherwise spec-expected on everything the library checks.

## Round 5: Inferno SMART App Launch test kit (STU2.2, Backend Services group) vs SEKMET's server
This time the direction is reversed: Inferno tests **SEKMET** as the authorization and FHIR server.
It runs in one rootless Podman container (`scripts/inferno.sh`).
First run: 7/9 (TLS failed; `authorization_endpoint` missing). Final: all Backend Services authorization tests
pass, including TLS 1.2+. One discovery check fails by design.
SEKMET fixes:
- **SMART discovery:** added the required `authorization_endpoint`. `/auth/authorize` answers
  `unsupported_response_type`, because only client_credentials is implemented. Dropped `issuer`: SMART 2.2 wants it
  omitted without `sso-openid-connect`.
- **HTTPS serving:** `tls_certfile` / `tls_keyfile`, and `sekmet keys tls-cert` for self-signed test certs.
By design: `3.1.02` requires the `authorization_code` grant plus PKCE `S256`, i.e. user-facing SMART App Launch.
SEKMET tests system-to-system integration and doesn't claim that capability.

## Round 6: Subscriptions Backport, SEKMET vs fhir-candle
Built topic-based subscriptions (R4 Backport IG) in SEKMET, both server and subscriber, then tested in both
directions. No new downloads needed.
- The **reference format was captured from candle**: first entry `Parameters` with kebab-case names
  (`events-since-subscription-start`, `notification-event`, `event-number`, `additional-context`), a handshake on
  creation, and no focus entry for `id-only`. SEKMET emits exactly that and also accepts camelCase and R4B
  `SubscriptionStatus`.
- `subscription_backport_roundtrip` passes on loopback and against candle (its `encounter-complete` topic
  filters on `subject`, hence the new `--var filter_param=subject`).
SEKMET fixes and additions:
- Backport subscriptions: topic registry (Basic + SubscriptionTopic extensions), handshake-gated activation,
  filter validation against `canFilterBy`, event and heartbeat delivery, `$status`, and topics in the
  CapabilityStatement.
- Receiver: handshake, heartbeat and event kinds are recorded, and `id-only` focus is fetched from the peer.
- `requires.public_url` now only gates remote peers; a localhost peer can reach a localhost SEKMET.
- `scenario run --var name=value` overrides scenario variables.

## Round 7: FHIR XML, SEKMET vs HAPI and Firely (XML on the wire)
Added XML to SEKMET's server and client. Results: the full library passes over XML on loopback (13/13),
against **HAPI in XML** (13/13: 198/199 responses were XML, every body SEKMET sent was XML except the deliberately
invalid probe) and against **Firely in XML** (13/13). Spark answers XML on request too.
Both servers accepted SEKMET's XML and SEKMET parsed both servers' XML. That independently validates the serializer
and parser SEKMET's own XML server uses.
SEKMET fixes and additions:
- Server: `application/fhir+xml` in and out, `_format`, errors as XML OperationOutcome, XXE-safe parsing, `xml` in
  `CapabilityStatement.format`. Unknown formats still get 406.
- Client: `peers.<name>.format: xml`, with a traffic note when a peer ignores it.
- **Design finding:** deliberately invalid probe bodies can't be serialized to XML through the models. The client
  sends those as JSON and records it, instead of crashing the step.
- Conformance: a new SHOULD-level check "answers XML when asked", gated by `requires: {format: xml}`; header
  expectations accept `~contains`.

## Round 8: FHIR TestScript engine: official R4 examples, HAPI, export/replay
Built a TestScript executor, TestReport output and run export in SEKMET, then tested:
- **Official R4 TestScript examples** (6) on SEKMET's own server: 5/6 pass. The remaining one is **R004 in
  `testscript-example-readtest`, which is stale**: it expects 400 for the id `ID-may-not-contain-CAPITALS`, but R4
  ids allow capitals, so 404 is correct. Preconditions the examples assume: a known `Patient/example` on the
  server, and a `Patient/pat1` fixture that the R4 spec doesn't publish (any Patient works).
- **Same scripts on HAPI public:** SEKMET executed everything, including XML accept, XPath asserts, header
  variables and multi-destination. The failures were environment ones: DELETE of the shared `Patient/example` → 409
  (referenced by others); fixture referencing `Organization/1` → 400 (HAPI enforces referential integrity);
  plus the stale R004.
- **Export → replay:** `lab_order_to_result` recorded, exported (38 operations, 18 id variables) and replayed on a
  fresh SEKMET: passes. The same flow recorded on HAPI replays on HAPI until a byte-identical create hits HAPI's
  de-duplication (412).
SEKMET fixes found while testing:
- XPath variable paths without the `fhir:` prefix (`Patient/id`) are normalized.
- Export: PUT fixtures kept real ids, so the replay sent no body (fixed). `${var}` in `resource.id` made exported
  scripts invalid FHIR; transaction PUT entries now drop `resource.id` (the parameterized `request.url` carries it).
  Conditional headers (`If-None-Exist`, `If-Match`…) are exported, and conditional creates accept 200 or 201.
- `<Type>.id` variables fall back to the `Location` header when a server answers with an empty body (HAPI
  conditional-create hit).

## Round 9: load and concurrency mode, SEKMET under load and fhir-candle
Built `sekmet load run`. Its first run (8 users, 20 s, `adt_admit_transfer_discharge`) against SEKMET's own server
found **two SEKMET server bugs**:
- **Concurrency bug:** reads on the shared SQLite connection ran without the store lock. Under parallel load, freshly
  created resources read back as **404** and some requests failed with **500 "bad parameter or other API
  misuse"**: 43.7% failed journeys, 1.8% 5xx. All store reads now hold the lock, and a regression test does 120
  parallel create→read→search round trips. Result: **0% failed journeys, 0% 5xx**.
- **Search scaled with data volume:** each search parameter was a correlated `EXISTS`, so SQLite scanned every
  resource of the type (51 ms per search at only 337 Encounters). Throughput fell from 160 to 83 req/s over 30 s.
  Search is now index-driven (`id IN (SELECT id FROM idx …)`) with new composite indexes on
  (type, param, rtype, rid), lo and num: **0.36 ms** for the same query. The same 30 s run went from
  110 → **145 req/s**, p95 156 → **84 ms**, and stays flat over time.
- Minor: `log_inbound: false` skips the inbound traffic log (about 10%).
**fhir-candle under the same load** (`adt_merge_update`, 8 users): 354 req/s, p95 37 ms, 0 errors, 0 failed
journeys. So SEKMET's load client isn't the bottleneck; SEKMET's own server (Python + SQLite + a single write
lock) tops out around 145 req/s on this machine.

## Round 10: Bulk Data $export, SEKMET, HAPI and Firely
Built async `$export` in SEKMET (server) and a protocol-following client (`bulk_export` step, `sekmet bulk
export`). `bulk_export_group` passes on loopback, **HAPI** (async job about 22 s; `X-Progress` QUEUED → IN_PROGRESS)
and **Firely** (about 32 s). Only small Group-level exports were used on the public servers.
SEKMET fix found while testing:
- **Client politeness:** HAPI answered `Retry-After: 120`, but the first client capped polling at 10 s, polling
  12× more often than asked. The spec says clients SHOULD honour it, so the client now does by default and
  records what was requested. The library scenario opts into `poll_max: 10` explicitly, with a comment, for
  speed.

## Round 11: web console catch-up
The console now covers everything the CLI does: TestScripts (upload/paste, peers, fixtures, variables, TestReport
download), Load (form with the non-local ownership guard, results, full report), Bulk (client runs plus the
exports SEKMET served), and topics plus notification kinds on the Subscriptions page. Checked in a real browser.
SEKMET fixes found while testing:
- A new route handler named `load_run` shadowed the imported `load_run()`, so run detail and TestReport pages
  returned 500. Renamed the handler; a UI test now covers the TestScript → TestReport path.
- Results tables overflowed their panels (link off-screen, horizontal page scroll); they now scroll inside the
  panel.
