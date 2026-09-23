"""Search parameter table: resource type -> param -> (kind, element paths).

Paths are dotted element paths walked through lists; choice types are listed explicitly
(e.g. effectiveDateTime|effectivePeriod). Only parameters used by hospital flows are defined;
anything else is reported back as unsupported rather than silently ignored.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchParam:
    name: str
    kind: str  # string | token | reference | date | number | uri
    paths: tuple[str, ...]
    target: str | None = None  # reference param restricted to one target type (e.g. patient -> Patient)


_DEFS: dict[str, str] = {
    "*": """
        _lastUpdated:date:meta.lastUpdated
        _tag:token:meta.tag
        _profile:uri:meta.profile
        _security:token:meta.security
        _source:uri:meta.source
    """,
    "Patient": """
        identifier:token:identifier
        name:string:name
        family:string:name.family
        given:string:name.given
        birthdate:date:birthDate
        gender:token:gender
        phone:token:telecom[phone]
        email:token:telecom[email]
        telecom:token:telecom
        address:string:address
        address-city:string:address.city
        address-postalcode:string:address.postalCode
        address-country:string:address.country
        active:token:active
        deceased:token:deceasedBoolean
        general-practitioner:reference:generalPractitioner
        organization:reference:managingOrganization
        link:reference:link.other
    """,
    "Practitioner": """
        identifier:token:identifier
        name:string:name
        family:string:name.family
        given:string:name.given
        active:token:active
        telecom:token:telecom
    """,
    "PractitionerRole": """
        identifier:token:identifier
        practitioner:reference:practitioner
        organization:reference:organization
        location:reference:location
        role:token:code
        specialty:token:specialty
        service:reference:healthcareService
        active:token:active
    """,
    "Organization": """
        identifier:token:identifier
        name:string:name|alias
        type:token:type
        active:token:active
        partof:reference:partOf
    """,
    "Location": """
        identifier:token:identifier
        name:string:name|alias
        status:token:status
        operational-status:token:operationalStatus
        type:token:type
        physical-type:token:physicalType
        organization:reference:managingOrganization
        partof:reference:partOf
    """,
    "HealthcareService": """
        identifier:token:identifier
        name:string:name
        organization:reference:providedBy
        location:reference:location
        service-type:token:type
        specialty:token:specialty
        active:token:active
    """,
    "RelatedPerson": """
        identifier:token:identifier
        patient:reference:patient
        name:string:name
        relationship:token:relationship
    """,
    "Endpoint": """
        identifier:token:identifier
        name:string:name
        status:token:status
        organization:reference:managingOrganization
        connection-type:token:connectionType
    """,
    "Encounter": """
        identifier:token:identifier
        status:token:status
        class:token:class
        type:token:type
        subject:reference:subject
        patient:reference:subject:Patient
        date:date:period
        location:reference:location.location
        location-period:date:location.period
        participant:reference:participant.individual
        practitioner:reference:participant.individual:Practitioner
        participant-type:token:participant.type
        service-provider:reference:serviceProvider
        episode-of-care:reference:episodeOfCare
        based-on:reference:basedOn
        appointment:reference:appointment
        part-of:reference:partOf
        reason-code:token:reasonCode
        account:reference:account
        special-arrangement:token:hospitalization.specialArrangement
    """,
    "EpisodeOfCare": """
        identifier:token:identifier
        patient:reference:patient
        status:token:status
        type:token:type
        date:date:period
        organization:reference:managingOrganization
        care-manager:reference:careManager
    """,
    "ServiceRequest": """
        identifier:token:identifier
        requisition:token:requisition
        status:token:status
        intent:token:intent
        priority:token:priority
        code:token:code
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        requester:reference:requester
        performer:reference:performer
        authored:date:authoredOn
        occurrence:date:occurrenceDateTime|occurrencePeriod
        based-on:reference:basedOn
        replaces:reference:replaces
        specimen:reference:specimen
    """,
    "Task": """
        identifier:token:identifier
        group-identifier:token:groupIdentifier
        status:token:status
        business-status:token:businessStatus
        intent:token:intent
        priority:token:priority
        code:token:code
        focus:reference:focus
        subject:reference:for
        patient:reference:for:Patient
        owner:reference:owner
        requester:reference:requester
        performer:token:performerType
        based-on:reference:basedOn
        part-of:reference:partOf
        encounter:reference:encounter
        authored-on:date:authoredOn
        modified:date:lastModified
        period:date:executionPeriod
    """,
    "Specimen": """
        identifier:token:identifier
        accession:token:accessionIdentifier
        subject:reference:subject
        patient:reference:subject:Patient
        type:token:type
        status:token:status
        collected:date:collection.collectedDateTime|collection.collectedPeriod
        collector:reference:collection.collector
        parent:reference:parent
    """,
    "Observation": """
        identifier:token:identifier
        status:token:status
        code:token:code
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        date:date:effectiveDateTime|effectivePeriod|effectiveInstant
        based-on:reference:basedOn
        part-of:reference:partOf
        performer:reference:performer
        specimen:reference:specimen
        derived-from:reference:derivedFrom
        has-member:reference:hasMember
        value-quantity:number:valueQuantity.value
        value-concept:token:valueCodeableConcept
        value-string:string:valueString
        component-code:token:component.code
        combo-code:token:code|component.code
        data-absent-reason:token:dataAbsentReason
    """,
    "DiagnosticReport": """
        identifier:token:identifier
        status:token:status
        code:token:code
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        date:date:effectiveDateTime|effectivePeriod
        issued:date:issued
        based-on:reference:basedOn
        result:reference:result
        performer:reference:performer
        results-interpreter:reference:resultsInterpreter
        specimen:reference:specimen
        conclusion:token:conclusionCode
        media:reference:media.link
    """,
    "ImagingStudy": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        basedon:reference:basedOn
        modality:token:series.modality
        started:date:started
        series:uri:series.uid
        instance:uri:series.instance.uid
        referrer:reference:referrer
        endpoint:reference:endpoint|series.endpoint
    """,
    "Media": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        based-on:reference:basedOn
        created:date:createdDateTime|createdPeriod
    """,
    "DocumentReference": """
        identifier:token:identifier
        status:token:status
        type:token:type
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:context.encounter
        date:date:date
        period:date:context.period
        author:reference:author
        related:reference:context.related
        relatesto:reference:relatesTo.target
        contenttype:token:content.attachment.contentType
    """,
    "Schedule": """
        identifier:token:identifier
        actor:reference:actor
        active:token:active
        date:date:planningHorizon
        service-type:token:serviceType
        specialty:token:specialty
        service-category:token:serviceCategory
    """,
    "Slot": """
        identifier:token:identifier
        schedule:reference:schedule
        status:token:status
        start:date:start
        service-type:token:serviceType
        specialty:token:specialty
        appointment-type:token:appointmentType
        service-category:token:serviceCategory
    """,
    "Appointment": """
        identifier:token:identifier
        status:token:status
        actor:reference:participant.actor
        patient:reference:participant.actor:Patient
        practitioner:reference:participant.actor:Practitioner
        location:reference:participant.actor:Location
        part-status:token:participant.status
        date:date:start
        slot:reference:slot
        service-type:token:serviceType
        service-category:token:serviceCategory
        appointment-type:token:appointmentType
        specialty:token:specialty
        based-on:reference:basedOn
        reason-code:token:reasonCode
        reason-reference:reference:reasonReference
        supporting-info:reference:supportingInformation
    """,
    "AppointmentResponse": """
        identifier:token:identifier
        appointment:reference:appointment
        actor:reference:actor
        patient:reference:actor:Patient
        practitioner:reference:actor:Practitioner
        location:reference:actor:Location
        part-status:token:participantStatus
    """,
    "Medication": """
        identifier:token:identifier
        code:token:code
        status:token:status
        form:token:form
        manufacturer:reference:manufacturer
    """,
    "MedicationRequest": """
        identifier:token:identifier
        status:token:status
        intent:token:intent
        priority:token:priority
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        code:token:medicationCodeableConcept
        medication:reference:medicationReference
        requester:reference:requester
        authoredon:date:authoredOn
        intended-dispenser:reference:dispenseRequest.performer
        intended-performer:reference:performer
        date:date:dosageInstruction.timing.event
    """,
    "MedicationDispense": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        context:reference:context
        prescription:reference:authorizingPrescription
        code:token:medicationCodeableConcept
        medication:reference:medicationReference
        performer:reference:performer.actor
        receiver:reference:receiver
        destination:reference:destination
        whenhandedover:date:whenHandedOver
        whenprepared:date:whenPrepared
        type:token:type
    """,
    "MedicationAdministration": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        context:reference:context
        request:reference:request
        code:token:medicationCodeableConcept
        medication:reference:medicationReference
        performer:reference:performer.actor
        effective-time:date:effectiveDateTime|effectivePeriod
        reason-given:token:reasonCode
    """,
    "MedicationStatement": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        context:reference:context
        code:token:medicationCodeableConcept
        medication:reference:medicationReference
        effective:date:effectiveDateTime|effectivePeriod
    """,
    "Condition": """
        identifier:token:identifier
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        code:token:code
        category:token:category
        clinical-status:token:clinicalStatus
        verification-status:token:verificationStatus
        severity:token:severity
        onset-date:date:onsetDateTime|onsetPeriod
        abatement-date:date:abatementDateTime|abatementPeriod
        recorded-date:date:recordedDate
        asserter:reference:asserter
        body-site:token:bodySite
    """,
    "AllergyIntolerance": """
        identifier:token:identifier
        patient:reference:patient
        code:token:code|reaction.substance
        clinical-status:token:clinicalStatus
        verification-status:token:verificationStatus
        category:token:category
        criticality:token:criticality
        type:token:type
        date:date:recordedDate
        onset:date:reaction.onset
        manifestation:token:reaction.manifestation
        severity:token:reaction.severity
        recorder:reference:recorder
        asserter:reference:asserter
    """,
    "Procedure": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        code:token:code
        category:token:category
        date:date:performedDateTime|performedPeriod
        based-on:reference:basedOn
        part-of:reference:partOf
        performer:reference:performer.actor
        location:reference:location
        reason-code:token:reasonCode
    """,
    "Immunization": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        vaccine-code:token:vaccineCode
        date:date:occurrenceDateTime
        lot-number:string:lotNumber
        performer:reference:performer.actor
        location:reference:location
    """,
    "CarePlan": """
        identifier:token:identifier
        status:token:status
        intent:token:intent
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        category:token:category
        date:date:period
        based-on:reference:basedOn
    """,
    "Communication": """
        identifier:token:identifier
        status:token:status
        category:token:category
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        sender:reference:sender
        recipient:reference:recipient
        sent:date:sent
        received:date:received
        based-on:reference:basedOn
        part-of:reference:partOf
    """,
    "CommunicationRequest": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        requester:reference:requester
        recipient:reference:recipient
        authored:date:authoredOn
    """,
    "Coverage": """
        identifier:token:identifier
        status:token:status
        type:token:type
        beneficiary:reference:beneficiary
        patient:reference:beneficiary:Patient
        subscriber:reference:subscriber
        policy-holder:reference:policyHolder
        payor:reference:payor
        class-value:string:class.value
        class-type:token:class.type
        dependent:string:dependent
    """,
    "CoverageEligibilityRequest": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        provider:reference:provider
        created:date:created
    """,
    "CoverageEligibilityResponse": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        request:reference:request
        outcome:token:outcome
        created:date:created
    """,
    "Account": """
        identifier:token:identifier
        status:token:status
        type:token:type
        name:string:name
        subject:reference:subject
        patient:reference:subject:Patient
        owner:reference:owner
        period:date:servicePeriod
    """,
    "ChargeItem": """
        identifier:token:identifier
        status:token:status
        code:token:code
        subject:reference:subject
        patient:reference:subject:Patient
        context:reference:context
        account:reference:account
        entered-date:date:enteredDate
        occurrence:date:occurrenceDateTime|occurrencePeriod
        performer-actor:reference:performer.actor
        service:reference:service
        price-override:number:priceOverride.value
    """,
    "Claim": """
        identifier:token:identifier
        status:token:status
        use:token:use
        patient:reference:patient
        created:date:created
        provider:reference:provider
        insurer:reference:insurer
        enterer:reference:enterer
        facility:reference:facility
        priority:token:priority
        encounter:reference:item.encounter
        care-team:reference:careTeam.provider
        payee:reference:payee.party
    """,
    "ClaimResponse": """
        identifier:token:identifier
        status:token:status
        use:token:use
        patient:reference:patient
        request:reference:request
        outcome:token:outcome
        disposition:string:disposition
        created:date:created
        insurer:reference:insurer
        requestor:reference:requestor
        payment-date:date:payment.date
    """,
    "ExplanationOfBenefit": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        claim:reference:claim
        provider:reference:provider
        created:date:created
        encounter:reference:item.encounter
    """,
    "Invoice": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        account:reference:account
        date:date:date
        issuer:reference:issuer
    """,
    "Subscription": """
        status:token:status
        criteria:string:criteria
        type:token:channel.type
        url:uri:channel.endpoint
        payload:token:channel.payload
        contact:token:contact
    """,
    "MessageHeader": """
        event:token:eventCoding
        source:string:source.name
        source-uri:uri:source.endpoint
        destination:string:destination.name
        destination-uri:uri:destination.endpoint
        focus:reference:focus
        code:token:response.code
        response-id:token:response.identifier
        sender:reference:sender
    """,
    "Bundle": """
        identifier:token:identifier
        type:token:type
        timestamp:date:timestamp
    """,
    "Provenance": """
        target:reference:target
        patient:reference:target:Patient
        agent:reference:agent.who
        recorded:date:recorded
        entity:reference:entity.what
    """,
    "AuditEvent": """
        type:token:type
        subtype:token:subtype
        action:token:action
        date:date:recorded
        agent:reference:agent.who
        entity:reference:entity.what
        patient:reference:entity.what:Patient
        outcome:token:outcome
    """,
    "Flag": """
        identifier:token:identifier
        status:token:status
        subject:reference:subject
        patient:reference:subject:Patient
        encounter:reference:encounter
        date:date:period
    """,
    "Consent": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        category:token:category
        date:date:dateTime
    """,
    "Device": """
        identifier:token:identifier
        status:token:status
        patient:reference:patient
        type:token:type
        organization:reference:owner
        location:reference:location
    """,
    "Basic": """
        identifier:token:identifier
        code:token:code
        subject:reference:subject
        author:reference:author
        created:date:created
    """,
    "Group": """
        identifier:token:identifier
        type:token:type
        member:reference:member.entity
        name:string:name
    """,
}

# params that are handled specially by the search engine (not index-backed)
SPECIAL = {"_id", "_count", "_offset", "_sort", "_include", "_revinclude", "_summary", "_elements",
           "_total", "_format", "_pretty", "_type", "_has", "_contained", "_containedType", "_since", "_at"}


def _parse() -> dict[str, dict[str, SearchParam]]:
    table: dict[str, dict[str, SearchParam]] = {}
    for rtype, block in _DEFS.items():
        params: dict[str, SearchParam] = {}
        for line in block.strip().splitlines():
            bits = line.strip().split(":")
            name, kind, paths = bits[0], bits[1], tuple(bits[2].split("|"))
            target = bits[3] if len(bits) > 3 else None
            params[name] = SearchParam(name, kind, paths, target)
        table[rtype] = params
    return table


PARAMS = _parse()


def params_for(resource_type: str) -> dict[str, SearchParam]:
    """All parameters available for a type (type-specific + common)."""
    return {**PARAMS["*"], **PARAMS.get(resource_type, {})}


def get_param(resource_type: str, name: str) -> SearchParam | None:
    return PARAMS.get(resource_type, {}).get(name) or PARAMS["*"].get(name)


def supported_types() -> list[str]:
    return sorted(t for t in PARAMS if t != "*")
