"""Shared FHIR helpers: R4 resource list, OperationOutcome, errors, time and reference utilities."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

FHIR_VERSION = "4.0.1"
FHIR_JSON = "application/fhir+json"

R4_RESOURCE_TYPES = frozenset("""
Account ActivityDefinition AdverseEvent AllergyIntolerance Appointment AppointmentResponse AuditEvent Basic
Binary BiologicallyDerivedProduct BodyStructure Bundle CapabilityStatement CarePlan CareTeam CatalogEntry
ChargeItem ChargeItemDefinition Claim ClaimResponse ClinicalImpression CodeSystem Communication
CommunicationRequest CompartmentDefinition Composition ConceptMap Condition Consent Contract Coverage
CoverageEligibilityRequest CoverageEligibilityResponse DetectedIssue Device DeviceDefinition DeviceMetric
DeviceRequest DeviceUseStatement DiagnosticReport DocumentManifest DocumentReference EffectEvidenceSynthesis
Encounter Endpoint EnrollmentRequest EnrollmentResponse EpisodeOfCare EventDefinition Evidence EvidenceVariable
ExampleScenario ExplanationOfBenefit FamilyMemberHistory Flag Goal GraphDefinition Group GuidanceResponse
HealthcareService ImagingStudy Immunization ImmunizationEvaluation ImmunizationRecommendation
ImplementationGuide InsurancePlan Invoice Library Linkage List Location Measure MeasureReport Media Medication
MedicationAdministration MedicationDispense MedicationKnowledge MedicationRequest MedicationStatement
MedicinalProduct MedicinalProductAuthorization MedicinalProductContraindication MedicinalProductIndication
MedicinalProductIngredient MedicinalProductInteraction MedicinalProductManufactured MedicinalProductPackaged
MedicinalProductPharmaceutical MedicinalProductUndesirableEffect MessageDefinition MessageHeader
MolecularSequence NamingSystem NutritionOrder Observation ObservationDefinition OperationDefinition
OperationOutcome Organization OrganizationAffiliation Parameters Patient PaymentNotice PaymentReconciliation
Person PlanDefinition Practitioner PractitionerRole Procedure Provenance Questionnaire QuestionnaireResponse
RelatedPerson RequestGroup ResearchDefinition ResearchElementDefinition ResearchStudy ResearchSubject
RiskAssessment RiskEvidenceSynthesis Schedule SearchParameter ServiceRequest Slot Specimen SpecimenDefinition
StructureDefinition StructureMap Subscription Substance SubstanceNucleicAcid SubstancePolymer SubstanceProtein
SubstanceReferenceInformation SubstanceSourceMaterial SubstanceSpecification SupplyDelivery SupplyRequest Task
TerminologyCapabilities TestReport TestScript ValueSet VerificationResult VisionPrescription
""".split())

ID_RE = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class FhirError(Exception):
    """Raised anywhere in request handling; rendered as an OperationOutcome."""

    def __init__(self, status: int, message: str, code: str = "processing", severity: str = "error",
                 expression: list[str] | None = None, issues: list[dict] | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}
        self.issues = issues or [issue(severity, code, message, expression)]

    def outcome(self) -> dict:
        return operation_outcome(self.issues)


def issue(severity: str, code: str, diagnostics: str, expression: list[str] | None = None) -> dict:
    i: dict[str, Any] = {"severity": severity, "code": code, "diagnostics": diagnostics}
    if expression:
        i["expression"] = expression
    return i


def operation_outcome(issues: list[dict] | None = None, text: str | None = None) -> dict:
    oo: dict[str, Any] = {"resourceType": "OperationOutcome", "id": new_id()}
    oo["issue"] = issues or [issue("information", "informational", text or "All OK")]
    return oo


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


def instant(dt: datetime | None = None) -> str:
    """FHIR instant with millisecond precision in UTC."""
    dt = (dt or now()).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def http_date(ts: str) -> str:
    from email.utils import format_datetime
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def ref(resource: dict) -> dict:
    """Build a Reference to a resource dict that has resourceType + id."""
    r: dict[str, Any] = {"reference": f"{resource['resourceType']}/{resource['id']}"}
    disp = display_of(resource)
    if disp:
        r["display"] = disp
    return r


def display_of(resource: dict) -> str | None:
    names = resource.get("name")
    if isinstance(names, list) and names:
        n = names[0]
        if n.get("text"):
            return n["text"]
        parts = [*n.get("given", []), n.get("family", "")]
        return " ".join(p for p in parts if p) or None
    if isinstance(names, str):
        return names
    code = resource.get("code")
    if isinstance(code, dict):
        return code.get("text") or next((c.get("display") for c in code.get("coding", []) if c.get("display")), None)
    return None


REF_RE = re.compile(r"^(?:(?P<base>.*?)/)?(?P<type>[A-Z][A-Za-z]+)/(?P<id>[A-Za-z0-9\-.]{1,64})(?:/_history/(?P<vid>[^/]+))?$")


def parse_reference(value: str, base_url: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Return (type, id, version) for a relative or absolute literal reference; (None, None, None) otherwise."""
    if not value or value.startswith(("urn:", "#")):
        return None, None, None
    m = REF_RE.match(value)
    if not m or m.group("type") not in R4_RESOURCE_TYPES:
        return None, None, None
    return m.group("type"), m.group("id"), m.group("vid")


def walk_references(node: Any) -> Iterator[dict]:
    """Yield every dict with a string 'reference' key (Reference datatypes) in a resource tree."""
    if isinstance(node, dict):
        if isinstance(node.get("reference"), str):
            yield node
        for v in node.values():
            yield from walk_references(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_references(v)


def codeable(system: str, code: str, display: str | None = None, text: str | None = None) -> dict:
    c: dict[str, Any] = {"system": system, "code": code}
    if display:
        c["display"] = display
    cc: dict[str, Any] = {"coding": [c]}
    if text or display:
        cc["text"] = text or display
    return cc


def first_identifier(resource: dict, system: str | None = None) -> dict | None:
    for ident in resource.get("identifier", []) or []:
        if system is None or ident.get("system") == system:
            return ident
    return None
