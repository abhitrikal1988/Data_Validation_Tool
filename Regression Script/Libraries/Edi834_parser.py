# ─────────────────────────────────────────────────────────────────────────────
# Edi834_parser.py
# X12 834 Benefit Enrollment and Maintenance (005010X220A1) parser for the
# Data Validation framework — FileConfig.Format = "EDI834".
#
# Design
#   - Follows the 834 standard, not any target schema. Every element is
#     extracted into business-named columns; mapping to a target (Oracle
#     tables, MongoDB, another file) happens only in the test case
#     (JoinFields / CompareFields).
#   - Same parser for inbound (received) and outbound (generated) 834 files.
#   - Output = one DataFrame per standard table (see _table_columns).
#   - Every row carries its full position: file, envelope, sponsor, payer,
#     member, coverage, sub-loop, occurrence and segment position.
#   - Values kept as text, trailing spaces trimmed, empty → None.
#   - Segments that cannot be placed go to UNMAPPED — nothing is dropped.
#   - A file is parsed once per run; every test case reuses the cached result.
#
# Hook into File_parser.parse_file():
#     from Libraries.Edi834_parser import parse_edi834_for_file_parser
#     parser_map["EDI834"] = parse_edi834_for_file_parser
# ─────────────────────────────────────────────────────────────────────────────

import hashlib
import json
import logging
import threading
from collections import OrderedDict

import pandas as pd

try:
    from Libraries import Edi834_codes as _codes
except ImportError:                      # running from inside Libraries/
    import Edi834_codes as _codes

logger = logging.getLogger()


# ─────────────────────────────────────────────────────────────────────────────
# Column definitions
# ─────────────────────────────────────────────────────────────────────────────

HIER_COLS = [
    "FILE_ID", "FILE_NAME",
    "INTERCHANGE_CONTROL_NUMBER", "GROUP_CONTROL_NUMBER",
    "TRANSACTION_CONTROL_NUMBER", "BGN_REFERENCE_ID",
    "SPONSOR_ID", "PAYER_ID",
    "MEMBER_SEQ", "MEMBER_KEY", "MEMBER_OCC",
    "SUBSCRIBER_ID", "MEMBER_ID", "SUBSCRIBER_INDICATOR", "RELATIONSHIP",
    "MEMBER_MAINTENANCE_TYPE",
    "LOOP_ID",
    "COVERAGE_SEQ", "COVERAGE_KEY", "COVERAGE_OCC",
    "COB_SEQ", "LOOP_SEQ",
    "OCC", "SEG_POS",
]

# Columns identifying a row's parent — used to number OCC
_PARENT_COLS = [
    "FILE_ID", "INTERCHANGE_CONTROL_NUMBER", "GROUP_CONTROL_NUMBER",
    "TRANSACTION_CONTROL_NUMBER", "MEMBER_SEQ", "LOOP_ID",
    "COVERAGE_SEQ", "COB_SEQ", "LOOP_SEQ",
]

_INT_COLS = ["MEMBER_SEQ", "MEMBER_OCC", "COVERAGE_SEQ", "COVERAGE_OCC",
             "COB_SEQ", "LOOP_SEQ", "OCC", "SEG_POS"]


def _pfx(prefix: str, field: str) -> str:
    """Prefixes a field name without doubling (ENTITY_ + ENTITY_CODE → ENTITY_CODE)."""
    return field if field.startswith(prefix) else prefix + field


def _cols(prefix: str, fields: list) -> list:
    return [_pfx(prefix, f) for f in fields]


_NAME_FIELDS = ["ENTITY_CODE", "ENTITY_DESC", "ENTITY_TYPE", "LAST_NAME", "FIRST_NAME",
                "MIDDLE_NAME", "PREFIX", "SUFFIX", "ID_QUALIFIER", "ID"]
_ADDRESS_FIELDS = ["ADDRESS_LINE1", "ADDRESS_LINE2", "CITY", "STATE", "ZIP", "COUNTRY",
                   "LOCATION_QUALIFIER", "LOCATION_CODE", "COUNTRY_SUBDIVISION"]
_PRV_FIELDS = ["PROVIDER_ROLE", "TAXONOMY_QUALIFIER", "TAXONOMY_CODE"]

_PER_QUALIFIER_COLS = {"TE": "PHONE", "EX": "PHONE_EXT", "FX": "FAX", "EM": "EMAIL",
                       "UR": "URL", "HP": "HOME_PHONE", "WP": "WORK_PHONE",
                       "CP": "CELL_PHONE", "AP": "ALTERNATE_PHONE", "BN": "BEEPER"}
_CONTACT_COLS = (["CONTACT_FUNCTION", "CONTACT_NAME"]
                 + list(_PER_QUALIFIER_COLS.values())
                 + ["OTHER_COMM_QUALIFIER", "OTHER_COMM_NUMBER"])
_REF_COLS = ["REF_QUALIFIER", "REF_QUALIFIER_DESC", "REF_ID", "REF_DESCRIPTION"]
_DATE_COLS = ["DATE_QUALIFIER", "DATE_QUALIFIER_DESC", "DATE_FORMAT", "START_DATE", "END_DATE"]
_AMOUNT_COLS = ["AMOUNT_QUALIFIER", "AMOUNT_QUALIFIER_DESC", "AMOUNT"]

_ENVELOPE_COLS = [
    "INTERCHANGE_SENDER_QUALIFIER", "INTERCHANGE_SENDER_ID",
    "INTERCHANGE_RECEIVER_QUALIFIER", "INTERCHANGE_RECEIVER_ID",
    "INTERCHANGE_DATE", "INTERCHANGE_TIME", "INTERCHANGE_VERSION",
    "ACK_REQUESTED", "USAGE_INDICATOR",
    "FUNCTIONAL_ID_CODE", "GROUP_SENDER", "GROUP_RECEIVER",
    "GROUP_DATE", "GROUP_TIME", "VERSION",
    "TRANSACTION_SET_ID", "IMPLEMENTATION_REFERENCE",
    "PURPOSE_CODE", "PURPOSE_DESC", "BGN_DATE", "BGN_TIME", "TIME_ZONE",
    "ORIGINAL_REFERENCE_ID", "TRANSACTION_TYPE", "ACTION_CODE", "ACTION_DESC",
]
_TRAILER_COLS = ["SEGMENT_COUNT", "GROUP_TRANSACTION_COUNT", "INTERCHANGE_GROUP_COUNT"]

_MEMBER_INS_COLS = [
    "SUBSCRIBER_INDICATOR", "RELATIONSHIP", "RELATIONSHIP_DESC",
    "MAINTENANCE_TYPE", "MAINTENANCE_TYPE_DESC",
    "MAINTENANCE_REASON", "MAINTENANCE_REASON_DESC",
    "BENEFIT_STATUS", "BENEFIT_STATUS_DESC",
    "MEDICARE_PLAN_CODE", "MEDICARE_PLAN_DESC",
    "MEDICARE_ELIGIBILITY_REASON", "MEDICARE_ELIGIBILITY_REASON_DESC",
    "COBRA_EVENT", "COBRA_EVENT_DESC",
    "EMPLOYMENT_STATUS", "EMPLOYMENT_STATUS_DESC",
    "STUDENT_STATUS", "STUDENT_STATUS_DESC",
    "HANDICAP_INDICATOR", "DEATH_DATE_FORMAT", "DEATH_DATE",
    "CONFIDENTIALITY_CODE", "BIRTH_SEQUENCE",
]
_MEMBER_DETAIL_COLS = [
    "DOB_FORMAT", "DATE_OF_BIRTH", "GENDER", "GENDER_DESC",
    "MARITAL_STATUS", "MARITAL_STATUS_DESC", "RACE_ETHNICITY_CODES",
    "CITIZENSHIP_STATUS", "CITIZENSHIP_STATUS_DESC",
    "RACE_CODE_LIST_QUALIFIER", "RACE_INDUSTRY_CODE",
    "INCOME_FREQUENCY", "WAGE_AMOUNT", "WORK_HOURS", "INCOME_LOCATION_ID", "SALARY_GRADE",
    "HEALTH_CODE", "HEIGHT", "WEIGHT",
]


def _named_cols(named: dict, group: str) -> list:
    """Column names produced by one named-qualifier group, in order."""
    out = []
    for value in named.get(group, {}).values():
        if isinstance(value, (list, tuple)):
            out += [c for c in value if c]
        elif value:
            out.append(value)
    return out


def _table_columns(named: dict) -> "OrderedDict[str, list]":
    """Column order per table (hierarchy columns are added in front)."""
    n = lambda *groups: [c for g in groups for c in _named_cols(named, g)]
    return OrderedDict([
        ("TRANSACTION", _ENVELOPE_COLS + n("HEADER_REF", "HEADER_DATE", "HEADER_QTY") + _TRAILER_COLS),
        ("TRANSACTION_REF", _REF_COLS),
        ("TRANSACTION_DATE", _DATE_COLS),
        ("TRANSACTION_QUANTITY", ["QUANTITY_QUALIFIER", "QUANTITY_QUALIFIER_DESC", "QUANTITY"]),
        ("PARTY", ["PARTY_CODE", "PARTY_DESC", "PARTY_NAME", "PARTY_ID_QUALIFIER",
                   "PARTY_ID", "ACCOUNT_NUMBER", "ACCOUNT_NUMBER_2"]),
        ("MEMBER", _MEMBER_INS_COLS + _cols("MEMBER_", _NAME_FIELDS) + ["MEMBER_SSN"]
                   + _cols("MEMBER_", _ADDRESS_FIELDS) + _MEMBER_DETAIL_COLS
                   + n("MEMBER_REF", "MEMBER_DATE", "MEMBER_AMOUNT")),
        ("MEMBER_REF", _REF_COLS),
        ("MEMBER_DATE", _DATE_COLS),
        ("MEMBER_CONTACT", _CONTACT_COLS),
        ("MEMBER_RACE_ETHNICITY", ["RACE_ETHNICITY_CODE", "CODE_LIST_QUALIFIER", "INDUSTRY_CODE"]),
        ("MEMBER_EMPLOYMENT_CLASS", ["CLASS_CODE_1", "CLASS_CODE_2", "CLASS_CODE_3"]),
        ("MEMBER_AMOUNT", _AMOUNT_COLS),
        ("MEMBER_LANGUAGE", ["LANGUAGE_QUALIFIER", "LANGUAGE_CODE", "LANGUAGE_DESCRIPTION",
                             "LANGUAGE_USE", "LANGUAGE_USE_DESC"]),
        ("MEMBER_OTHER_NAME", _cols("ENTITY_", _NAME_FIELDS) + _cols("ENTITY_", _ADDRESS_FIELDS)
                              + ["DOB_FORMAT", "DATE_OF_BIRTH", "GENDER", "GENDER_DESC",
                                 "MARITAL_STATUS", "MARITAL_STATUS_DESC"]),
        ("MEMBER_DISABILITY", ["DISABILITY_TYPE", "DISABILITY_TYPE_DESC",
                               "DIAGNOSIS_QUALIFIER", "DIAGNOSIS_CODE"] + n("DISABILITY_DATE")),
        ("DISABILITY_DATE", _DATE_COLS),
        ("COVERAGE", ["MAINTENANCE_TYPE", "MAINTENANCE_TYPE_DESC",
                      "INSURANCE_LINE", "INSURANCE_LINE_DESC", "PLAN_COVERAGE_DESCRIPTION",
                      "COVERAGE_LEVEL", "COVERAGE_LEVEL_DESC", "LATE_ENROLLMENT_INDICATOR"]
                     + n("COVERAGE_REF", "COVERAGE_DATE", "COVERAGE_AMOUNT")),
        ("COVERAGE_DATE", _DATE_COLS),
        ("COVERAGE_AMOUNT", _AMOUNT_COLS),
        ("COVERAGE_REF", _REF_COLS),
        ("COVERAGE_ID_CARD", ["PLAN_COVERAGE_DESCRIPTION", "ID_CARD_TYPE",
                              "CARD_QUANTITY", "ACTION_CODE"]),
        ("COVERAGE_PROVIDER", ["ASSIGNED_NUMBER"] + _cols("PROVIDER_", _NAME_FIELDS)
                              + ["ENTITY_RELATIONSHIP"] + _cols("PROVIDER_", _ADDRESS_FIELDS)
                              + _PRV_FIELDS
                              + ["PROVIDER_CHANGE_ACTION", "PROVIDER_CHANGE_ENTITY",
                                 "PROVIDER_CHANGE_DATE", "PROVIDER_CHANGE_TIME",
                                 "PROVIDER_CHANGE_REASON"]),
        ("PROVIDER_CONTACT", _CONTACT_COLS),
        ("COB", ["PAYER_RESPONSIBILITY", "PAYER_RESPONSIBILITY_DESC", "COB_POLICY_NUMBER",
                 "COB_CODE", "COB_CODE_DESC", "SERVICE_TYPE_CODES"] + n("COB_REF", "COB_DATE")),
        ("COB_REF", _REF_COLS),
        ("COB_DATE", _DATE_COLS),
        ("COB_ENTITY", _cols("ENTITY_", _NAME_FIELDS) + _cols("ENTITY_", _ADDRESS_FIELDS)),
        ("COB_ENTITY_CONTACT", _CONTACT_COLS),
        ("REPORTING_CATEGORY", ["ASSIGNED_NUMBER", "CATEGORY_ENTITY_CODE", "CATEGORY_NAME"]
                               + _REF_COLS + _DATE_COLS),
        ("UNMAPPED", ["SEG_ID", "SEGMENT_TEXT", "REASON"]),
    ])


TABLE_NAMES = list(_table_columns({}).keys())

# OCC rule: occurrence number of the row's key under its parent.
# Empty key = order under the parent.
#   table: (key columns, parent columns to leave out)
_OCC_SPEC = {
    "TRANSACTION_REF":         (["REF_QUALIFIER"], []),
    "TRANSACTION_DATE":        (["DATE_QUALIFIER"], []),
    "TRANSACTION_QUANTITY":    (["QUANTITY_QUALIFIER"], []),
    "PARTY":                   (["PARTY_CODE"], ["LOOP_SEQ", "LOOP_ID"]),
    "MEMBER_REF":              (["REF_QUALIFIER"], []),
    "MEMBER_DATE":             (["DATE_QUALIFIER"], []),
    "MEMBER_CONTACT":          ([], []),
    "MEMBER_RACE_ETHNICITY":   (["RACE_ETHNICITY_CODE"], []),
    "MEMBER_EMPLOYMENT_CLASS": ([], []),
    "MEMBER_AMOUNT":           (["AMOUNT_QUALIFIER"], []),
    "MEMBER_LANGUAGE":         (["LANGUAGE_CODE"], []),
    "MEMBER_OTHER_NAME":       (["ENTITY_CODE"], ["LOOP_SEQ"]),
    "MEMBER_DISABILITY":       (["DISABILITY_TYPE"], ["LOOP_SEQ"]),
    "DISABILITY_DATE":         (["DATE_QUALIFIER"], []),
    "COVERAGE_DATE":           (["DATE_QUALIFIER"], []),
    "COVERAGE_AMOUNT":         (["AMOUNT_QUALIFIER"], []),
    "COVERAGE_REF":            (["REF_QUALIFIER"], []),
    "COVERAGE_ID_CARD":        ([], []),
    "COVERAGE_PROVIDER":       (["PROVIDER_ENTITY_CODE", "PROVIDER_ID"], ["LOOP_SEQ"]),
    "PROVIDER_CONTACT":        ([], []),
    "COB":                     (["PAYER_RESPONSIBILITY", "COB_POLICY_NUMBER"], ["COB_SEQ"]),
    "COB_REF":                 (["REF_QUALIFIER"], []),
    "COB_DATE":                (["DATE_QUALIFIER"], []),
    "COB_ENTITY":              (["ENTITY_CODE", "ENTITY_ID"], ["LOOP_SEQ"]),
    "COB_ENTITY_CONTACT":      ([], []),
    "REPORTING_CATEGORY":      (["CATEGORY_NAME", "REF_ID"], ["LOOP_SEQ"]),
    "UNMAPPED":                ([], []),
}

# NM101 → 2100 loop for member-level names
_MEMBER_NAME_LOOPS = {"IL": "2100A", "74": "2100A", "70": "2100B", "31": "2100C",
                      "36": "2100D", "M8": "2100E", "S3": "2100F", "45": "2100H"}
_RESPONSIBLE_CODES = {"6Y", "9K", "E1", "EI", "EXS", "GB", "GD", "J6", "LR",
                      "QD", "S1", "TZ"}                     # 2100G
_PARTY_LOOPS = {"P5": "1000A", "IN": "1000B", "BO": "1000C", "TV": "1000C"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _v(parts: list, i: int):
    """Element i of a segment: trailing spaces trimmed, empty/missing → None."""
    if i >= len(parts):
        return None
    s = parts[i].rstrip()
    return s if s != "" else None


def _split_range(value):
    """'20260101-20261231' → ('20260101', '20261231'); '20260101' → ('20260101', None)."""
    if value is None:
        return None, None
    if "-" in value:
        start, end = value.split("-", 1)
        return (start.strip() or None), (end.strip() or None)
    return value, None


def _decode(content, encoding=None) -> str:
    if isinstance(content, str):
        return content
    if encoding:
        return content.decode(encoding, errors="replace")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _detect_separators(text: str) -> tuple:
    """
    Reads the delimiters from the ISA segment.
    Returns (text_from_ISA, element_sep, component_sep, repetition_sep, segment_term).
    """
    t = text.lstrip("﻿ \t\r\n")
    if not t.startswith("ISA") or len(t) < 106:
        raise ValueError("EDI834: content does not start with an ISA segment.")

    element_sep = t[3]
    positions = [i for i, ch in enumerate(t[:400]) if ch == element_sep]
    if len(positions) < 16:
        raise ValueError("EDI834: ISA segment has fewer than 16 elements.")

    isa16_pos = positions[15] + 1
    component_sep = t[isa16_pos]
    segment_term = t[isa16_pos + 1]

    isa_fields = t[:isa16_pos + 1].split(element_sep)
    rep = isa_fields[11] if len(isa_fields) > 11 else None
    repetition_sep = rep if rep and len(rep) == 1 and not rep.isalnum() else None

    return t, element_sep, component_sep, repetition_sep, segment_term


def _to_file_id(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return value


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class Edi834Parser:
    """Single-pass, context-tracking 834 parser. Use parse_834() rather than this class."""

    def __init__(self, filename="", file_id=None, code_lists=None, named_qualifiers=None):
        self.filename = filename or None
        self.file_id = _to_file_id(file_id)

        self.code_lists = {k: dict(v) for k, v in _codes.CODE_LISTS.items()}
        for name, values in (code_lists or {}).items():
            self.code_lists.setdefault(name, {}).update(values)

        self.named = {k: dict(v) for k, v in _codes.NAMED_QUALIFIERS.items()}
        for group, values in (named_qualifiers or {}).items():
            self.named.setdefault(group, {}).update(values)

        self.columns = _table_columns(self.named)
        self.tables = {name: [] for name in self.columns}

        self.parties_by_txn = {}      # txn → {"SPONSOR_ID":…, "PAYER_ID":…}
        self.member_rows = {}         # (txn, member_seq) → MEMBER row
        self.subscriber_ids = {}      # (txn, member_seq) → REF*0F
        self.coverage_rows = {}       # (txn, member_seq, coverage_seq) → COVERAGE row

        self.isa, self.gs = {}, {}
        self.isa13 = self.gs06 = self.st02 = None
        self.cur_txn = None
        self.group_txns, self.interchange_txns = [], []
        self._reset_transaction()

    # ── state ────────────────────────────────────────────────────────────────

    def _reset_transaction(self):
        self.bgn_ref = None
        self.area = None
        self.party_seq = 0
        self.cur_party = None
        self.member_seq = 0
        self._reset_member()

    def _reset_member(self):
        self.cur_member = None
        self.member_ctx = {}
        self.member_named = False
        self.other_seq = 0
        self.cur_other = None
        self.disability_seq = 0
        self.cur_disability = None
        self.coverage_seq = 0
        self.category_seq = 0
        self.cur_category = None
        self._reset_coverage()

    def _reset_coverage(self):
        self.cur_coverage = None
        self.coverage_key = None
        self.provider_seq = 0
        self.cur_provider = None
        self.cob_seq = 0
        self.cur_cob = None
        self.cob_entity_seq = 0
        self.cur_cob_entity = None

    def _txn_key(self):
        return (self.isa13, self.gs06, self.st02)

    def _hier(self, pos, loop=None, member=False, coverage=False, cob=False, loop_seq=None):
        mctx = self.member_ctx if member else {}
        return {
            "FILE_ID": self.file_id,
            "FILE_NAME": self.filename,
            "INTERCHANGE_CONTROL_NUMBER": self.isa13,
            "GROUP_CONTROL_NUMBER": self.gs06,
            "TRANSACTION_CONTROL_NUMBER": self.st02,
            "BGN_REFERENCE_ID": self.bgn_ref,
            "SPONSOR_ID": None,
            "PAYER_ID": None,
            "MEMBER_SEQ": self.member_seq if member else None,
            "MEMBER_KEY": None,
            "MEMBER_OCC": None,
            "SUBSCRIBER_ID": None,
            "MEMBER_ID": None,
            "SUBSCRIBER_INDICATOR": mctx.get("SUBSCRIBER_INDICATOR"),
            "RELATIONSHIP": mctx.get("RELATIONSHIP"),
            "MEMBER_MAINTENANCE_TYPE": mctx.get("MAINTENANCE_TYPE"),
            "LOOP_ID": loop or self.area,
            "COVERAGE_SEQ": self.coverage_seq if coverage else None,
            "COVERAGE_KEY": self.coverage_key if coverage else None,
            "COVERAGE_OCC": None,
            "COB_SEQ": self.cob_seq if cob else None,
            "LOOP_SEQ": loop_seq,
            "OCC": None,
            "SEG_POS": pos,
        }

    def _desc(self, list_name, code):
        if code is None:
            return None
        return self.code_lists.get(list_name, {}).get(code)

    def _add(self, table, row):
        self.tables[table].append(row)
        return row

    def _unmapped(self, pos, seg_id, raw, reason):
        row = self._hier(pos, member=self.cur_member is not None,
                         coverage=self.cur_coverage is not None,
                         cob=self.cur_cob is not None)
        row.update({"SEG_ID": seg_id, "SEGMENT_TEXT": raw, "REASON": reason})
        self._add("UNMAPPED", row)

    def _set_named(self, parent, group, qual, *values):
        """Fills the named column(s) for a qualifier on the parent row (first occurrence wins)."""
        if parent is None:
            return
        target = self.named.get(group, {}).get(qual)
        if not target:
            return
        cols = target if isinstance(target, (list, tuple)) else [target]
        for col, value in zip(cols, values):
            if col and parent.get(col) is None:
                parent[col] = value

    # ── main loop ────────────────────────────────────────────────────────────

    def parse(self, text: str) -> dict:
        text, self.el, self.comp, self.rep, term = _detect_separators(text)

        pos = 0
        for raw in text.split(term):
            raw = raw.strip()
            if not raw:
                continue
            pos += 1
            parts = raw.split(self.el)
            seg_id = parts[0].strip().upper()
            handler = getattr(self, f"_seg_{seg_id}", None)
            if handler is None:
                self._unmapped(pos, seg_id, raw, "Unknown segment")
            else:
                handler(parts, pos, raw)

        return self._finalize()

    # ── envelope ─────────────────────────────────────────────────────────────

    def _seg_ISA(self, p, pos, raw):
        s = lambda i: (p[i].strip() or None) if i < len(p) else None
        self.isa = {
            "INTERCHANGE_SENDER_QUALIFIER": s(5), "INTERCHANGE_SENDER_ID": s(6),
            "INTERCHANGE_RECEIVER_QUALIFIER": s(7), "INTERCHANGE_RECEIVER_ID": s(8),
            "INTERCHANGE_DATE": s(9), "INTERCHANGE_TIME": s(10),
            "INTERCHANGE_VERSION": s(12), "ACK_REQUESTED": s(14), "USAGE_INDICATOR": s(15),
        }
        self.isa13 = s(13)
        self.gs06 = self.st02 = None
        self.interchange_txns = []

    def _seg_GS(self, p, pos, raw):
        self.gs = {
            "FUNCTIONAL_ID_CODE": _v(p, 1), "GROUP_SENDER": _v(p, 2),
            "GROUP_RECEIVER": _v(p, 3), "GROUP_DATE": _v(p, 4),
            "GROUP_TIME": _v(p, 5), "VERSION": _v(p, 8),
        }
        self.gs06 = _v(p, 6)
        self.st02 = None
        self.group_txns = []

    def _seg_ST(self, p, pos, raw):
        self.st02 = _v(p, 2)
        self._reset_transaction()
        self.area = "HEADER"
        row = self._hier(pos)
        row.update(self.isa)
        row.update(self.gs)
        row.update({"TRANSACTION_SET_ID": _v(p, 1), "IMPLEMENTATION_REFERENCE": _v(p, 3)})
        self.cur_txn = self._add("TRANSACTION", row)
        self.group_txns.append(row)
        self.interchange_txns.append(row)

    def _seg_BGN(self, p, pos, raw):
        if self.cur_txn is None:
            return self._unmapped(pos, "BGN", raw, "BGN outside a transaction")
        self.bgn_ref = _v(p, 2)
        self.cur_txn.update({
            "PURPOSE_CODE": _v(p, 1), "PURPOSE_DESC": self._desc("BGN01", _v(p, 1)),
            "BGN_REFERENCE_ID": self.bgn_ref, "BGN_DATE": _v(p, 3), "BGN_TIME": _v(p, 4),
            "TIME_ZONE": _v(p, 5), "ORIGINAL_REFERENCE_ID": _v(p, 6),
            "TRANSACTION_TYPE": _v(p, 7),
            "ACTION_CODE": _v(p, 8), "ACTION_DESC": self._desc("BGN08", _v(p, 8)),
        })

    def _seg_SE(self, p, pos, raw):
        if self.cur_txn is not None:
            self.cur_txn["SEGMENT_COUNT"] = _v(p, 1)
        self.cur_txn = None
        self._reset_transaction()

    def _seg_GE(self, p, pos, raw):
        for row in self.group_txns:
            row["GROUP_TRANSACTION_COUNT"] = _v(p, 1)

    def _seg_IEA(self, p, pos, raw):
        for row in self.interchange_txns:
            row["INTERCHANGE_GROUP_COUNT"] = _v(p, 1)

    # ── header: QTY, parties (1000A/B/C) ─────────────────────────────────────

    def _seg_QTY(self, p, pos, raw):
        if self.area != "HEADER":
            return self._unmapped(pos, "QTY", raw, "QTY not expected here")
        qual, qty = _v(p, 1), _v(p, 2)
        row = self._hier(pos)
        row.update({"QUANTITY_QUALIFIER": qual,
                    "QUANTITY_QUALIFIER_DESC": self._desc("QTY01", qual), "QUANTITY": qty})
        self._add("TRANSACTION_QUANTITY", row)
        self._set_named(self.cur_txn, "HEADER_QTY", qual, qty)

    def _seg_N1(self, p, pos, raw):
        code = _v(p, 1)

        # 2750 reporting category name
        if self.area in ("2700", "2750"):
            if self.cur_category is None or self.cur_category.get("CATEGORY_NAME") is not None:
                return self._unmapped(pos, "N1", raw, "N1 in 2700 without its own LX")
            self.cur_category.update({"CATEGORY_ENTITY_CODE": code, "CATEGORY_NAME": _v(p, 2)})
            self.area = self.cur_category["LOOP_ID"] = "2750"
            return

        # 1000A/B/C parties
        if self.cur_member is None and self.area is not None and (
                self.area == "HEADER" or self.area.startswith("1")):
            self.party_seq += 1
            self.area = _PARTY_LOOPS.get(code, "1000")
            row = self._hier(pos, loop_seq=self.party_seq)
            row.update({"PARTY_CODE": code, "PARTY_DESC": self._desc("N101", code),
                        "PARTY_NAME": _v(p, 2), "PARTY_ID_QUALIFIER": _v(p, 3),
                        "PARTY_ID": _v(p, 4)})
            self.cur_party = self._add("PARTY", row)
            parties = self.parties_by_txn.setdefault(self._txn_key(), {})
            if code == "P5":
                parties.setdefault("SPONSOR_ID", _v(p, 4))
            elif code == "IN":
                parties.setdefault("PAYER_ID", _v(p, 4))
            return

        self._unmapped(pos, "N1", raw, "N1 not expected here")

    def _seg_ACT(self, p, pos, raw):
        if self.cur_party is None or self.area != "1000C":
            return self._unmapped(pos, "ACT", raw, "ACT not expected here")
        self.cur_party.update({"ACCOUNT_NUMBER": _v(p, 1), "ACCOUNT_NUMBER_2": _v(p, 6)})
        self.cur_party["LOOP_ID"] = self.area = "1100C"

    # ── member (2000) ────────────────────────────────────────────────────────

    def _seg_INS(self, p, pos, raw):
        if self.cur_txn is None:
            return self._unmapped(pos, "INS", raw, "INS outside a transaction")
        self._reset_member()
        self.member_seq += 1
        self.area = "2000"
        self.member_ctx = {"SUBSCRIBER_INDICATOR": _v(p, 1), "RELATIONSHIP": _v(p, 2),
                           "MAINTENANCE_TYPE": _v(p, 3)}

        ins06 = (_v(p, 6) or "").split(self.comp)
        comp = lambda lst, i: (lst[i].strip() or None) if i < len(lst) else None
        d = self._desc

        row = self._hier(pos, member=True)
        row.update({
            "SUBSCRIBER_INDICATOR": _v(p, 1),
            "RELATIONSHIP": _v(p, 2), "RELATIONSHIP_DESC": d("INS02", _v(p, 2)),
            "MAINTENANCE_TYPE": _v(p, 3), "MAINTENANCE_TYPE_DESC": d("INS03", _v(p, 3)),
            "MAINTENANCE_REASON": _v(p, 4), "MAINTENANCE_REASON_DESC": d("INS04", _v(p, 4)),
            "BENEFIT_STATUS": _v(p, 5), "BENEFIT_STATUS_DESC": d("INS05", _v(p, 5)),
            "MEDICARE_PLAN_CODE": comp(ins06, 0),
            "MEDICARE_PLAN_DESC": d("INS06_1", comp(ins06, 0)),
            "MEDICARE_ELIGIBILITY_REASON": comp(ins06, 1),
            "MEDICARE_ELIGIBILITY_REASON_DESC": d("INS06_2", comp(ins06, 1)),
            "COBRA_EVENT": _v(p, 7), "COBRA_EVENT_DESC": d("INS07", _v(p, 7)),
            "EMPLOYMENT_STATUS": _v(p, 8), "EMPLOYMENT_STATUS_DESC": d("INS08", _v(p, 8)),
            "STUDENT_STATUS": _v(p, 9), "STUDENT_STATUS_DESC": d("INS09", _v(p, 9)),
            "HANDICAP_INDICATOR": _v(p, 10),
            "DEATH_DATE_FORMAT": _v(p, 11), "DEATH_DATE": _v(p, 12),
            "CONFIDENTIALITY_CODE": _v(p, 13), "BIRTH_SEQUENCE": _v(p, 17),
        })
        self.cur_member = self._add("MEMBER", row)
        self.member_rows[self._txn_key() + (self.member_seq,)] = row

    @property
    def _in_member_names(self):
        return self.cur_member is not None and (self.area == "2000" or
                                                (self.area or "").startswith("2100"))

    # ── names, addresses, contacts ───────────────────────────────────────────

    def _fill_name(self, row, prefix, p):
        nm101 = _v(p, 1)
        row.update({
            _pfx(prefix, "ENTITY_CODE"): nm101,
            _pfx(prefix, "ENTITY_DESC"): self._desc("NM101", nm101),
            _pfx(prefix, "ENTITY_TYPE"): _v(p, 2),
            _pfx(prefix, "LAST_NAME"): _v(p, 3),
            _pfx(prefix, "FIRST_NAME"): _v(p, 4),
            _pfx(prefix, "MIDDLE_NAME"): _v(p, 5),
            _pfx(prefix, "PREFIX"): _v(p, 6),
            _pfx(prefix, "SUFFIX"): _v(p, 7),
            _pfx(prefix, "ID_QUALIFIER"): _v(p, 8),
            _pfx(prefix, "ID"): _v(p, 9),
        })

    def _seg_NM1(self, p, pos, raw):
        code = _v(p, 1)

        # 2100A–2100H member names
        if self._in_member_names:
            loop = _MEMBER_NAME_LOOPS.get(code) or ("2100G" if code in _RESPONSIBLE_CODES else None)
            if loop is None:
                return self._unmapped(pos, "NM1", raw, f"Unknown member name entity '{code}'")
            if loop == "2100A":
                if self.member_named:
                    return self._unmapped(pos, "NM1", raw, "Second member name (2100A) for the member")
                self._fill_name(self.cur_member, "MEMBER_", p)
                self.cur_member["LOOP_ID"] = self.area = "2100A"
                self.member_named = True
                return
            self.other_seq += 1
            self.area = loop
            row = self._hier(pos, member=True, loop_seq=self.other_seq)
            self._fill_name(row, "ENTITY_", p)
            self.cur_other = self._add("MEMBER_OTHER_NAME", row)
            return

        # 2310 provider
        if self.area == "2310" and self.cur_provider is not None:
            if self.cur_provider.get("PROVIDER_ENTITY_CODE") is not None:
                return self._unmapped(pos, "NM1", raw, "Second provider name in one 2310 loop")
            self._fill_name(self.cur_provider, "PROVIDER_", p)
            self.cur_provider["ENTITY_RELATIONSHIP"] = _v(p, 10)
            return

        # 2330 COB related entity
        if self.area in ("2320", "2330") and self.cur_cob is not None:
            self.cob_entity_seq += 1
            self.area = "2330"
            row = self._hier(pos, member=True, coverage=True, cob=True,
                             loop_seq=self.cob_entity_seq)
            self._fill_name(row, "ENTITY_", p)
            self.cur_cob_entity = self._add("COB_ENTITY", row)
            return

        self._unmapped(pos, "NM1", raw, "NM1 not expected here")

    def _name_target(self):
        """Row and column prefix that N3/N4 belong to in the current context."""
        if self.area == "2100A":
            return self.cur_member, "MEMBER_"
        if (self.area or "").startswith("2100") and self.cur_other is not None:
            return self.cur_other, "ENTITY_"
        if self.area == "2310" and self.cur_provider is not None:
            return self.cur_provider, "PROVIDER_"
        if self.area == "2330" and self.cur_cob_entity is not None:
            return self.cur_cob_entity, "ENTITY_"
        return None, None

    def _seg_N3(self, p, pos, raw):
        row, prefix = self._name_target()
        if row is None:
            return self._unmapped(pos, "N3", raw, "N3 not expected here")
        row[_pfx(prefix, "ADDRESS_LINE1")] = _v(p, 1)
        row[_pfx(prefix, "ADDRESS_LINE2")] = _v(p, 2)

    def _seg_N4(self, p, pos, raw):
        row, prefix = self._name_target()
        if row is None:
            return self._unmapped(pos, "N4", raw, "N4 not expected here")
        row.update({
            _pfx(prefix, "CITY"): _v(p, 1), _pfx(prefix, "STATE"): _v(p, 2),
            _pfx(prefix, "ZIP"): _v(p, 3), _pfx(prefix, "COUNTRY"): _v(p, 4),
            _pfx(prefix, "LOCATION_QUALIFIER"): _v(p, 5),
            _pfx(prefix, "LOCATION_CODE"): _v(p, 6),
            _pfx(prefix, "COUNTRY_SUBDIVISION"): _v(p, 7),
        })

    def _seg_PER(self, p, pos, raw):
        if self.area == "2100A":
            table, row = "MEMBER_CONTACT", self._hier(pos, member=True)
        elif self.area in ("2100D", "2100E", "2100F", "2100G") and self.cur_other is not None:
            table, row = "MEMBER_CONTACT", self._hier(pos, member=True, loop_seq=self.other_seq)
        elif self.area == "2310" and self.cur_provider is not None:
            table, row = "PROVIDER_CONTACT", self._hier(pos, member=True, coverage=True,
                                                        loop_seq=self.provider_seq)
        elif self.area == "2330" and self.cur_cob_entity is not None:
            table, row = "COB_ENTITY_CONTACT", self._hier(pos, member=True, coverage=True,
                                                          cob=True, loop_seq=self.cob_entity_seq)
        else:
            return self._unmapped(pos, "PER", raw, "PER not expected here")

        row["CONTACT_FUNCTION"] = _v(p, 1)
        row["CONTACT_NAME"] = _v(p, 2)
        for q_idx in (3, 5, 7):
            qual, number = _v(p, q_idx), _v(p, q_idx + 1)
            if qual is None and number is None:
                continue
            col = _PER_QUALIFIER_COLS.get(qual)
            if col and row.get(col) is None:
                row[col] = number
            elif row.get("OTHER_COMM_NUMBER") is None:
                row["OTHER_COMM_QUALIFIER"] = qual
                row["OTHER_COMM_NUMBER"] = number
            else:
                self._unmapped(pos, "PER", raw, f"Extra communication number '{qual}' in PER")
        self._add(table, row)

    # ── member detail (2100A / 2100B) ────────────────────────────────────────

    def _seg_DMG(self, p, pos, raw):
        if self.area == "2100A":
            row = self.cur_member
        elif self.area == "2100B" and self.cur_other is not None:
            row = self.cur_other
        else:
            return self._unmapped(pos, "DMG", raw, "DMG not expected here")

        gender, marital = _v(p, 3), _v(p, 4)
        row.update({"DOB_FORMAT": _v(p, 1), "DATE_OF_BIRTH": _v(p, 2),
                    "GENDER": gender, "GENDER_DESC": self._desc("DMG03", gender),
                    "MARITAL_STATUS": marital,
                    "MARITAL_STATUS_DESC": self._desc("DMG04", marital)})
        if row is not self.cur_member:
            return

        citizen = _v(p, 6)
        row.update({"CITIZENSHIP_STATUS": citizen,
                    "CITIZENSHIP_STATUS_DESC": self._desc("DMG06", citizen),
                    "RACE_CODE_LIST_QUALIFIER": _v(p, 10), "RACE_INDUSTRY_CODE": _v(p, 11)})

        dmg05 = _v(p, 5)
        if dmg05 is None:
            return
        reps = dmg05.split(self.rep) if self.rep else [dmg05]
        codes = []
        for rep in reps:
            comps = rep.split(self.comp)
            c = lambda i: (comps[i].strip() or None) if i < len(comps) else None
            if not any(comps):
                continue
            if c(0):
                codes.append(c(0))
            r = self._hier(pos, member=True)
            r.update({"RACE_ETHNICITY_CODE": c(0), "CODE_LIST_QUALIFIER": c(1),
                      "INDUSTRY_CODE": c(2)})
            self._add("MEMBER_RACE_ETHNICITY", r)
        row["RACE_ETHNICITY_CODES"] = ",".join(codes) or None

    def _member_detail_or_unmapped(self, seg_id, pos, raw):
        if self.area == "2100A" and self.cur_member is not None:
            return True
        self._unmapped(pos, seg_id, raw, f"{seg_id} not expected here")
        return False

    def _seg_EC(self, p, pos, raw):
        if not self._member_detail_or_unmapped("EC", pos, raw):
            return
        row = self._hier(pos, member=True)
        row.update({"CLASS_CODE_1": _v(p, 1), "CLASS_CODE_2": _v(p, 2), "CLASS_CODE_3": _v(p, 3)})
        self._add("MEMBER_EMPLOYMENT_CLASS", row)

    def _seg_ICM(self, p, pos, raw):
        if not self._member_detail_or_unmapped("ICM", pos, raw):
            return
        self.cur_member.update({"INCOME_FREQUENCY": _v(p, 1), "WAGE_AMOUNT": _v(p, 2),
                                "WORK_HOURS": _v(p, 3), "INCOME_LOCATION_ID": _v(p, 4),
                                "SALARY_GRADE": _v(p, 5)})

    def _seg_HLH(self, p, pos, raw):
        if not self._member_detail_or_unmapped("HLH", pos, raw):
            return
        self.cur_member.update({"HEALTH_CODE": _v(p, 1), "HEIGHT": _v(p, 2), "WEIGHT": _v(p, 3)})

    def _seg_LUI(self, p, pos, raw):
        if not self._member_detail_or_unmapped("LUI", pos, raw):
            return
        row = self._hier(pos, member=True)
        row.update({"LANGUAGE_QUALIFIER": _v(p, 1), "LANGUAGE_CODE": _v(p, 2),
                    "LANGUAGE_DESCRIPTION": _v(p, 3), "LANGUAGE_USE": _v(p, 4),
                    "LANGUAGE_USE_DESC": self._desc("LUI04", _v(p, 4))})
        self._add("MEMBER_LANGUAGE", row)

    def _seg_AMT(self, p, pos, raw):
        qual, amount = _v(p, 1), _v(p, 2)
        values = {"AMOUNT_QUALIFIER": qual,
                  "AMOUNT_QUALIFIER_DESC": self._desc("AMT01", qual), "AMOUNT": amount}
        if self.area == "2100A" and self.cur_member is not None:
            row = self._hier(pos, member=True)
            row.update(values)
            self._add("MEMBER_AMOUNT", row)
            return self._set_named(self.cur_member, "MEMBER_AMOUNT", qual, amount)
        if self.area == "2300" and self.cur_coverage is not None:
            row = self._hier(pos, member=True, coverage=True)
            row.update(values)
            self._add("COVERAGE_AMOUNT", row)
            return self._set_named(self.cur_coverage, "COVERAGE_AMOUNT", qual, amount)
        self._unmapped(pos, "AMT", raw, "AMT not expected here")

    # ── REF / DTP (context dependent) ────────────────────────────────────────

    def _seg_REF(self, p, pos, raw):
        qual, ref_id = _v(p, 1), _v(p, 2)
        values = {"REF_QUALIFIER": qual, "REF_QUALIFIER_DESC": self._desc("REF01", qual),
                  "REF_ID": ref_id, "REF_DESCRIPTION": _v(p, 3)}

        if self.area == "HEADER":
            row = self._hier(pos)
            row.update(values)
            self._add("TRANSACTION_REF", row)
            return self._set_named(self.cur_txn, "HEADER_REF", qual, ref_id)

        if self.area == "2000" and self.cur_member is not None:
            row = self._hier(pos, member=True)
            row.update(values)
            self._add("MEMBER_REF", row)
            if qual == "0F":
                self.subscriber_ids.setdefault(self._txn_key() + (self.member_seq,), ref_id)
            return self._set_named(self.cur_member, "MEMBER_REF", qual, ref_id)

        if self.area == "2300" and self.cur_coverage is not None:
            row = self._hier(pos, member=True, coverage=True)
            row.update(values)
            self._add("COVERAGE_REF", row)
            return self._set_named(self.cur_coverage, "COVERAGE_REF", qual, ref_id)

        if self.area == "2320" and self.cur_cob is not None:
            row = self._hier(pos, member=True, coverage=True, cob=True)
            row.update(values)
            self._add("COB_REF", row)
            return self._set_named(self.cur_cob, "COB_REF", qual, ref_id)

        if self.area == "2750" and self.cur_category is not None:
            if self.cur_category.get("REF_QUALIFIER") is not None:
                return self._unmapped(pos, "REF", raw, "Additional REF in one 2750 loop")
            return self.cur_category.update(values)

        self._unmapped(pos, "REF", raw, "REF not expected here")

    def _seg_DTP(self, p, pos, raw):
        qual = _v(p, 1)
        start, end = _split_range(_v(p, 3))
        values = {"DATE_QUALIFIER": qual, "DATE_QUALIFIER_DESC": self._desc("DTP01", qual),
                  "DATE_FORMAT": _v(p, 2), "START_DATE": start, "END_DATE": end}

        routes = {
            "HEADER": ("TRANSACTION_DATE", self.cur_txn, "HEADER_DATE", {}),
            "2000":   ("MEMBER_DATE", self.cur_member, "MEMBER_DATE", {"member": True}),
            "2200":   ("DISABILITY_DATE", self.cur_disability, "DISABILITY_DATE",
                       {"member": True, "loop_seq": self.disability_seq}),
            "2300":   ("COVERAGE_DATE", self.cur_coverage, "COVERAGE_DATE",
                       {"member": True, "coverage": True}),
            "2320":   ("COB_DATE", self.cur_cob, "COB_DATE",
                       {"member": True, "coverage": True, "cob": True}),
        }
        if self.area in routes and routes[self.area][1] is not None:
            table, parent, group, kw = routes[self.area]
            row = self._hier(pos, **kw)
            row.update(values)
            self._add(table, row)
            return self._set_named(parent, group, qual, start, end)

        if self.area == "2750" and self.cur_category is not None:
            if self.cur_category.get("DATE_QUALIFIER") is not None:
                return self._unmapped(pos, "DTP", raw, "Additional DTP in one 2750 loop")
            return self.cur_category.update(values)

        self._unmapped(pos, "DTP", raw, "DTP not expected here")

    # ── disability (2200) ────────────────────────────────────────────────────

    def _seg_DSB(self, p, pos, raw):
        if self.cur_member is None or self.cur_coverage is not None:
            return self._unmapped(pos, "DSB", raw, "DSB not expected here")
        self.disability_seq += 1
        self.area = "2200"
        dtype = _v(p, 1)
        row = self._hier(pos, member=True, loop_seq=self.disability_seq)
        row.update({"DISABILITY_TYPE": dtype, "DISABILITY_TYPE_DESC": self._desc("DSB01", dtype),
                    "DIAGNOSIS_QUALIFIER": _v(p, 7), "DIAGNOSIS_CODE": _v(p, 8)})
        self.cur_disability = self._add("MEMBER_DISABILITY", row)

    # ── coverage (2300) ──────────────────────────────────────────────────────

    def _seg_HD(self, p, pos, raw):
        if self.cur_member is None or self.area in ("2700", "2750"):
            return self._unmapped(pos, "HD", raw, "HD not expected here")
        self._reset_coverage()
        self.coverage_seq += 1
        self.area = "2300"
        hd = lambda i: _v(p, i)
        self.coverage_key = "+".join(x or "" for x in (hd(1), hd(3), hd(4), hd(5)))
        row = self._hier(pos, member=True, coverage=True)
        row.update({
            "MAINTENANCE_TYPE": hd(1), "MAINTENANCE_TYPE_DESC": self._desc("HD01", hd(1)),
            "INSURANCE_LINE": hd(3), "INSURANCE_LINE_DESC": self._desc("HD03", hd(3)),
            "PLAN_COVERAGE_DESCRIPTION": hd(4),
            "COVERAGE_LEVEL": hd(5), "COVERAGE_LEVEL_DESC": self._desc("HD05", hd(5)),
            "LATE_ENROLLMENT_INDICATOR": hd(9),
        })
        self.cur_coverage = self._add("COVERAGE", row)
        self.coverage_rows[self._txn_key() + (self.member_seq, self.coverage_seq)] = row

    def _seg_IDC(self, p, pos, raw):
        if self.area != "2300" or self.cur_coverage is None:
            return self._unmapped(pos, "IDC", raw, "IDC not expected here")
        row = self._hier(pos, member=True, coverage=True)
        row.update({"PLAN_COVERAGE_DESCRIPTION": _v(p, 1), "ID_CARD_TYPE": _v(p, 2),
                    "CARD_QUANTITY": _v(p, 3), "ACTION_CODE": _v(p, 4)})
        self._add("COVERAGE_ID_CARD", row)

    def _seg_LX(self, p, pos, raw):
        # 2310 provider within a coverage
        if self.area in ("2300", "2310") and self.cur_coverage is not None:
            self.provider_seq += 1
            self.area = "2310"
            row = self._hier(pos, member=True, coverage=True, loop_seq=self.provider_seq)
            row["ASSIGNED_NUMBER"] = _v(p, 1)
            self.cur_provider = self._add("COVERAGE_PROVIDER", row)
            return
        # 2710 reporting category
        if self.area in ("2700", "2750") and self.cur_member is not None:
            self.category_seq += 1
            self.area = "2700"
            row = self._hier(pos, loop="2750", member=True, loop_seq=self.category_seq)
            row["ASSIGNED_NUMBER"] = _v(p, 1)
            self.cur_category = self._add("REPORTING_CATEGORY", row)
            return
        self._unmapped(pos, "LX", raw, "LX not expected here")

    def _seg_PRV(self, p, pos, raw):
        if self.area != "2310" or self.cur_provider is None:
            return self._unmapped(pos, "PRV", raw, "PRV not expected here")
        self.cur_provider.update({"PROVIDER_ROLE": _v(p, 1), "TAXONOMY_QUALIFIER": _v(p, 2),
                                  "TAXONOMY_CODE": _v(p, 3)})

    def _seg_PLA(self, p, pos, raw):
        if self.area != "2310" or self.cur_provider is None:
            return self._unmapped(pos, "PLA", raw, "PLA not expected here")
        self.cur_provider.update({"PROVIDER_CHANGE_ACTION": _v(p, 1),
                                  "PROVIDER_CHANGE_ENTITY": _v(p, 2),
                                  "PROVIDER_CHANGE_DATE": _v(p, 3),
                                  "PROVIDER_CHANGE_TIME": _v(p, 4),
                                  "PROVIDER_CHANGE_REASON": _v(p, 5)})

    # ── coordination of benefits (2320 / 2330) ───────────────────────────────

    def _seg_COB(self, p, pos, raw):
        if self.cur_coverage is None or self.area not in ("2300", "2310", "2320", "2330"):
            return self._unmapped(pos, "COB", raw, "COB not expected here")
        self.cob_seq += 1
        self.cob_entity_seq = 0
        self.cur_cob_entity = None
        self.cur_provider = None
        self.area = "2320"
        cob04 = _v(p, 4)
        service_types = ([c.strip() for c in cob04.split(self.rep) if c.strip()]
                         if cob04 and self.rep else ([cob04] if cob04 else []))
        row = self._hier(pos, member=True, coverage=True, cob=True)
        row.update({"PAYER_RESPONSIBILITY": _v(p, 1),
                    "PAYER_RESPONSIBILITY_DESC": self._desc("COB01", _v(p, 1)),
                    "COB_POLICY_NUMBER": _v(p, 2),
                    "COB_CODE": _v(p, 3), "COB_CODE_DESC": self._desc("COB03", _v(p, 3)),
                    "SERVICE_TYPE_CODES": ",".join(service_types) or None})
        self.cur_cob = self._add("COB", row)

    # ── reporting categories (2700 / 2750) ───────────────────────────────────

    def _seg_LS(self, p, pos, raw):
        if self.cur_member is None or _v(p, 1) != "2700" or self.area in ("2700", "2750"):
            return self._unmapped(pos, "LS", raw, "LS not expected here")
        self._reset_coverage()
        self.area = "2700"

    def _seg_LE(self, p, pos, raw):
        if self.area not in ("2700", "2750"):
            return self._unmapped(pos, "LE", raw, "LE without LS")
        self.cur_category = None
        self.area = "2700_END"

    # ── finalize ─────────────────────────────────────────────────────────────

    def _finalize(self) -> dict:
        # member identity, key and occurrence (in file order)
        member_occ_count = {}
        member_info = {}
        for mkey_full, mrow in self.member_rows.items():
            txn, mseq = mkey_full[:3], mkey_full[3]
            sub_id = self.subscriber_ids.get(mkey_full)
            member_id = mrow.get("MEMBER_ID")
            # NM108 = 34: NM109 is the SSN. Keep it only in MEMBER.MEMBER_SSN so
            # it is not copied into MEMBER_ID / MEMBER_KEY on every table (and
            # from there into run-log samples).
            if mrow.get("MEMBER_ID_QUALIFIER") == "34":
                mrow["MEMBER_SSN"] = member_id
                member_id = None
            key = "+".join(x or "" for x in (
                sub_id, mrow.get("RELATIONSHIP"), member_id,
                mrow.get("MEMBER_LAST_NAME"), mrow.get("MEMBER_FIRST_NAME"),
                mrow.get("DATE_OF_BIRTH")))
            count_key = txn + (key,)
            member_occ_count[count_key] = member_occ_count.get(count_key, 0) + 1
            member_info[mkey_full] = {"SUBSCRIBER_ID": sub_id, "MEMBER_ID": member_id,
                                      "MEMBER_KEY": key,
                                      "MEMBER_OCC": member_occ_count[count_key]}

        coverage_occ_count = {}
        coverage_occ = {}
        for ckey_full, crow in self.coverage_rows.items():
            count_key = ckey_full[:4] + (crow["COVERAGE_KEY"],)
            coverage_occ_count[count_key] = coverage_occ_count.get(count_key, 0) + 1
            coverage_occ[ckey_full] = coverage_occ_count[count_key]

        result = {}
        for table, data_cols in self.columns.items():
            rows = self.tables[table]
            data_cols = [c for c in OrderedDict.fromkeys(data_cols) if c not in HIER_COLS]

            for row in rows:
                txn = (row["INTERCHANGE_CONTROL_NUMBER"], row["GROUP_CONTROL_NUMBER"],
                       row["TRANSACTION_CONTROL_NUMBER"])
                parties = self.parties_by_txn.get(txn, {})
                row["SPONSOR_ID"] = parties.get("SPONSOR_ID")
                row["PAYER_ID"] = parties.get("PAYER_ID")
                if row["MEMBER_SEQ"] is not None:
                    row.update(member_info.get(txn + (row["MEMBER_SEQ"],), {}))
                if row["COVERAGE_SEQ"] is not None:
                    row["COVERAGE_OCC"] = coverage_occ.get(
                        txn + (row["MEMBER_SEQ"], row["COVERAGE_SEQ"]))

            df = pd.DataFrame(rows, columns=HIER_COLS + data_cols)
            df = df.astype(object).where(pd.notna(df), None)

            if not df.empty:
                if table == "MEMBER":
                    df["OCC"] = df["MEMBER_OCC"]
                elif table == "COVERAGE":
                    df["OCC"] = df["COVERAGE_OCC"]
                elif table == "TRANSACTION":
                    df["OCC"] = 1
                else:
                    key_cols, exclude = _OCC_SPEC.get(table, ([], []))
                    group_cols = [c for c in _PARENT_COLS if c not in exclude] + key_cols
                    keys = df[group_cols].astype(object).where(df[group_cols].notna(), "\x00")
                    keys = keys.astype(str)
                    df["OCC"] = (keys.groupby(group_cols, sort=False, dropna=False)
                                 .cumcount() + 1).to_numpy()

            for col in _INT_COLS:
                df[col] = pd.array(df[col], dtype="Int64")

            result[table] = df
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_CACHE = OrderedDict()
_CACHE_MAX = 8
# Data_Validation runs test cases in parallel threads: one lock keeps the
# cache consistent and makes a file that several test cases read at the same
# time be parsed once.
_CACHE_LOCK = threading.RLock()


def clear_cache():
    """Drops all cached parse results (they hold PHI in memory)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _blank_null_values(df, null_values):
    """
    NullValues → None. A dict is used on purpose: on pandas 1.x,
    df.replace(list, None) forward-fills from the row above instead of
    blanking; the dict form blanks the value on every pandas version.
    """
    if not null_values or df.empty:
        return df
    return df.replace({v: None for v in null_values})


def parse_834(content, filename: str = "", file_id=None, file_config: dict = None) -> dict:
    """
    Parses an 834 file into all standard tables.

    Args:
        content     : raw bytes or text of the file
        filename    : file name (FILE_NAME column, logging)
        file_id     : file detail ID (FILE_ID column)
        file_config : FileConfig dict — uses Encoding, CodeLists, NamedQualifiers,
                      IncludeDescriptions, NullValues

    Returns:
        dict  table name → DataFrame (every table present, possibly empty)
    """
    file_config = file_config or {}
    text = _decode(content, file_config.get("Encoding"))
    with _CACHE_LOCK:
        overrides = {k: file_config.get(k) for k in
                     ("CodeLists", "NamedQualifiers", "IncludeDescriptions", "NullValues")}
        cache_key = (hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest(),
                     filename, str(file_id),
                     json.dumps(overrides, sort_keys=True, default=str))

        if cache_key in _CACHE:
            _CACHE.move_to_end(cache_key)
            return _CACHE[cache_key]

        parser = Edi834Parser(filename=filename, file_id=file_id,
                              code_lists=file_config.get("CodeLists"),
                              named_qualifiers=file_config.get("NamedQualifiers"))
        tables = parser.parse(text)

        include_desc = file_config.get("IncludeDescriptions", True)
        null_values = [v for v in (file_config.get("NullValues") or []) if v != ""]

        for name, df in tables.items():
            if include_desc is False:
                df = df.drop(columns=[c for c in df.columns if c.endswith("_DESC")])
            df = _blank_null_values(df, null_values)
            tables[name] = df

        counts = ", ".join(f"{k}={len(v)}" for k, v in tables.items() if len(v))
        logger.info(f"EDI834: parsed '{filename}' — {counts}")
        if len(tables["UNMAPPED"]):
            logger.warning(f"EDI834: {len(tables['UNMAPPED'])} segment(s) in '{filename}' "
                           f"could not be placed — see table UNMAPPED.")

        _CACHE[cache_key] = tables
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        return tables


def parse_edi834_for_file_parser(content: bytes, file_config: dict, filename: str = "") -> tuple:
    """
    File_parser.parse_file() entry point for FileConfig.Format = "EDI834".

    FileConfig keys used:
        Table           (required) which table to return, e.g. "MEMBER", "COVERAGE"
        FileDetailId    file detail ID for the FILE_ID column
        plus the keys listed in parse_834()

    Returns:
        (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    file_config = file_config or {}
    table = (file_config.get("Table") or "MEMBER").strip().upper()
    if not file_config.get("Table"):
        logger.info(f"EDI834: FileConfig.Table not set — returning MEMBER.")
    if table not in TABLE_NAMES:
        raise ValueError(f"EDI834: FileConfig.Table '{table}' is not valid. "
                         f"Valid tables: {TABLE_NAMES}")

    file_id = file_config.get("FileDetailId", file_config.get("_FileDetailId"))
    tables = parse_834(content, filename=filename, file_id=file_id, file_config=file_config)

    df = tables[table].copy()
    logger.info(f"EDI834: returning table {table} — {len(df)} row(s) from '{filename}'")
    return df, [], [], []