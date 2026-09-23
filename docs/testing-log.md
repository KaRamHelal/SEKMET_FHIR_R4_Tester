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
