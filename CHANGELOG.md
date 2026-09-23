# Changelog

## 0.1.1

- `sekmet --version` / `-V`, and `sekmet.__version__` read from the package metadata.
- Releases now publish from GitHub Actions via PyPI Trusted Publishing (no API tokens).

## 0.1.0 (first public release)

- **FHIR R4 server:** CRUD, versioning, conditional operations, transactions/batch, search (chaining, `_has`,
  includes, modifiers, prefixes, paging), JSON and XML, `$everything`, `$validate`, `$process-message`,
  `Claim/$submit`, Bulk Data `$export`, Subscriptions (R4 rest-hook and R4 Backport topics with `$status`),
  SMART Backend Services, Basic and bearer auth, HTTPS.
- **Client and workflows:** 40 hospital workflows (ADT, lab/imaging orders and results, scheduling, medications,
  clinical, billing) targeting a local store, a REST peer or a messaging peer. Auth: SMART JWT, OAuth2 client
  credentials, Basic, bearer.
- **Simulator** that plays lab, RIS, scheduler, pharmacy and payer for inbound work.
- **Scenario runner:** 14 library scenarios, capability-aware skipping, SHALL/SHOULD grading, peer roles, HTML,
  JSON and JUnit reports.
- **FHIR TestScript engine** with TestReport output, and export of runs as replayable TestScripts.
- **Load mode:** parallel user journeys, percentiles, correctness under concurrency, CI gates.
- **Web console**, traffic inspector with redaction, `scripts/inferno.sh` for running Inferno kits against SEKMET.
- Tested against HAPI, Firely, Spark, WildFHIR, fhir-candle and the Inferno SMART kit
  (see `docs/testing-log.md`).
