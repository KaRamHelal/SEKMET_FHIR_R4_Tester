"""Small terminology catalogue used to build realistic resources (LOINC, SNOMED CT, RxNorm, HL7 code systems)."""
from __future__ import annotations

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
UCUM = "http://unitsofmeasure.org"
V3_ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V2_0003 = "http://terminology.hl7.org/CodeSystem/v2-0003"
INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
COND_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
COND_VER = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
COND_CAT = "http://terminology.hl7.org/CodeSystem/condition-category"
ALLERGY_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
ALLERGY_VER = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
PARTICIPATION = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"
DISCHARGE = "http://terminology.hl7.org/CodeSystem/discharge-disposition"
ADMIT_SOURCE = "http://terminology.hl7.org/CodeSystem/admit-source"
LOC_PHYSICAL = "http://terminology.hl7.org/CodeSystem/location-physical-type"
ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
DICOM = "http://dicom.nema.org/resources/ontology/DCM"
SERVICE_TYPE = "http://terminology.hl7.org/CodeSystem/service-type"
COVERAGE_CLASS = "http://terminology.hl7.org/CodeSystem/coverage-class"
CLAIM_TYPE = "http://terminology.hl7.org/CodeSystem/claim-type"
PROCESS_PRIORITY = "http://terminology.hl7.org/CodeSystem/processpriority"
ADJUDICATION = "http://terminology.hl7.org/CodeSystem/adjudication"
PAYEE_TYPE = "http://terminology.hl7.org/CodeSystem/payeetype"
CHARGE_CODES = "urn:oid:1.2.3.4.5.200"  # local charge master
MED_REQ_CAT = "http://terminology.hl7.org/CodeSystem/medicationrequest-category"
ROUTE = SNOMED

# panel/test code -> (display, [(member loinc, display, unit, low, high, decimals)])
LAB_TESTS: dict[str, tuple[str, list[tuple[str, str, str, float, float, int]]]] = {
    "58410-2": ("CBC panel - Blood by Automated count", [
        ("718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL", 12.0, 17.5, 1),
        ("6690-2", "Leukocytes [#/volume] in Blood by Automated count", "10*3/uL", 4.0, 11.0, 1),
        ("777-3", "Platelets [#/volume] in Blood by Automated count", "10*3/uL", 150, 400, 0),
        ("4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count", "%", 36, 50, 1),
    ]),
    "51990-0": ("Basic metabolic panel - Blood", [
        ("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", 70, 99, 0),
        ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", 0.6, 1.3, 2),
        ("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L", 135, 145, 0),
        ("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L", 3.5, 5.1, 1),
    ]),
    "2345-7": ("Glucose [Mass/volume] in Serum or Plasma", [
        ("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", 70, 99, 0)]),
    "4548-4": ("Hemoglobin A1c/Hemoglobin.total in Blood", [
        ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", 4.0, 5.6, 1)]),
    "2160-0": ("Creatinine [Mass/volume] in Serum or Plasma", [
        ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", 0.6, 1.3, 2)]),
}

# imaging order code -> (display, DICOM modality, body site snomed, display)
IMAGING: dict[str, tuple[str, str, str, str]] = {
    "36643-5": ("XR Chest 2 Views", "DX", "51185008", "Thoracic structure"),
    "24627-2": ("CT Chest", "CT", "51185008", "Thoracic structure"),
    "24558-9": ("CT Head WO contrast", "CT", "69536005", "Head structure"),
    "30704-1": ("US Abdomen", "US", "818983003", "Abdomen"),
}

# RxNorm code -> (display, dose value, dose unit, frequency/day, route snomed, route display)
MEDICATIONS: dict[str, tuple[str, float, str, int, str, str]] = {
    "197361": ("Amlodipine 5 MG Oral Tablet", 1, "tablet", 1, "26643006", "Oral route"),
    "313782": ("Acetaminophen 325 MG Oral Tablet", 2, "tablet", 3, "26643006", "Oral route"),
    "860975": ("24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet", 1, "tablet", 1, "26643006", "Oral route"),
}

CONDITIONS = {
    "38341003": "Hypertensive disorder, systemic arterial",
    "44054006": "Diabetes mellitus type 2",
    "233604007": "Pneumonia",
    "195967001": "Asthma",
}

ALLERGIES = {
    "91936005": ("Allergy to penicillin", "medication"),
    "300916003": ("Latex allergy", "environment"),
    "91935009": ("Allergy to peanuts", "food"),
}

PROCEDURES = {
    "80146002": "Appendectomy",
    "73761001": "Colonoscopy",
    "387713003": "Surgical procedure",
}

VITALS = {
    "8867-4": ("Heart rate", "/min", 60, 100, 0),
    "8310-5": ("Body temperature", "Cel", 36.4, 37.4, 1),
    "29463-7": ("Body weight", "kg", 55, 95, 1),
    "9279-1": ("Respiratory rate", "/min", 12, 20, 0),
    "59408-5": ("Oxygen saturation in Arterial blood by Pulse oximetry", "%", 94, 100, 0),
}

CHARGES = {
    "BED-DAY": ("Inpatient bed day", 450.00),
    "LAB-CBC": ("Complete blood count", 35.00),
    "LAB-BMP": ("Basic metabolic panel", 42.00),
    "RAD-XR": ("Radiograph", 120.00),
    "CONSULT": ("Physician consultation", 150.00),
    "PHARM": ("Pharmacy dispense", 25.00),
}

# Business events -> (system, code, display). v2 trigger events are the lingua franca of HIS integration.
EVENTS = {
    "patient-register": (V2_0003, "A04", "ADT/ACK - Register a patient"),
    "patient-update": (V2_0003, "A08", "ADT/ACK - Update patient information"),
    "patient-merge": (V2_0003, "A40", "ADT/ACK - Merge patient - patient identifier list"),
    "admit": (V2_0003, "A01", "ADT/ACK - Admit/visit notification"),
    "transfer": (V2_0003, "A02", "ADT/ACK - Transfer a patient"),
    "discharge": (V2_0003, "A03", "ADT/ACK - Discharge/end visit"),
    "cancel-admit": (V2_0003, "A11", "ADT/ACK - Cancel admit/visit notification"),
    "order": (V2_0003, "O21", "OML - Laboratory order"),
    "imaging-order": (V2_0003, "O23", "OMI - Imaging order"),
    "order-cancel": (V2_0003, "O21", "OML - Laboratory order (cancel)"),
    "result": (V2_0003, "R01", "ORU/ACK - Unsolicited transmission of an observation message"),
    "appointment-book": (V2_0003, "S12", "SIU/ACK - Notification of new appointment booking"),
    "appointment-cancel": (V2_0003, "S15", "SIU/ACK - Notification of appointment cancellation"),
    "appointment-reschedule": (V2_0003, "S13", "SIU/ACK - Notification of appointment rescheduling"),
    "prescription": (V2_0003, "O11", "RDE - Pharmacy/treatment encoded order"),
    "dispense": (V2_0003, "O13", "RDS - Pharmacy/treatment dispense"),
    "administration": (V2_0003, "R01", "RAS - Pharmacy/treatment administration"),
    "charge": (V2_0003, "P03", "DFT/ACK - Post detail financial transaction"),
    "claim": (V2_0003, "P03", "DFT/ACK - Post detail financial transaction (claim)"),
    "clinical-update": (V2_0003, "A08", "ADT/ACK - Update patient information (clinical)"),
}
