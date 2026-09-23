# Scenarios

## Catalogue

`Needs` lists what must be true for the whole scenario to run; individual steps may add their own requirements
and skip on their own.

| id | Purpose | Needs | Known peer behaviour seen |
|---|---|---|---|
| `conformance_smoke` | Core REST: CapabilityStatement, CRUD, XML on request (SHOULD), conditional create, If-Match 409/412, vread, history, transaction with `urn:uuid`, chained search + `_include`, paging, invalid-resource rejection (SHOULD), `search.mode` (SHOULD), delete→410 | – | candle: 500 on stale If-Match, no `next` link; HAPI/WildFHIR/candle accept unknown elements; Spark omits `search.mode` |
| `adt_admit_transfer_discharge` | Register, admit, transfer (location history), discharge (disposition, `period.end`), version history | – | candle: no history |
| `adt_merge_update` | Demographic update; merge via `Patient.link` (replaced-by / replaces), old MRN kept | – | – |
| `lab_order_to_result` | SEKMET drives placer **and** filler: SR+Task transaction, Task states, Specimen, Observations + DiagnosticReport transaction, `based-on` / `_include` searches | – | candle leaves `urn:uuid` unresolved in `Task.basedOn[]` |
| `lab_order_peer_fills` | SEKMET as **placer only**: orders and waits for the peer's final DiagnosticReport | role `lab-filler` | – |
| `rad_order_imaging_report` | Imaging SR, ImagingStudy (DICOM UIDs), RAD report with `presentedForm` | – | – |
| `appointment_book_cancel` | Schedule/slots, book (slot busy), reschedule (slot freed), cancel, proposed → AppointmentResponse → booked, check-in, complete | – | Firely returns absolute references |
| `medication_order_dispense` | Prescribe, dispense, administer, stop | – | – |
| `clinical_documentation` | Conditions by category / clinical status, allergy, procedure, vitals incl. BP components | – | – |
| `billing_claim` | Coverage, Account, ChargeItem, Claim; then the payer's ClaimResponse and `Claim/$submit` | payer steps need role `payer` | – |
| `subscription_roundtrip` | R4 rest-hook Subscription on the peer, matching changes notified to SEKMET `/hooks` | `Subscription` create, `public_url` | candle supports backport (topic) Subscriptions only |
| `subscription_backport_roundtrip` | R4 **Subscriptions Backport** (topic-based): handshake, filtered `id-only` event for `encounter-complete` (admit must not fire), focus fetched from the peer, `$status` | `Subscription` create, `public_url` for remote peers. Other peers' topics: `--var topic=… --var filter_param=…` | candle: passes (its topic filters on `subject`) |
| `messaging_adt_orders` | A04/A01/O21/R01/A03 message Bundles to `$process-message`, `response.code = ok`; malformed message rejected | peer advertises messaging | – |

Per-server results and what they changed in SEKMET are in [testing-log.md](testing-log.md).

Besides YAML scenarios, SEKMET runs standard **FHIR TestScript** resources and exports any run as one. See
[operations.md → FHIR TestScript](operations.md#fhir-testscript).

## Writing scenarios

Put YAML files in `./scenarios/`. A file with the same id as a library scenario overrides it.

```yaml
name: Readable name
description: What this proves.
tags: [adt]
target: peer                 # default target for action steps: peer | local | rest | messaging
simulator: false             # loopback only: let the simulator react (filler/payer scenarios)
continue_on_failure: false   # true when steps are independent checks
requires: {role: payer}      # whole scenario skipped when unmet (keys below)
vars: {x: 1}
steps:
  - name: Register
    action: adt.register_patient          # any key from `sekmet workflow list`
    args: {family: Test}
    save: reg                             # result available as ${reg.patient.id}, ${reg.mrn}
    expect: {on: patient, fhirpath: ["Patient.active = true"]}
  - name: Raw call
    request: {method: GET, path: Patient, params: {identifier: "${ids.mrn}|${reg.mrn}"}}
    expect: {status: 200, headers: {ETag: present}, fhirpath: ["Bundle.entry.resource.ofType(Patient).count() = 1"]}
  - assert: {on: reg, fhirpath: ["patient.exists()"]}
  - wait_for: {search: {type: DiagnosticReport, params: {based-on: "ServiceRequest/${o.service_request.id}"}},
               fhirpath: "DiagnosticReport.status = 'final'", timeout: 60}
  - wait_for: {notification: {resource_type: Encounter, resource_id: "${adm.encounter.id}"}, timeout: 30}
  - subscribe: {criteria: "Encounter?patient=Patient/${reg.patient.id}"}   # classic R4 rest-hook; deleted after run
  - subscribe: {topic: "http://sekmet.dev/fhir/SubscriptionTopic/encounter-complete",   # R4 Backport
                filters: ["Encounter?patient=Patient/${reg.patient.id}"], content: id-only, heartbeat: 60}
  - wait_for: {notification: {kind: handshake}, timeout: 20}   # kind: handshake | heartbeat | event-notification
  - validate: {resource: "${reg.patient}", conformance: false}
  - set: {k: v}
  - sleep: 1
```

**Header expectations:** `present`, an exact value, or `~text` (header contains the text, case-insensitive).

**Step options:** `name`, `save`, `expect`, `target`, `requires`, `level: should` (a failure becomes a warning),
`continue_on_failure`, `always` (runs even after a failure).

**`requires` keys:**

| Key | Meaning |
|---|---|
| `resource` | must be advertised in the CapabilityStatement |
| `interaction` | must be advertised, on `resource` if given, else at system level |
| `operation` | e.g. `submit`, `process-message` |
| `messaging` | peer advertises FHIR messaging |
| `format` | peer advertises that format in `CapabilityStatement.format`, e.g. `xml` |
| `role` | peer declares that actor in `roles` |
| `public_url` | SEKMET is reachable by the peer |
| `mode` | peer mode must match |
| `simulator` | simulator must be on |
| `loopback` | peer must be `self` |

**Running a whole library over XML:** point it at a peer with `format: xml`; every workflow then round-trips
through XML.

**Variables:** override any `vars:` entry on the command line with `--var name=value`. Built-ins: `${peer}`, `${peer_base}`, `${run_id}`, `${uid}` (unique per run), `${now}`, `${today}`,
`${ids.<system>}`, `${settings.…}`, plus everything saved. A value that is exactly `${x}` keeps its type (object,
list); inside text it's stringified.

## Portability rules (learned from real servers)

1. **Count results by type:** `Bundle.entry.resource.ofType(X)`. Never rely on `entry.search.mode`, which is
   optional (Spark omits it).
2. **Make every created payload unique** with `${uid}`. Some servers de-duplicate identical content (HAPI 412).
3. **Don't assume reference form.** Servers may answer `https://host/fhir/Patient/1`. In FHIRPath use
   `.contains('Patient/1')` or `.endsWith(…)`, not `=`.
4. **Grade expectations:** SHALL → default; SHOULD or common leniency → `level: should`.
5. **Gate on capability, not hope:** add `requires` for anything optional (history, vread, delete,
   transaction, operations, messaging, roles, callbacks), so "not supported" reads as skipped, not failed.
6. **YAML gotcha:** `on:` is fine (SEKMET maps YAML 1.1's boolean key back to `on`). Quote values containing `:`
   or starting with `$`, `{`, `[` or `*`.
