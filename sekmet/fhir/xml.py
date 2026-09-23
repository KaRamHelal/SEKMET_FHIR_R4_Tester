"""FHIR XML <-> JSON conversion via the fhir.resources models (element order and cardinality come from the
structure definitions, which a generic converter cannot know)."""
from __future__ import annotations

from functools import lru_cache

from lxml import etree

from .common import FhirError

FHIR_XML = "application/fhir+xml"
FHIR_NS = "http://hl7.org/fhir"
XML_TYPES = ("application/fhir+xml", "application/xml", "text/xml")


@lru_cache(maxsize=None)
def _model(rtype: str):
    from fhir.resources.R4B import get_fhir_model_class
    try:
        return get_fhir_model_class(rtype)
    except (KeyError, ValueError, ImportError, AttributeError):
        return None


def to_xml(resource: dict) -> bytes:
    """Serialize a FHIR JSON resource as FHIR XML."""
    model = _model(resource.get("resourceType", ""))
    if model is None:
        raise FhirError(406, f"Cannot render {resource.get('resourceType')} as XML", "not-supported")
    try:
        out = model.model_validate(resource).model_dump_xml()
    except Exception as e:
        raise FhirError(500, f"XML serialization failed: {e}", "exception")
    return out if isinstance(out, bytes) else out.encode()


def from_xml(data: bytes | str) -> dict:
    """Parse FHIR XML into FHIR JSON (dict). Raises FhirError(400) on malformed or non-conformant XML."""
    raw = data.encode() if isinstance(data, str) else data
    try:
        root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as e:
        raise FhirError(400, f"Malformed XML: {e}", "structure")
    qn = etree.QName(root)
    if qn.namespace != FHIR_NS:
        raise FhirError(400, f"Root element must be in the FHIR namespace {FHIR_NS}", "structure")
    model = _model(qn.localname)
    if model is None:
        raise FhirError(400, f"Unknown resource type '{qn.localname}'", "not-supported")
    try:
        obj = model.model_validate_xml(raw)
    except Exception as e:
        raise FhirError(400, f"XML does not match the {qn.localname} structure: {str(e)[:500]}", "structure")
    return obj.model_dump(mode="json", exclude_none=True, by_alias=True)


def wants_xml(accept: str | None, fmt: str | None) -> bool:
    """True when the client asks for XML via _format or Accept (JSON wins if both are acceptable)."""
    if fmt:
        return "xml" in fmt.lower()
    accept = (accept or "").lower()
    if not accept:
        return False
    return any(t in accept for t in XML_TYPES) and "json" not in accept
