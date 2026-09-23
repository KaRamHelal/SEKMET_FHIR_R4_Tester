# Contributing to SEKMET

Thanks for helping make FHIR integrations easier to test. Useful contributions include:

- **Scenarios** for hospital flows that aren't covered yet (YAML in `sekmet/scenarios/library/`).
- **Workflows** (`sekmet/workflows/`): new resource flows, using standard terminology only.
- **Findings from real systems.** If SEKMET misjudged a server, that's a bug in SEKMET. If a server deviates from
  the spec, add it to the behaviour matrix in `docs/testing-log.md`. Public servers only: no customer names,
  endpoints, credentials or patient data.
- **Protocol coverage**: FHIR features not implemented yet.

## Development

```bash
git clone https://github.com/KaRamHelal/SEKMET_FHIR_R4_Tester.git && cd SEKMET_FHIR_R4_Tester
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # unit, REST, auth (incl. end-to-end SMART), TestScript, load and every scenario over loopback
```

## Principles

1. **Stay generic.** SEKMET tests any HIS, so never adapt it to one vendor's quirks. Grade the behaviour instead:
   `level: should`, `requires:` capability gates, or a documented finding.
2. **Portable assertions.** Count results with `entry.resource.ofType(X)`, make payloads unique with `${uid}`, and
   don't assume the reference form. See the portability rules in `docs/scenarios.md`.
3. **Every bug fix gets a regression test,** and every new feature gets tests plus a loopback scenario where it
   makes sense.
4. **Keep the docs current:** `docs/operations.md` for anything operational, `docs/scenarios.md` for scenarios.
5. **Never commit secrets or captured data.** `tests/test_repo_hygiene.py` enforces this.

## Pull requests

- Keep them focused, and describe what you tested (which peers, which scenarios).
- `pytest -q` must pass.
- New settings need an entry in `config/settings.example.yaml` and the configuration reference.
