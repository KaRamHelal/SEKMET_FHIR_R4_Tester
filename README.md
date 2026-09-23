# SEKMET FHIR R4 Tester

A **micro HIS** for testing real Hospital Information Systems' FHIR R4 interfaces. SEKMET is not a
hospital system. It plays the *other side* of normal hospital integration flows, in both directions, and records
every exchange so you can see exactly where a real HIS deviates from the spec or from your expectations.

- **FHIR R4 server** (`/fhir`): the real HIS can push, query, subscribe and message to it.
- **FHIR R4 client**: it drives the real HIS through ADT, orders/results, scheduling, medications, clinical and billing flows.
- **Simulator**: when the HIS sends an order, appointment, prescription or claim, SEKMET plays the lab/RIS filler,
  scheduler, pharmacy or payer and produces the downstream resources.
- **Scenario runner**: YAML scenarios with FHIRPath assertions, reported as JSON, JUnit XML (for CI) and HTML.
- **Load testing**: any scenario as a user journey for N parallel users, with per-endpoint latency percentiles,
  error rates, correctness under concurrency and CI gates.
- **FHIR TestScript**: runs standard TestScripts (JSON/XML) with TestReport output, and exports any run as a
  replayable TestScript for Touchstone or other engines.
- **Traffic log**: every request and response in both directions, with correlation and run ids, in the web console.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config/settings.example.yaml settings.yaml      # edit peers / auth
sekmet serve                                        # FHIR: http://localhost:8090/fhir  Console: http://localhost:8090/ui
```

Try it end to end against itself (`self` is the loopback peer):

```bash
sekmet scenario run all --peer self                 # starts an embedded server if none is running
```

Then point it at the real system:

```bash
sekmet peers test my-his                            # CapabilityStatement + SMART token check + gap list
sekmet scenario run all --peer my-his               # exit code 1 on any failure; reports in ./reports/
```

## Documentation

- [docs/operations.md](docs/operations.md): install, commands, procedure for a new system, configuration
  reference, result statuses, troubleshooting, public-repo rules.
- [docs/scenarios.md](docs/scenarios.md): scenario catalogue with known peer behaviour, authoring reference,
  portability rules.
- [docs/testing-log.md](docs/testing-log.md): per-server test rounds, behaviour matrix, and what each round
  changed in SEKMET.

## What it covers

| Flow | Workflows (`sekmet workflow list`) | Resources |
|---|---|---|
| ADT / registration | register (A04), update (A08), merge (A40 via `Patient.link`), admit (A01), transfer (A02), discharge (A03), cancel (A11), outpatient visit | Patient, Encounter, Location, Organization, Practitioner, PractitionerRole |
| Orders & results | lab order, imaging order, accept, collect specimen, lab result, imaging study, imaging report, cancel | ServiceRequest, Task, Specimen, Observation, DiagnosticReport, ImagingStudy |
| Scheduling | create schedule + slots, find slots, book, respond, reschedule, cancel, check-in, complete | Schedule, Slot, Appointment, AppointmentResponse, Encounter |
| Medications | prescribe, dispense, administer, stop | MedicationRequest, MedicationDispense, MedicationAdministration |
| Clinical | condition, allergy, procedure, vital signs | Condition, AllergyIntolerance, Procedure, Observation |
| Billing | coverage, account, charge, claim (REST or `Claim/$submit`), adjudicate | Coverage, Account, ChargeItem, Claim, ClaimResponse |

Every workflow can run against a **target**:

- `local`: this server's store (the real HIS then reads or subscribes).
- `rest:<peer>`: writes directly to the peer's FHIR API. Multi-resource steps use `transaction` Bundles with
  `urn:uuid` references (set `use_transactions: false` for sequential creates).
- `messaging:<peer>`: writes locally and sends a FHIR message (`Bundle.type=message`, MessageHeader with the
  HL7 v2 trigger event: A01, O21, R01, S12…) to the peer's `$process-message`, then checks the acknowledgement.

## The FHIR server

JSON and XML (`Accept`, `Content-Type` or `_format`). Errors come back as an OperationOutcome in the requested
format.

- read, vread, create, update (update-as-create), JSON Patch and simple FHIRPath Patch, delete, history
  (instance, type and system), versioning with `ETag`/`If-Match`/`If-None-Match`, `Prefer: return=…`
- conditional create (`If-None-Exist`), conditional update and conditional delete
- `transaction` (atomic with rollback, `urn:uuid` and conditional reference resolution, spec processing order) and `batch`
- search: string, token, reference, date, number and uri params; modifiers `:exact :contains :text :not :missing
  :identifier :[Type] :below`; date and number prefixes; OR (`,`) and AND (repeat); chaining
  (`subject:Patient.identifier=`); reverse chaining (`_has`); `_id`, `_lastUpdated`, `_sort`, `_count`/paging
  links, `_include`/`_revinclude` (plus `:iterate`), `_summary`, `_elements`. Unknown params are reported in a
  `search.mode=outcome` entry, or rejected with `Prefer: handling=strict`.
- compartments (`Patient/123/Observation`), `Patient/$everything`, `$validate`, `$process-message`, `Claim/$submit`
- R4 **rest-hook Subscriptions**: peers `POST /fhir/Subscription`; SEKMET notifies on matching writes (retries, then
  `status=error`). With a payload the default is R4's `PUT {endpoint}/{type}/{id}`; set
  `subscriptions.notify_method: post-endpoint` to POST to the endpoint instead.
- the generated CapabilityStatement at `/fhir/metadata` reflects exactly the above

**Inbound auth** (`server_auth.types`, any combination): `none`, `basic`, static `bearer`, and `smart`.
SMART Backend Services uses `/auth/token` with `private_key_jwt` (RS384/ES384) or a client secret, discovery at
`/fhir/.well-known/smart-configuration`, and SMART v1 or v2 system scopes enforced per request.

**Outbound auth** per peer: `none`, `basic`, `bearer`, and `smart`. `smart` signs a client assertion, discovers
the token endpoint, caches the token, and refreshes on 401. Generate a key with `sekmet keys generate`; the public
key is served at `/.well-known/jwks.json` so the HIS can register SEKMET by JWKS URL.

## Scenarios

The built-in library (`sekmet scenario list`):

| id | checks |
|---|---|
| `conformance_smoke` | CapabilityStatement, CRUD, conditional create, If-Match 409/412, vread, history, transaction with `urn:uuid`, `_include`, paging, invalid-resource rejection, delete→410 |
| `adt_admit_transfer_discharge` | encounter lifecycle, location history, discharge disposition, search by patient+status |
| `adt_merge_update` | demographic update, merge links, old MRN carried over |
| `lab_order_to_result` | SR+Task transaction, Task state machine, Observations/DiagnosticReport transaction, `based-on` and `_include` searches |
| `lab_order_peer_fills` | SEKMET as **placer**: waits for the peer (LIS) to produce a final DiagnosticReport |
| `rad_order_imaging_report` | ImagingStudy with DICOM UIDs, RAD report with `presentedForm`, `basedon` search |
| `appointment_book_cancel` | slots, booking marks slot busy, reschedule, cancel, proposed→AppointmentResponse→booked, check-in, complete |
| `medication_order_dispense` | prescribe, dispense, administer, stop; `prescription` and `status` searches |
| `clinical_documentation` | conditions by category and clinical status, allergies, procedures, vitals incl. BP components |
| `billing_claim` | coverage, account, charges, claim, and waits for the payer's ClaimResponse; `Claim/$submit` |
| `subscription_roundtrip` | registers a rest-hook Subscription on the peer and verifies notifications reach `/hooks` |
| `messaging_adt_orders` | A04/A01/O21/R01/A03 messages acknowledged `ok`; malformed message rejected |

Scenario steps are `action` (a workflow), `request` (raw FHIR call), `expect` (status, headers, FHIRPath), `assert`,
`wait_for` (a notification or a search result), `subscribe`, `set`, `sleep` and `validate`. Values interpolate
`${saved.path}`. Steps and scenarios can declare `requires: {resource, interaction, operation, messaging}`. When the
peer's CapabilityStatement doesn't advertise the requirement, the step is **skipped** rather than failed, which
separates "not supported" from "broken". Put your own YAML files in `./scenarios/`; they override library entries
with the same id. Here is an example:

```yaml
name: My HIS - admit shows in census
steps:
  - {action: adt.register_patient, save: reg}
  - {action: adt.admit, args: {patient: "${reg.patient.id}"}, save: adm}
  - name: Census search
    request: {method: GET, path: Encounter, params: {status: in-progress, location: "${adm.encounter.location.0.location.reference}"}}
    expect: {status: 200, fhirpath: ["Bundle.entry.resource.ofType(Encounter).id contains '${adm.encounter.id}'"]}
```

Against the loopback peer the simulator is suppressed so scenarios can drive both sides deterministically.
Scenarios that need it declare `simulator: true`.

## Validation

- Structural validation of every inbound write uses `fhir.resources` models (`validation.inbound: strict|warn|off`).
  The library ships R4B models, which match R4 for all resources used here. Set `warn` if a valid R4-only construct
  is ever rejected.
- For IG/profile conformance, run `sekmet validator download`, then set `validation.validator_jar` and `igs`
  (e.g. `hl7.fhir.us.core#6.1.0`). This enables `$validate`, `sekmet validate --conformance file.json`, and
  `validate` scenario steps.

## Docker

```bash
docker compose up -d                                 # SEKMET on :8090, HAPI FHIR (stand-in HIS) on :8081
docker compose exec sekmet sekmet scenario run all --peer hapi
```

## Layout

```
sekmet/fhir/          store (SQLite + history + search index), search engine, REST router, bundles, capability, validation
sekmet/auth/          inbound guard + SMART token endpoint, outbound auth providers, key tools
sekmet/client/        PeerClient (FHIR REST client with traffic logging)
sekmet/workflows/     hospital workflows, targets (local/rest/messaging), terminology catalogue, simulator
sekmet/messaging/     message Bundle builder/sender, $process-message
sekmet/subscriptions/ rest-hook engine, remote registration, /hooks receiver
sekmet/scenarios/     runner, reports, built-in library
sekmet/ui/            web console (Jinja2 + htmx)
```

`pytest` runs unit, REST, auth (including end-to-end SMART) and every library scenario over real HTTP loopback.
