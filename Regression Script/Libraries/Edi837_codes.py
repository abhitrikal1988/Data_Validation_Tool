# ─────────────────────────────────────────────────────────────────────────────
# Edi837_codes.py
# Code lists and named qualifier columns for the EDI837 file format
# (005010X222A1 Professional, 005010X223A2 Institutional, 005010X224A2 Dental).
#
# Defaults only. Extend or override per test case through FileConfig:
#     "CodeLists"       : { "SBR09": { "MC": "Medicaid", ... }, ... }
#     "NamedQualifiers" : { "CLAIM_REF": { "G1": "PRIOR_AUTH_NUMBER" }, ... }
# so new codes or qualifiers never need a parser change.
# ─────────────────────────────────────────────────────────────────────────────

_RELATIONSHIP = {
    "01": "Spouse", "18": "Self", "19": "Child", "20": "Employee", "21": "Unknown",
    "39": "Organ Donor", "40": "Cadaver Donor", "53": "Life Partner",
    "G8": "Other Relationship",
}

CODE_LISTS = {
    # BHT02 — Transaction Set Purpose Code
    "BHT02": {"00": "Original", "18": "Reissue"},
    # BHT06 — Claim or Encounter Identifier
    "BHT06": {"CH": "Chargeable", "RP": "Reporting", "31": "Subrogation Demand"},
    # NM101 — Entity Identifier Code
    "NM101": {
        "41": "Submitter", "40": "Receiver", "85": "Billing Provider",
        "87": "Pay-to Provider", "PE": "Payee", "IL": "Insured or Subscriber",
        "PR": "Payer", "QC": "Patient", "DN": "Referring Provider",
        "P3": "Primary Care Provider", "82": "Rendering Provider",
        "77": "Service Location", "DQ": "Supervising Physician",
        "PW": "Pickup Address", "45": "Drop-off Location",
        "71": "Attending Physician", "72": "Operating Physician",
        "ZZ": "Mutually Defined (Other Operating Physician)",
        "DD": "Assistant Surgeon", "QB": "Purchase Service Provider",
        "DK": "Ordering Physician",
    },
    # SBR01 — Payer Responsibility Sequence Number Code
    "SBR01": {
        "P": "Primary", "S": "Secondary", "T": "Tertiary",
        "A": "Payer Responsibility Four", "B": "Payer Responsibility Five",
        "C": "Payer Responsibility Six", "D": "Payer Responsibility Seven",
        "E": "Payer Responsibility Eight", "F": "Payer Responsibility Nine",
        "G": "Payer Responsibility Ten", "H": "Payer Responsibility Eleven",
        "U": "Unknown",
    },
    # SBR02 / PAT01 — Individual Relationship Code
    "SBR02": _RELATIONSHIP,
    # SBR05 — Insurance Type Code (Medicare secondary)
    "SBR05": {
        "12": "Medicare Secondary Working Aged Beneficiary or Spouse with Employer Group Health Plan",
        "13": "Medicare Secondary End-Stage Renal Disease Beneficiary in the Mandated Coordination Period with an Employer's Group Health Plan",
        "14": "Medicare Secondary, No-fault Insurance including Auto is Primary",
        "15": "Medicare Secondary Worker's Compensation",
        "16": "Medicare Secondary Public Health Service (PHS) or Other Federal Agency",
        "41": "Medicare Secondary Black Lung",
        "42": "Medicare Secondary Veteran's Administration",
        "43": "Medicare Secondary Disabled Beneficiary Under Age 65 with Large Group Health Plan (LGHP)",
        "47": "Medicare Secondary, Other Liability Insurance is Primary",
    },
    # SBR09 — Claim Filing Indicator Code
    "SBR09": {
        "11": "Other Non-Federal Programs", "12": "Preferred Provider Organization (PPO)",
        "13": "Point of Service (POS)", "14": "Exclusive Provider Organization (EPO)",
        "15": "Indemnity Insurance", "16": "Health Maintenance Organization (HMO) Medicare Risk",
        "17": "Dental Maintenance Organization", "AM": "Automobile Medical",
        "BL": "Blue Cross/Blue Shield", "CH": "Champus", "CI": "Commercial Insurance Co.",
        "DS": "Disability", "FI": "Federal Employees Program",
        "HM": "Health Maintenance Organization", "LM": "Liability Medical",
        "MA": "Medicare Part A", "MB": "Medicare Part B", "MC": "Medicaid",
        "OF": "Other Federal Program", "TV": "Title V", "VA": "Veterans Affairs Plan",
        "WC": "Workers' Compensation Health Claim", "ZZ": "Mutually Defined",
    },
    # CLM05-3 — Claim Frequency Type Code
    "CLM05_3": {
        "1": "Original", "6": "Corrected", "7": "Replacement of Prior Claim",
        "8": "Void/Cancel of Prior Claim",
    },
    # CLM11 — Related Causes Code
    "CLM11": {"AA": "Auto Accident", "EM": "Employment", "OA": "Other Accident"},
    # CL101 — Admission Type Code
    "CL101": {
        "1": "Emergency", "2": "Urgent", "3": "Elective", "4": "Newborn",
        "5": "Trauma", "9": "Information Not Available",
    },
    # DMG03 — Gender Code
    "DMG03": {"F": "Female", "M": "Male", "U": "Unknown"},
    # HI01-1 — Code List Qualifier Code
    "HI01": {
        "ABK": "ICD-10-CM Principal Diagnosis", "ABF": "ICD-10-CM Diagnosis",
        "ABJ": "ICD-10-CM Admitting Diagnosis", "APR": "ICD-10-CM Patient's Reason for Visit",
        "ABN": "ICD-10-CM External Cause of Injury",
        "BK": "ICD-9-CM Principal Diagnosis", "BF": "ICD-9-CM Diagnosis",
        "BJ": "ICD-9-CM Admitting Diagnosis", "PR": "ICD-9-CM Patient's Reason for Visit",
        "BN": "ICD-9-CM External Cause of Injury",
        "BBR": "ICD-10-PCS Principal Procedure", "BBQ": "ICD-10-PCS Other Procedure",
        "BR": "ICD-9-CM Principal Procedure", "BQ": "ICD-9-CM Other Procedure",
        "CAH": "Advanced Billing Concepts (ABC) Codes",
        "BP": "HCPCS Principal Procedure", "BO": "HCPCS Procedure",
        "BE": "Value", "BG": "Condition", "BH": "Occurrence", "BI": "Occurrence Span",
        "DR": "Diagnosis Related Group (DRG)", "TC": "Treatment Codes",
    },
    # CAS01 — Claim Adjustment Group Code
    "CAS01": {
        "CO": "Contractual Obligations", "CR": "Correction and Reversals",
        "OA": "Other adjustments", "PI": "Payor Initiated Reductions",
        "PR": "Patient Responsibility",
    },
    # SV1-03 / SV2-04 — Unit or Basis for Measurement Code
    "UNIT": {
        "DA": "Days", "F2": "International Unit", "GR": "Gram", "ME": "Milligram",
        "MJ": "Minutes", "ML": "Milliliter", "UN": "Unit",
    },
    # PWK01 — Report Type Code
    "PWK01": {
        "03": "Report Justifying Treatment Beyond Utilization Guidelines",
        "04": "Drugs Administered", "05": "Treatment Diagnosis", "06": "Initial Assessment",
        "07": "Functional Goals", "08": "Plan of Treatment", "09": "Progress Report",
        "10": "Continued Treatment", "11": "Chemical Analysis", "13": "Certified Test Report",
        "15": "Justification for Admission", "21": "Recovery Plan",
        "A3": "Allergies/Sensitivities Document", "A4": "Autopsy Report",
        "AM": "Ambulance Certification", "AS": "Admission Summary", "B2": "Prescription",
        "B3": "Physician Order", "B4": "Referral Form", "BR": "Benchmark Testing Results",
        "BS": "Baseline", "BT": "Blanket Test Results", "CB": "Chiropractic Justification",
        "CK": "Consent Form(s)", "CT": "Certification", "D2": "Drug Profile Document",
        "DA": "Dental Models", "DB": "Durable Medical Equipment Prescription",
        "DG": "Diagnostic Report", "DJ": "Discharge Monitoring Report",
        "DS": "Discharge Summary", "EB": "Explanation of Benefits",
        "HC": "Health Certificate", "HR": "Health Clinic Records", "I5": "Immunization Record",
        "IR": "State School Immunization Records", "LA": "Laboratory Results",
        "M1": "Medical Record Attachment", "MT": "Models", "NN": "Nursing Notes",
        "OB": "Operative Note", "OC": "Oxygen Content Averaging Report",
        "OD": "Orders and Treatments Document", "OE": "Objective Physical Examination",
        "OX": "Oxygen Therapy Certification", "OZ": "Support Data for Claim",
        "P4": "Pathology Report", "P5": "Patient Medical History Document",
        "PE": "Parenteral or Enteral Certification", "PN": "Physical Therapy Notes",
        "PO": "Prosthetics or Orthotic Certification", "PQ": "Paramedical Results",
        "PY": "Physician's Report", "PZ": "Physical Therapy Certification",
        "RB": "Radiology Films", "RR": "Radiology Reports",
        "RT": "Report of Tests and Analysis Report",
        "RX": "Renewable Oxygen Content Averaging Report", "SG": "Symptoms Document",
        "V5": "Death Notification", "XP": "Photographs",
    },
    # PWK02 — Report Transmission Code
    "PWK02": {
        "AA": "Available on Request at Provider Site", "BM": "By Mail",
        "EL": "Electronically Only", "EM": "E-Mail", "FT": "File Transfer", "FX": "By Fax",
    },
    # DN202 — Tooth Status Code (837D)
    "DN202": {"E": "To Be Extracted", "M": "Missing"},
    # QTY01 — Quantity Qualifier
    "QTY01": {"PT": "Patients", "FL": "Units"},
    # AMT01 — Amount Qualifier Code
    "AMT01": {
        "F5": "Patient Amount Paid", "F3": "Patient Responsibility - Estimated",
        "D": "Payor Amount Paid", "A8": "Noncovered Charges - Actual",
        "EAF": "Amount Owed", "T": "Tax", "F4": "Postage Claimed",
        "GT": "Goods and Services Tax", "N8": "Facility Tax",
    },
    # REF01 — Reference Identification Qualifier
    "REF01": {
        "EI": "Employer's Identification Number", "SY": "Social Security Number",
        "0B": "State License Number", "1G": "Provider UPIN Number",
        "G2": "Provider Commercial Number", "LU": "Location Number",
        "2U": "Payer Identification Number", "FY": "Claim Office Number",
        "NF": "National Association of Insurance Commissioners (NAIC) Code",
        "Y4": "Agency Claim Number", "1W": "Member Identification Number",
        "4N": "Special Payment Reference Number", "F5": "Medicare Version Code",
        "EW": "Mammography Certification Number", "9F": "Referral Number",
        "G1": "Prior Authorization Number", "F8": "Original Reference Number",
        "X4": "Clinical Laboratory Improvement Amendment Number",
        "9A": "Repriced Claim Reference Number",
        "9C": "Adjusted Repriced Claim Reference Number", "LX": "Qualified Products List",
        "D9": "Claim Number", "EA": "Medical Record Identification Number",
        "P4": "Project Code", "1J": "Facility ID Number", "6R": "Provider Control Number",
        "9B": "Repriced Line Item Reference Number",
        "9D": "Adjusted Repriced Line Item Reference Number", "BT": "Batch Number",
        "F4": "Facility Certification Number", "XZ": "Pharmacy Prescription Number",
        "VY": "Link Sequence Number", "T4": "Signal Code",
    },
    # DTP01 — Date/Time Qualifier
    "DTP01": {
        "011": "Shipped", "050": "Received", "090": "Report Start", "091": "Report End",
        "096": "Discharge", "196": "Start", "198": "Completion",
        "296": "Initial Disability Period Return To Work",
        "297": "Initial Disability Period Last Day Worked", "304": "Latest Visit or Consultation",
        "314": "Disability", "360": "Initial Disability Period Start",
        "361": "Initial Disability Period End", "431": "Onset of Current Symptoms or Illness",
        "434": "Statement", "435": "Admission", "439": "Accident",
        "441": "Prior Placement", "444": "First Visit or Consultation", "446": "Replacement",
        "452": "Appliance Placement", "453": "Acute Manifestation of a Chronic Condition",
        "454": "Initial Treatment", "455": "Last X-Ray", "461": "Last Certification",
        "463": "Begin Therapy", "471": "Prescription", "472": "Service",
        "484": "Last Menstrual Period", "573": "Date Claim Paid",
        "607": "Certification Revision", "738": "Most Recent Hemoglobin or Hematocrit or Both",
        "739": "Most Recent Serum Creatine",
    },
}


# Named qualifier columns, per level.
#   *_REF    : qualifier → column
#   *_DATE   : qualifier → [start column, end column or None]
#   *_AMOUNT : qualifier → column
#   *_QTY    : qualifier → column
# The first occurrence under the parent fills the named column; every
# occurrence is still kept in the level's REF / DATE / AMOUNT table.
NAMED_QUALIFIERS = {
    "BILLING_REF": {
        "EI": "TAX_ID", "SY": "SSN", "0B": "STATE_LICENSE_NUMBER", "1G": "UPIN",
    },
    "SUBSCRIBER_REF": {
        "SY": "SSN", "Y4": "PROPERTY_CASUALTY_CLAIM_NUMBER",
    },
    "PATIENT_REF": {
        "Y4": "PROPERTY_CASUALTY_CLAIM_NUMBER", "1W": "MEMBER_ID_NUMBER", "SY": "SSN",
    },
    "CLAIM_DATE": {
        "434": ["STATEMENT_FROM_DATE", "STATEMENT_TO_DATE"],
        "435": ["ADMISSION_DATE", None],
        "096": ["DISCHARGE_DATE", None],
        "431": ["ONSET_DATE", None],
        "454": ["INITIAL_TREATMENT_DATE", None],
        "304": ["LAST_SEEN_DATE", None],
        "453": ["ACUTE_MANIFESTATION_DATE", None],
        "439": ["ACCIDENT_DATE", None],
        "484": ["LAST_MENSTRUAL_PERIOD_DATE", None],
        "455": ["LAST_XRAY_DATE", None],
        "471": ["PRESCRIPTION_DATE", None],
        "314": ["DISABILITY_START_DATE", "DISABILITY_END_DATE"],
        "360": ["DISABILITY_BEGIN_DATE", None],
        "361": ["DISABILITY_END_DATE_361", None],
        "297": ["LAST_WORKED_DATE", None],
        "296": ["RETURN_TO_WORK_DATE", None],
        "090": ["ASSUMED_CARE_START_DATE", None],
        "091": ["ASSUMED_CARE_END_DATE", None],
        "444": ["FIRST_CONTACT_DATE", None],
        "050": ["REPRICER_RECEIVED_DATE", None],
        "472": ["SERVICE_DATE", "SERVICE_END_DATE"],
        "452": ["APPLIANCE_PLACEMENT_DATE", None],
        "196": ["TREATMENT_START_DATE", None],
        "198": ["TREATMENT_COMPLETION_DATE", None],
    },
    "CLAIM_REF": {
        "4N": "SERVICE_AUTH_EXCEPTION_CODE",
        "F5": "MEDICARE_SECTION_4081_INDICATOR",
        "EW": "MAMMOGRAPHY_CERT_NUMBER",
        "9F": "REFERRAL_NUMBER",
        "G1": "PRIOR_AUTH_NUMBER",
        "F8": "PAYER_CLAIM_CONTROL_NUMBER",
        "X4": "CLIA_NUMBER",
        "9A": "REPRICED_CLAIM_NUMBER",
        "9C": "ADJUSTED_REPRICED_CLAIM_NUMBER",
        "LX": "INVESTIGATIONAL_DEVICE_EXEMPTION",
        "D9": "CLAIM_IDENTIFIER_FOR_TRANSMISSION",
        "EA": "MEDICAL_RECORD_NUMBER",
        "P4": "DEMONSTRATION_PROJECT_ID",
        "1J": "CARE_PLAN_OVERSIGHT_NUMBER",
    },
    "CLAIM_AMOUNT": {
        "F5": "PATIENT_AMOUNT_PAID",
        "F3": "PATIENT_ESTIMATED_AMOUNT_DUE",
    },
    "OTHER_SUBSCRIBER_AMOUNT": {
        "D": "PAYER_PAID_AMOUNT",
        "A8": "NONCOVERED_AMOUNT",
        "EAF": "REMAINING_PATIENT_LIABILITY",
    },
    "OTHER_SUBSCRIBER_DATE": {
        "573": ["ADJUDICATION_DATE", None],
    },
    "OTHER_SUBSCRIBER_REF": {
        "F8": "PAYER_CLAIM_CONTROL_NUMBER",
        "G1": "PRIOR_AUTH_NUMBER",
        "9F": "REFERRAL_NUMBER",
        "T4": "CLAIM_ADJUSTMENT_INDICATOR",
    },
    "LINE_DATE": {
        "472": ["SERVICE_DATE", "SERVICE_END_DATE"],
        "471": ["PRESCRIPTION_DATE", None],
        "607": ["CERT_REVISION_DATE", None],
        "463": ["BEGIN_THERAPY_DATE", None],
        "461": ["LAST_CERTIFICATION_DATE", None],
        "304": ["LAST_SEEN_DATE", None],
        "738": ["HEMOGLOBIN_TEST_DATE", None],
        "739": ["SERUM_CREATININE_TEST_DATE", None],
        "011": ["SHIPPED_DATE", None],
        "455": ["LAST_XRAY_DATE", None],
        "454": ["INITIAL_TREATMENT_DATE", None],
        "441": ["PRIOR_PLACEMENT_DATE", None],
        "452": ["APPLIANCE_PLACEMENT_DATE", None],
        "446": ["REPLACEMENT_DATE", None],
        "196": ["TREATMENT_START_DATE", None],
        "198": ["TREATMENT_COMPLETION_DATE", None],
    },
    "LINE_REF": {
        "6R": "LINE_ITEM_CONTROL_NUMBER",
        "9B": "REPRICED_LINE_ITEM_REF",
        "9D": "ADJUSTED_REPRICED_LINE_ITEM_REF",
        "G1": "PRIOR_AUTH_NUMBER",
        "9F": "REFERRAL_NUMBER",
        "BT": "IMMUNIZATION_BATCH_NUMBER",
        "EW": "MAMMOGRAPHY_CERT_NUMBER",
        "X4": "CLIA_NUMBER",
        "F4": "REFERRING_CLIA_NUMBER",
        "XZ": "PRESCRIPTION_NUMBER",
        "VY": "LINK_SEQUENCE_NUMBER",
    },
    "LINE_AMOUNT": {
        "T": "SALES_TAX_AMOUNT",
        "F4": "POSTAGE_CLAIMED_AMOUNT",
        "GT": "SERVICE_TAX_AMOUNT",
        "N8": "FACILITY_TAX_AMOUNT",
    },
    "LINE_QTY": {
        "PT": "AMBULANCE_PATIENT_COUNT",
        "FL": "OBSTETRIC_ANESTHESIA_UNITS",
    },
}