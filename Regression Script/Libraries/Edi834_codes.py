# ─────────────────────────────────────────────────────────────────────────────
# Edi834_codes.py
# Code lists and named qualifier columns for the EDI834 file format
# (005010X220A1 Benefit Enrollment and Maintenance).
#
# Defaults only. Extend or override per test case through FileConfig:
#     "CodeLists"       : { "INS03": { "021": "Addition", ... }, ... }
#     "NamedQualifiers" : { "MEMBER_REF": { "0F": "SUBSCRIBER_NUMBER" }, ... }
# so new codes or qualifiers never need a parser change.
# ─────────────────────────────────────────────────────────────────────────────

_MAINTENANCE_TYPE = {
    "001": "Change",
    "002": "Delete",
    "021": "Addition",
    "024": "Cancellation or Termination",
    "025": "Reinstatement",
    "026": "Correction",
    "030": "Audit or Compare",
    "032": "Employee Information Not Applicable",
}

CODE_LISTS = {
    # BGN01 — Transaction Set Purpose Code
    "BGN01": {"00": "Original", "15": "Re-Submission", "22": "Information Copy"},
    # BGN08 — Action Code
    "BGN08": {"2": "Change (Update)", "4": "Verify", "RX": "Replace"},
    # N101 — Entity Identifier Code (1000A/B/C, 2750)
    "N101": {
        "P5": "Plan Sponsor",
        "IN": "Insurer",
        "BO": "Broker or Sales Office",
        "TV": "Third Party Administrator (TPA)",
        "75": "Participant",
    },
    # NM101 — Entity Identifier Code (2100A-H, 2310, 2330)
    "NM101": {
        "IL": "Insured or Subscriber",
        "74": "Corrected Insured",
        "70": "Prior Incorrect Insured",
        "31": "Postal Mailing Address",
        "36": "Employer",
        "M8": "Educational Institution",
        "S3": "Custodial Parent",
        "45": "Drop-off Location",
        "6Y": "Case Manager",
        "9K": "Key Person",
        "E1": "Person or Other Entity Legally Responsible for a Child",
        "EI": "Executor of Estate",
        "EXS": "Ex-spouse",
        "GB": "Other Insured",
        "GD": "Guardian",
        "J6": "Power of Attorney",
        "LR": "Legal Representative",
        "QD": "Responsible Party",
        "S1": "Parent",
        "TZ": "Significant Other",
        "P3": "Primary Care Provider",
        "1X": "Laboratory",
        "3D": "Obstetrics and Gynecology Facility",
        "80": "Hospital",
        "FA": "Facility",
        "OD": "Doctor of Optometry",
        "QA": "Pharmacy",
        "QN": "Dentist",
        "Y2": "Managed Care Organization",
        "IN": "Insurer",
        "GW": "Group",
    },
    # INS02 — Individual Relationship Code
    "INS02": {
        "01": "Spouse",
        "03": "Father or Mother",
        "04": "Grandfather or Grandmother",
        "05": "Grandson or Granddaughter",
        "06": "Uncle or Aunt",
        "07": "Nephew or Niece",
        "08": "Cousin",
        "09": "Adopted Child",
        "10": "Foster Child",
        "11": "Son-in-law or Daughter-in-law",
        "12": "Brother-in-law or Sister-in-law",
        "13": "Mother-in-law or Father-in-law",
        "14": "Brother or Sister",
        "15": "Ward",
        "16": "Stepparent",
        "17": "Stepson or Stepdaughter",
        "18": "Self",
        "19": "Child",
        "23": "Sponsored Dependent",
        "24": "Dependent of a Minor Dependent",
        "25": "Ex-spouse",
        "26": "Guardian",
        "31": "Court Appointed Guardian",
        "38": "Collateral Dependent",
        "53": "Life Partner",
        "60": "Annuitant",
        "D2": "Trustee",
        "G8": "Other Relationship",
        "G9": "Other Relative",
    },
    # INS03 / HD01 — Maintenance Type Code
    "INS03": _MAINTENANCE_TYPE,
    "HD01": _MAINTENANCE_TYPE,
    # INS04 — Maintenance Reason Code
    "INS04": {
        "01": "Divorce", "02": "Birth", "03": "Death", "04": "Retirement",
        "05": "Adoption", "06": "Strike", "07": "Termination of Benefits",
        "08": "Termination of Employment", "09": "Consolidation Omnibus Budget Reconciliation Act (COBRA)",
        "10": "Consolidation Omnibus Budget Reconciliation Act (COBRA) Premium Paid",
        "11": "Surviving Spouse", "14": "Voluntary Withdrawal",
        "15": "Primary Care Provider (PCP) Change", "16": "Quit", "17": "Fired",
        "18": "Suspended", "20": "Active", "21": "Disability", "22": "Plan Change",
        "25": "Change in Identifying Data Elements", "26": "Declined Coverage",
        "27": "Pre-Enrollment", "28": "Initial Enrollment", "29": "Benefit Selection",
        "31": "Legal Separation", "32": "Marriage", "33": "Personnel Data",
        "37": "Leave of Absence with Benefits", "38": "Leave of Absence without Benefits",
        "39": "Lay Off with Benefits", "40": "Lay Off without Benefits",
        "41": "Re-enrollment", "43": "Change of Location", "59": "Non Payment",
        "AA": "Dissatisfaction with Office Staff", "AB": "Dissatisfaction with Medical Care/Services Rendered",
        "AC": "Inconvenient Office Location", "AD": "Dissatisfaction with Office Hours",
        "AE": "Unable to Schedule Appointments in a Timely Manner",
        "AF": "Dissatisfaction with Physician's Referral Policy",
        "AG": "Other", "AH": "Due to Change in Office", "AI": "No Reason Given",
        "AJ": "Appointment Times not Met in a Timely Manner",
        "EC": "Member Benefit Selection", "XN": "Notification Only", "XT": "Transfer",
    },
    # INS05 — Benefit Status Code
    "INS05": {
        "A": "Active",
        "C": "Consolidated Omnibus Budget Reconciliation Act (COBRA)",
        "S": "Surviving Insured",
        "T": "Tax Equity and Fiscal Responsibility Act (TEFRA)",
    },
    # INS06-1 — Medicare Plan Code
    "INS06_1": {
        "A": "Medicare Part A", "B": "Medicare Part B", "C": "Medicare Part A and B",
        "D": "Medicare", "E": "No Medicare",
    },
    # INS06-2 — Eligibility Reason Code
    "INS06_2": {"0": "Age", "1": "Disability", "2": "End Stage Renal Disease (ESRD)"},
    # INS07 — COBRA Qualifying Event Code
    "INS07": {
        "1": "Termination of Employment", "2": "Reduction of work hours",
        "3": "Medicare", "4": "Death", "5": "Divorce", "6": "Separation",
        "7": "Ineligible Child", "8": "Bankruptcy of Retiree's Former Employer",
        "9": "Layoff", "10": "Leave of Absence", "ZZ": "Mutually Defined",
    },
    # INS08 — Employment Status Code
    "INS08": {
        "AC": "Active", "AO": "Active Overseas", "AU": "Active Military - USA",
        "FT": "Full-time", "L1": "Leave of Absence", "PT": "Part-time",
        "RT": "Retired", "TE": "Terminated",
    },
    # INS09 — Student Status Code
    "INS09": {"F": "Full-time", "N": "Not a Student", "P": "Part-time"},
    # DMG03 — Gender Code
    "DMG03": {"F": "Female", "M": "Male", "U": "Unknown"},
    # DMG04 — Marital Status Code
    "DMG04": {
        "B": "Registered Domestic Partner", "D": "Divorced", "I": "Single",
        "M": "Married", "R": "Unreported", "S": "Separated", "U": "Unmarried",
        "W": "Widowed", "X": "Legally Separated",
    },
    # DMG06 — Citizenship Status Code
    "DMG06": {
        "1": "U.S. Citizen", "2": "Non-Resident Alien", "3": "Resident Alien",
        "4": "Illegal Alien", "5": "Alien", "6": "U.S. Citizen - Non-Resident",
        "7": "U.S. Citizen - Resident",
    },
    # DSB01 — Disability Type Code
    "DSB01": {
        "1": "Short Term Disability", "2": "Long Term Disability",
        "3": "Permanent or Total Disability", "4": "No Disability",
    },
    # HD03 — Insurance Line Code
    "HD03": {
        "AG": "Preventative Care/Wellness", "AH": "24 Hour Care",
        "AJ": "Medicare Risk", "AK": "Mental Health", "DCP": "Dental Capitation",
        "DEN": "Dental", "EPO": "Exclusive Provider Organization", "FAC": "Facility",
        "HE": "Hearing", "HLT": "Health", "HMO": "Health Maintenance Organization",
        "LTC": "Long-Term Care", "LTD": "Long-Term Disability",
        "MM": "Major Medical", "MOD": "Mail Order Drug", "PDG": "Prescription Drug",
        "POS": "Point of Service", "PPO": "Preferred Provider Organization",
        "PRA": "Practitioners", "STD": "Short-Term Disability",
        "UR": "Utilization Review", "VIS": "Vision",
    },
    # HD05 — Coverage Level Code
    "HD05": {
        "CHD": "Children Only", "DEP": "Dependents Only",
        "E1D": "Employee and One Dependent", "E2D": "Employee and Two Dependents",
        "E3D": "Employee and Three Dependents", "E5D": "Employee and One or More Dependents",
        "E6D": "Employee and Two or More Dependents", "E7D": "Employee and Three or More Dependents",
        "E8D": "Employee and Four or More Dependents", "E9D": "Employee and Five or More Dependents",
        "ECH": "Employee and Children", "EMP": "Employee Only", "ESP": "Employee and Spouse",
        "FAM": "Family", "IND": "Individual", "SPC": "Spouse and Children",
        "SPO": "Spouse Only", "TWO": "Two Party",
    },
    # COB01 — Payer Responsibility Sequence Number Code
    "COB01": {"P": "Primary", "S": "Secondary", "T": "Tertiary", "U": "Unknown"},
    # COB03 — Coordination of Benefits Code
    "COB03": {"1": "Coordination of Benefits", "5": "Unknown", "6": "No Coordination of Benefits"},
    # LUI04 — Language Use Indicator
    "LUI04": {"5": "Language Reading", "6": "Language Writing",
              "7": "Language Speaking", "8": "Native Language"},
    # QTY01 — Quantity Qualifier
    "QTY01": {"DT": "Dependent Total", "ET": "Employee Total", "TO": "Total"},
    # AMT01 — Amount Qualifier Code
    "AMT01": {
        "B9": "Co-insurance - Actual", "C1": "Co-Payment Amount",
        "D2": "Deductible Amount", "EBA": "Expected Expenditure Amount",
        "FK": "Other Unlisted Amount", "P3": "Premium Amount", "R": "Spend Down",
    },
    # REF01 — Reference Identification Qualifier
    "REF01": {
        "00": "Contracting District Number", "0B": "State License Number",
        "0F": "Subscriber Number", "17": "Client Reporting Category",
        "18": "Plan Number", "19": "Division Identifier", "1L": "Group or Policy Number",
        "23": "Client Number", "26": "Union Number", "38": "Master Policy Number",
        "3H": "Case Number", "3L": "Branch Identifier", "4A": "Personal Identification Number (PIN)",
        "60": "Account Suffix Code", "6M": "Application Number", "6O": "Cross Reference Number",
        "6P": "Group Number", "9V": "Payment Category", "9X": "Account Category",
        "ABB": "Personal ID Number", "CE": "Class of Contract Code",
        "D3": "National Council for Prescription Drug Programs Pharmacy Number",
        "DX": "Department/Agency Number", "E8": "Service Contract (Coverage) Number",
        "F6": "Health Insurance Claim (HIC) Number", "GE": "Geographic Number",
        "LU": "Location Number", "M7": "Medical Assistance Category",
        "P5": "Position Code", "PID": "Program Identification Number",
        "Q4": "Prior Identifier Number", "QQ": "Unit Number", "RB": "Rate code number",
        "SY": "Social Security Number", "X9": "Internal Control Number",
        "XM": "Issuer Number", "XX1": "Special Program Code", "XX2": "Service Area Code",
        "YY": "Geographic Key", "ZX": "County Code", "ZZ": "Mutually Defined",
    },
    # DTP01 — Date/Time Qualifier
    "DTP01": {
        "007": "Effective", "050": "Received", "090": "Report Start",
        "091": "Report End", "286": "Retirement",
        "296": "Initial Disability Period Return To Work",
        "297": "Initial Disability Period Last Day Worked",
        "300": "Enrollment Signature Date",
        "301": "Consolidated Omnibus Budget Reconciliation Act (COBRA) Qualifying Event",
        "303": "Maintenance Effective", "336": "Employment Begin", "337": "Employment End",
        "338": "Medicare Begin", "339": "Medicare End", "340": "COBRA Begin",
        "341": "COBRA End", "343": "Premium Paid to Date End",
        "344": "Coordination of Benefits Begin", "345": "Coordination of Benefits End",
        "348": "Benefit Begin", "349": "Benefit End", "350": "Education Begin",
        "351": "Education End", "356": "Eligibility Begin", "357": "Eligibility End",
        "360": "Initial Disability Period Start", "361": "Initial Disability Period End",
        "382": "Enrollment", "383": "Adjusted Hire", "385": "Credited Service Begin",
        "386": "Credited Service End", "388": "Payment Commencement",
        "393": "Plan Participation Suspension", "394": "Rehire",
        "473": "Medicaid Begin", "474": "Medicaid End",
        "543": "Last Premium Paid Date", "695": "Previous Period",
    },
}


# Named qualifier columns, per level.
#   *_REF    : qualifier → column
#   *_DATE   : qualifier → [start column, end column or None]
#   *_AMOUNT : qualifier → column
#   *_QTY    : qualifier → column
# The first occurrence under the parent fills the named column; every
# occurrence is still kept in the level's REF/DATE/AMOUNT table.
NAMED_QUALIFIERS = {
    "HEADER_REF": {
        "38": "MASTER_POLICY_NUMBER",
    },
    "HEADER_DATE": {
        "007": ["FILE_EFFECTIVE_DATE", None],
        "090": ["REPORT_START_DATE", None],
        "091": ["REPORT_END_DATE", None],
        "303": ["MAINTENANCE_EFFECTIVE_DATE", None],
        "382": ["ENROLLMENT_DATE", None],
        "388": ["PAYMENT_COMMENCEMENT_DATE", None],
    },
    "HEADER_QTY": {
        "ET": "EMPLOYEE_TOTAL",
        "DT": "DEPENDENT_TOTAL",
        "TO": "TOTAL_COUNT",
    },
    "MEMBER_REF": {
        "0F": "SUBSCRIBER_NUMBER",
        "1L": "GROUP_POLICY_NUMBER",
        "17": "CLIENT_REPORTING_CATEGORY",
        "23": "CLIENT_NUMBER",
        "3H": "CASE_NUMBER",
        "4A": "PERSONAL_ID_NUMBER",
        "6O": "CROSS_REFERENCE_NUMBER",
        "ABB": "PERSONAL_ID",
        "D3": "PHARMACY_NUMBER",
        "DX": "DEPARTMENT_AGENCY_NUMBER",
        "F6": "HIC_NUMBER",
        "P5": "POSITION_CODE",
        "Q4": "PRIOR_IDENTIFIER",
        "QQ": "UNIT_NUMBER",
        "ZZ": "MUTUALLY_DEFINED_ID",
    },
    "MEMBER_DATE": {
        "050": ["RECEIVED_DATE", None],
        "286": ["RETIREMENT_DATE", None],
        "296": ["DISABILITY_RETURN_TO_WORK_DATE", None],
        "297": ["DISABILITY_LAST_DAY_WORKED", None],
        "300": ["ENROLLMENT_SIGNATURE_DATE", None],
        "301": ["COBRA_QUALIFYING_EVENT_DATE", None],
        "303": ["MAINTENANCE_EFFECTIVE_DATE", None],
        "336": ["EMPLOYMENT_BEGIN_DATE", None],
        "337": ["EMPLOYMENT_END_DATE", None],
        "338": ["MEDICARE_BEGIN_DATE", None],
        "339": ["MEDICARE_END_DATE", None],
        "340": ["COBRA_BEGIN_DATE", None],
        "341": ["COBRA_END_DATE", None],
        "350": ["EDUCATION_BEGIN_DATE", None],
        "351": ["EDUCATION_END_DATE", None],
        "356": ["ELIGIBILITY_BEGIN_DATE", None],
        "357": ["ELIGIBILITY_END_DATE", None],
        "383": ["ADJUSTED_HIRE_DATE", None],
        "385": ["CREDITED_SERVICE_BEGIN_DATE", None],
        "386": ["CREDITED_SERVICE_END_DATE", None],
        "393": ["PLAN_SUSPENSION_DATE", None],
        "394": ["REHIRE_DATE", None],
        "473": ["MEDICAID_BEGIN_DATE", None],
        "474": ["MEDICAID_END_DATE", None],
    },
    "MEMBER_AMOUNT": {
        "B9": "COINSURANCE_AMOUNT",
        "C1": "COPAY_AMOUNT",
        "D2": "DEDUCTIBLE_AMOUNT",
        "EBA": "EXPECTED_EXPENDITURE_AMOUNT",
        "FK": "OTHER_AMOUNT",
        "P3": "PREMIUM_AMOUNT",
        "R": "SPEND_DOWN_AMOUNT",
    },
    "DISABILITY_DATE": {
        "360": ["DISABILITY_BEGIN_DATE", None],
        "361": ["DISABILITY_END_DATE", None],
    },
    "COVERAGE_REF": {
        "1L": "GROUP_POLICY_NUMBER",
        "17": "CLIENT_REPORTING_CATEGORY",
        "9V": "PAYMENT_CATEGORY",
        "CE": "CLASS_OF_CONTRACT",
        "E8": "SERVICE_CONTRACT_NUMBER",
        "M7": "MEDICAL_ASSISTANCE_CATEGORY",
        "PID": "PROGRAM_ID",
        "QQ": "PRIOR_COVERAGE_MONTHS",
        "RB": "RATE_CODE",
        "X9": "INTERNAL_CONTROL_NUMBER",
        "XM": "ISSUER_NUMBER",
        "XX1": "SPECIAL_PROGRAM_CODE",
        "XX2": "SERVICE_AREA_CODE",
        "ZX": "COUNTY_CODE",
        "ZZ": "MUTUALLY_DEFINED_ID",
    },
    "COVERAGE_DATE": {
        "300": ["ENROLLMENT_SIGNATURE_DATE", None],
        "303": ["MAINTENANCE_EFFECTIVE_DATE", None],
        "343": ["PREMIUM_PAID_TO_DATE_END", None],
        "348": ["BENEFIT_BEGIN_DATE", None],
        "349": ["BENEFIT_END_DATE", None],
        "543": ["LAST_PREMIUM_PAID_DATE", None],
        "695": ["PREVIOUS_PERIOD_START_DATE", "PREVIOUS_PERIOD_END_DATE"],
    },
    "COVERAGE_AMOUNT": {
        "B9": "COINSURANCE_AMOUNT",
        "C1": "COPAY_AMOUNT",
        "D2": "DEDUCTIBLE_AMOUNT",
        "EBA": "EXPECTED_EXPENDITURE_AMOUNT",
        "FK": "OTHER_AMOUNT",
        "P3": "PREMIUM_AMOUNT",
        "R": "SPEND_DOWN_AMOUNT",
    },
    "COB_REF": {
        "60": "ACCOUNT_SUFFIX_CODE",
        "6P": "GROUP_NUMBER",
        "SY": "SSN",
        "ZZ": "MUTUALLY_DEFINED_ID",
    },
    "COB_DATE": {
        "344": ["COB_BEGIN_DATE", None],
        "345": ["COB_END_DATE", None],
    },
}