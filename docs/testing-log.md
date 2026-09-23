# Testing log: what testing SEKMET against real servers taught us

Each round runs the scenario library against one independent FHIR implementation. Findings are split into
**SEKMET fixes** (bugs or gaps in the tester, fixed in code) and **peer behaviour** (reported, never adapted to).
Only public test servers are recorded here.

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
