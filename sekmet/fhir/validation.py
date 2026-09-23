"""Structural validation (fhir.resources R4B models) and optional HL7 validator_cli.jar conformance checks."""
from __future__ import annotations

import json
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from .common import R4_RESOURCE_TYPES, issue, operation_outcome

VALIDATOR_URL = "https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar"


@lru_cache(maxsize=None)
def _model(resource_type: str):
    from fhir.resources.R4B import get_fhir_model_class
    try:
        return get_fhir_model_class(resource_type)
    except (KeyError, ValueError, ImportError, AttributeError):
        return None


def structural_issues(resource: dict) -> list[dict]:
    """Return OperationOutcome issues (empty when valid)."""
    if not isinstance(resource, dict):
        return [issue("error", "structure", "Body is not a JSON object")]
    rtype = resource.get("resourceType")
    if not rtype:
        return [issue("error", "required", "Missing resourceType")]
    if rtype not in R4_RESOURCE_TYPES:
        return [issue("error", "not-supported", f"'{rtype}' is not a FHIR R4 resource type")]
    model = _model(rtype)
    if model is None:
        return [issue("information", "informational", f"No structural model available for {rtype}; not validated")]
    try:
        model.model_validate(resource)
    except ValidationError as e:
        out = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
            expr = f"{rtype}.{loc}" if loc else rtype
            out.append(issue("error", "structure", f"{err.get('msg')} ({expr})", [expr]))
        return out
    except Exception as e:  # model-level validators may raise other errors
        return [issue("error", "structure", str(e))]
    return []


def has_errors(issues: list[dict]) -> bool:
    return any(i.get("severity") in ("error", "fatal") for i in issues)


def run_hl7_validator(resource: dict, jar: str, igs: list[str] | None = None, tx: str | None = "n/a",
                      java: str = "java", profile: str | None = None, timeout: int = 300) -> dict:
    """Run the official HL7 validator on a resource and return its OperationOutcome."""
    if not Path(jar).exists():
        return operation_outcome([issue("error", "not-found", f"validator jar not found at {jar}; run `sekmet validator download`")])
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"{resource.get('resourceType', 'resource')}.json"
        out = Path(td) / "out.json"
        src.write_text(json.dumps(resource))
        cmd = [java, "-jar", jar, str(src), "-version", "4.0.1", "-output", str(out)]
        for ig in igs or []:
            cmd += ["-ig", ig]
        if tx:
            cmd += ["-tx", tx]
        if profile:
            cmd += ["-profile", profile]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return operation_outcome([issue("error", "exception", f"validator failed to run: {e}")])
        if out.exists():
            try:
                return json.loads(out.read_text())
            except json.JSONDecodeError:
                pass
        return operation_outcome([issue("error", "exception", (proc.stdout + proc.stderr)[-4000:])])


def validate(resource: dict, settings=None, conformance: bool = False, profile: str | None = None) -> dict:
    """Structural validation always; HL7 validator too when conformance=True and a jar is configured."""
    issues = structural_issues(resource)
    if conformance and settings and settings.validation.validator_jar:
        v = settings.validation
        oo = run_hl7_validator(resource, v.validator_jar, v.igs, v.tx_server, v.java, profile)
        issues += oo.get("issue", [])
    return operation_outcome(issues or None, text="Validation successful")
