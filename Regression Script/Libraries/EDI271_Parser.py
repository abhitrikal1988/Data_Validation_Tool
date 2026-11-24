# ─────────────────────────────────────────────────────────────────────────────
# EDI271_Parser.py
# X12 271 Eligibility Benefit Response (005010X279A1) parser for the
# Data Validation framework — FileConfig.Format = "EDI271".
#
# Design
#   - Follows the 271 standard, not any target schema. Every element is
#     extracted into business-named columns; mapping to a target happens only
#     in the test case (JoinFields / CompareFields).
#   - Output = one DataFrame per standard table (see TABLE_COLUMNS).
#   - Every row carries the full hierarchy path (HIER_COLS).
#   - Values kept as text, trailing spaces trimmed, empty → None.
#   - Segments that cannot be placed go to UNMAPPED — nothing is dropped.
#   - A file is parsed once per run; every test case reuses the cached result.
#
# Hook into File_parser.parse_file():
#     from Libraries.Edi271_parser import parse_edi271_for_file_parser
#     parser_map["EDI271"] = parse_edi271_for_file_parser
# ─────────────────────────────────────────────────────────────────────────────

import hashlib
import json
import logging
from collections import OrderedDict

import pandas as pd

try:
    from Libraries import EDI271_Codes as _codes
except ImportError:                      # running from inside Libraries/
    import EDI271_Codes as _codes

logger = logging.getLogger()


# ─────────────────────────────────────────────────────────────────────────────
# Column definitions
# ─────────────────────────────────────────────────────────────────────────────

HIER_COLS = [
    "FILE_ID", "FILE_NAME",
    "INTERCHANGE_CONTROL_NUMBER", "GROUP_CONTROL_NUMBER",
    "TRANSACTION_CONTROL_NUMBER",
    "PAYER_HL", "PAYER_ID",
    "PROVIDER_HL", "PROVIDER_ID",
    "SUBSCRIBER_HL", "SUBSCRIBER_ID",
    "DEPENDENT_HL", "MEMBER_LEVEL",
    "LOOP_ID", "EB_SEQ", "EB_KEY", "ENTITY_SEQ",
    "OCC", "SEG_POS",
]

# Columns identifying a row's parent — used to number OCC
_PARENT_COLS = [
    "FILE_ID", "INTERCHANGE_CONTROL_NUMBER", "GROUP_CONTROL_NUMBER",
    "TRANSACTION_CONTROL_NUMBER", "PAYER_HL", "PROVIDER_HL",
    "SUBSCRIBER_HL", "DEPENDENT_HL", "LOOP_ID", "EB_SEQ", "ENTITY_SEQ",
]

_INT_COLS = ["EB_SEQ", "ENTITY_SEQ", "OCC", "SEG_POS"]


def _pfx(prefix: str, field: str) -> str:
    """Prefixes a field name without doubling (ENTITY_ + ENTITY_CODE → ENTITY_CODE)."""
    return field if field.startswith(prefix) else prefix + field


_NAME_FIELDS = ["ENTITY_CODE", "ENTITY_DESC", "ENTITY_TYPE", "LAST_NAME",
                "FIRST_NAME", "MIDDLE_NAME", "SUFFIX", "ID_QUALIFIER", "ID"]
_ADDRESS_FIELDS = ["ADDRESS_LINE1", "ADDRESS_LINE2", "CITY", "STATE", "ZIP",
                   "COUNTRY", "COUNTRY_SUBDIVISION"]
_PRV_FIELDS = ["PROVIDER_ROLE", "TAXONOMY_QUALIFIER", "TAXONOMY_CODE"]


def _cols(prefix: str, fields: list) -> list:
    return [_pfx(prefix, f) for f in fields]


_CONTACT_COLS = ["CONTACT_FUNCTION", "CONTACT_NAME", "PHONE", "PHONE_EXT",
                 "FAX", "EMAIL", "URL", "OTHER_COMM_QUALIFIER", "OTHER_COMM_NUMBER"]
_REF_COLS = ["REF_QUALIFIER", "REF_QUALIFIER_DESC", "REF_ID", "REF_DESC"]
_DATE_COLS = ["DATE_QUALIFIER", "DATE_QUALIFIER_DESC", "DATE_FORMAT",
              "START_DATE", "END_DATE"]
_AAA_COLS = ["VALID_REQUEST", "REJECT_REASON", "REJECT_REASON_DESC",
             "FOLLOW_UP_ACTION", "FOLLOW_UP_DESC"]

_MEMBER_EXTRA = (
    ["DOB_FORMAT", "DATE_OF_BIRTH", "GENDER", "GENDER_DESC",
     "INS_RESPONSE", "RELATIONSHIP", "RELATIONSHIP_DESC",
     "MAINTENANCE_TYPE", "MAINTENANCE_REASON", "BIRTH_SEQUENCE"]
    + [c for i in range(1, 9) for c in (f"DIAGNOSIS_QUALIFIER_{i}", f"DIAGNOSIS_CODE_{i}")]
    + ["MILITARY_INFO_STATUS", "EMPLOYMENT_STATUS", "GOVT_AFFILIATION",
       "MILITARY_DESC", "MILITARY_RANK", "SERVICE_DATE_FORMAT",
       "SERVICE_START", "SERVICE_END"]
)

_BENEFIT_COLS = [
    "BENEFIT_INFO_CODE", "BENEFIT_INFO_DESC",
    "COVERAGE_LEVEL", "COVERAGE_LEVEL_DESC",
    "SERVICE_TYPE_CODES",
    "INSURANCE_TYPE", "INSURANCE_TYPE_DESC",
    "PLAN_DESCRIPTION",
    "TIME_PERIOD", "TIME_PERIOD_DESC",
    "BENEFIT_AMOUNT", "BENEFIT_PERCENT",
    "QUANTITY_QUALIFIER", "QUANTITY_QUALIFIER_DESC", "QUANTITY",
    "AUTH_REQUIRED", "IN_PLAN_NETWORK",
    "PROCEDURE_QUALIFIER", "PROCEDURE_CODE",
    "MODIFIER_1", "MODIFIER_2", "MODIFIER_3", "MODIFIER_4",
    "PROCEDURE_DESC",
    "DIAG_POINTER_1", "DIAG_POINTER_2", "DIAG_POINTER_3", "DIAG_POINTER_4",
]

# Description columns added by the parser (dropped when IncludeDescriptions=false).
# Data columns that happen to end in _DESC (REF_DESC, PROCEDURE_DESC,
# MILITARY_DESC) are not in this set.
DESC_COLUMNS = {
    "ENTITY_DESC", "PAYER_ENTITY_DESC", "PROVIDER_ENTITY_DESC", "MEMBER_ENTITY_DESC",
    "REF_QUALIFIER_DESC", "DATE_QUALIFIER_DESC", "REJECT_REASON_DESC",
    "FOLLOW_UP_DESC", "GENDER_DESC", "RELATIONSHIP_DESC",
    "BENEFIT_INFO_DESC", "COVERAGE_LEVEL_DESC", "INSURANCE_TYPE_DESC",
    "TIME_PERIOD_DESC", "QUANTITY_QUALIFIER_DESC", "SERVICE_TYPE_DESC",
}


def _table_columns(named_cols: list) -> "OrderedDict[str, list]":
    """Column order per table (hierarchy columns are added in front)."""
    return OrderedDict([
        ("TRANSACTION", [
            "INTERCHANGE_SENDER_QUALIFIER", "INTERCHANGE_SENDER_ID",
            "INTERCHANGE_RECEIVER_QUALIFIER", "INTERCHANGE_RECEIVER_ID",
            "INTERCHANGE_DATE", "INTERCHANGE_TIME", "INTERCHANGE_VERSION",
            "ACK_REQUESTED", "USAGE_INDICATOR",
            "FUNCTIONAL_ID_CODE", "GROUP_SENDER", "GROUP_RECEIVER",
            "GROUP_DATE", "GROUP_TIME", "VERSION",
            "TRANSACTION_SET_ID", "IMPLEMENTATION_REFERENCE",
            "HIERARCHY_STRUCTURE_CODE", "PURPOSE_CODE", "REFERENCE_ID",
            "TRANSACTION_DATE", "TRANSACTION_TIME",
            "SEGMENT_COUNT", "GROUP_TRANSACTION_COUNT", "INTERCHANGE_GROUP_COUNT",
        ]),
        ("PAYER", ["PAYER_HL_CHILD_CODE"] + _cols("PAYER_", _NAME_FIELDS)),
        ("PAYER_CONTACT", _CONTACT_COLS),
        ("PAYER_VALIDATION", _AAA_COLS),
        ("PROVIDER", ["PROVIDER_HL_CHILD_CODE"] + _cols("PROVIDER_", _NAME_FIELDS)
                     + _cols("PROVIDER_", _ADDRESS_FIELDS) + _cols("PROVIDER_", _PRV_FIELDS)),
        ("PROVIDER_REF", _REF_COLS),
        ("PROVIDER_VALIDATION", _AAA_COLS),
        ("MEMBER", ["MEMBER_HL_CHILD_CODE"] + _cols("MEMBER_", _NAME_FIELDS)
                   + _cols("MEMBER_", _ADDRESS_FIELDS) + _cols("MEMBER_", _PRV_FIELDS)
                   + _MEMBER_EXTRA + named_cols),
        ("MEMBER_TRACE", ["TRACE_TYPE", "TRACE_NUMBER", "TRACE_ORIGINATOR_ID",
                          "TRACE_ORIGINATOR_SUPP_ID"]),
        ("MEMBER_REF", _REF_COLS),
        ("MEMBER_DATE", _DATE_COLS),
        ("MEMBER_VALIDATION", _AAA_COLS),
        ("BENEFIT", _BENEFIT_COLS + named_cols),
        ("BENEFIT_SERVICE_TYPE", ["SERVICE_TYPE_CODE", "SERVICE_TYPE_DESC"]),
        ("BENEFIT_DELIVERY", ["QUANTITY_QUALIFIER", "QUANTITY", "UNIT",
                              "SAMPLE_MODULUS", "PERIOD_QUALIFIER", "PERIOD_COUNT",
                              "FREQUENCY", "PATTERN_TIME"]),
        ("BENEFIT_REF", _REF_COLS),
        ("BENEFIT_DATE", _DATE_COLS),
        ("BENEFIT_VALIDATION", _AAA_COLS),
        ("BENEFIT_MESSAGE", ["MESSAGE_TEXT"]),
        ("BENEFIT_INDUSTRY_CODE", ["CODE_LIST_QUALIFIER", "INDUSTRY_CODE",
                                   "CODE_CATEGORY", "FREE_TEXT"]),
        ("BENEFIT_ENTITY", _cols("ENTITY_", _NAME_FIELDS) + _cols("ENTITY_", _ADDRESS_FIELDS)
                           + _cols("ENTITY_", _PRV_FIELDS)),
        ("BENEFIT_ENTITY_CONTACT", _CONTACT_COLS),
        ("UNMAPPED", ["SEG_ID", "SEGMENT_TEXT", "REASON"]),
    ])


TABLE_NAMES = list(_table_columns([]).keys())

# OCC rule: occurrence number of the row's key under its parent.
# Empty key = order under the parent. Tables not listed → OCC = 1.
#   table: (key columns, parent columns to leave out)
_OCC_SPEC = {
    "PAYER_CONTACT":          ([], []),
    "PAYER_VALIDATION":       ([], []),
    "PROVIDER_REF":           (["REF_QUALIFIER"], []),
    "PROVIDER_VALIDATION":    ([], []),
    "MEMBER_TRACE":           (["TRACE_TYPE"], []),
    "MEMBER_REF":             (["REF_QUALIFIER"], []),
    "MEMBER_DATE":            (["DATE_QUALIFIER"], []),
    "MEMBER_VALIDATION":      ([], []),
    "BENEFIT":                (["EB_KEY"], ["EB_SEQ", "ENTITY_SEQ"]),
    "BENEFIT_SERVICE_TYPE":   (["SERVICE_TYPE_CODE"], []),
    "BENEFIT_DELIVERY":       ([], []),
    "BENEFIT_REF":            (["REF_QUALIFIER"], []),
    "BENEFIT_DATE":           (["DATE_QUALIFIER"], []),
    "BENEFIT_VALIDATION":     ([], []),
    "BENEFIT_MESSAGE":        (["MESSAGE_TEXT"], []),
    "BENEFIT_INDUSTRY_CODE":  ([], []),
    "BENEFIT_ENTITY":         (["ENTITY_CODE", "ENTITY_LAST_NAME", "ENTITY_ID"], ["ENTITY_SEQ"]),
    "BENEFIT_ENTITY_CONTACT": ([], []),
    "UNMAPPED":               ([], []),
}

_PER_QUALIFIER_COLS = {"TE": "PHONE", "EX": "PHONE_EXT", "FX": "FAX",
                       "EM": "EMAIL", "UR": "URL"}


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
    """'20260501-20260531' → ('20260501', '20260531'); '20260112' → ('20260112', None)."""
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
    t = text.lstrip("\ufeff \t\r\n")
    if not t.startswith("ISA") or len(t) < 106:
        raise ValueError("EDI271: content does not start with an ISA segment.")

    element_sep = t[3]
    positions = [i for i, ch in enumerate(t[:400]) if ch == element_sep]
    if len(positions) < 16:
        raise ValueError("EDI271: ISA segment has fewer than 16 elements.")

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

class Edi271Parser:
    """Single-pass, context-tracking 271 parser. Use parse_271() rather than this class."""

    def __init__(self, filename="", file_id=None, code_lists=None,
                 named_ref=None, named_dates=None):
        self.filename = filename or None
        self.file_id = _to_file_id(file_id)

        self.code_lists = {k: dict(v) for k, v in _codes.CODE_LISTS.items()}
        for name, values in (code_lists or {}).items():
            self.code_lists.setdefault(name, {}).update(values)

        self.named_ref = dict(_codes.NAMED_REF_QUALIFIERS)
        self.named_ref.update(named_ref or {})
        self.named_dates = {k: list(v) for k, v in _codes.NAMED_DATE_QUALIFIERS.items()}
        self.named_dates.update({k: list(v) for k, v in (named_dates or {}).items()})

        named = list(self.named_ref.values())
        for start_col, end_col in self.named_dates.values():
            named += [c for c in (start_col, end_col) if c]
        self.named_cols = list(OrderedDict.fromkeys(named))
        self.columns = _table_columns(self.named_cols)

        self.tables = {name: [] for name in self.columns}
        self.ids_by_hl = {}       # (isa13, gs06, st02, hl01) → NM109
        self.hl_path = {}         # (isa13, gs06, st02, hl01) → hierarchy dict

        # envelope state
        self.isa = {}
        self.gs = {}
        self.isa13 = self.gs06 = self.st02 = None
        self.cur_txn = None
        self.group_txns = []
        self.interchange_txns = []
        self._reset_transaction()

    # ── state ────────────────────────────────────────────────────────────────

    def _reset_transaction(self):
        self.payer_hl = self.provider_hl = self.sub_hl = self.dep_hl = None
        self.member_level = None
        self.level_letter = None       # "C" subscriber, "D" dependent
        self.area = None               # 2000A, 2100A, ..., 2110C, 2120C
        self.cur_payer = self.cur_provider = self.cur_member = None
        self.nm1_done = False
        self._reset_benefit()
        self.eb_seq = 0

    def _reset_benefit(self):
        self.cur_eb = None
        self.eb_key = None
        self.in_ls = False
        self.cur_entity = None
        self.entity_seq = 0

    def _txn_key(self):
        return (self.isa13, self.gs06, self.st02)

    def _hier(self, pos, loop=None, eb=False, entity=False):
        return {
            "FILE_ID": self.file_id,
            "FILE_NAME": self.filename,
            "INTERCHANGE_CONTROL_NUMBER": self.isa13,
            "GROUP_CONTROL_NUMBER": self.gs06,
            "TRANSACTION_CONTROL_NUMBER": self.st02,
            "PAYER_HL": self.payer_hl,
            "PAYER_ID": None,
            "PROVIDER_HL": self.provider_hl,
            "PROVIDER_ID": None,
            "SUBSCRIBER_HL": self.sub_hl,
            "SUBSCRIBER_ID": None,
            "DEPENDENT_HL": self.dep_hl,
            "MEMBER_LEVEL": self.member_level,
            "LOOP_ID": loop or self.area,
            "EB_SEQ": self.eb_seq if eb else None,
            "EB_KEY": self.eb_key if eb else None,
            "ENTITY_SEQ": self.entity_seq if entity else None,
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
        row = self._hier(pos, eb=self.cur_eb is not None, entity=self.cur_entity is not None)
        row.update({"SEG_ID": seg_id, "SEGMENT_TEXT": raw, "REASON": reason})
        self._add("UNMAPPED", row)

    @property
    def _member_area(self):
        return self.level_letter is not None and self.area in (
            f"2000{self.level_letter}", f"2100{self.level_letter}")

    @property
    def _benefit_area(self):
        return (self.cur_eb is not None and not self.in_ls
                and self.area == f"2110{self.level_letter}")

    # ── main loop ────────────────────────────────────────────────────────────

    def parse(self, text: str) -> dict:
        text, self.el, self.comp, self.rep, term = _detect_separators(text)

        segments = [s.strip() for s in text.split(term)]
        pos = 0
        for raw in segments:
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
            "INTERCHANGE_VERSION": s(12), "ACK_REQUESTED": s(14),
            "USAGE_INDICATOR": s(15),
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
        row = self._hier(pos, loop="HEADER")
        row.update(self.isa)
        row.update(self.gs)
        row.update({"TRANSACTION_SET_ID": _v(p, 1),
                    "IMPLEMENTATION_REFERENCE": _v(p, 3)})
        self.cur_txn = self._add("TRANSACTION", row)
        self.group_txns.append(row)
        self.interchange_txns.append(row)

    def _seg_BHT(self, p, pos, raw):
        if self.cur_txn is None:
            return self._unmapped(pos, "BHT", raw, "BHT outside a transaction")
        self.cur_txn.update({
            "HIERARCHY_STRUCTURE_CODE": _v(p, 1), "PURPOSE_CODE": _v(p, 2),
            "REFERENCE_ID": _v(p, 3), "TRANSACTION_DATE": _v(p, 4),
            "TRANSACTION_TIME": _v(p, 5),
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

    # ── hierarchy ────────────────────────────────────────────────────────────

    def _seg_HL(self, p, pos, raw):
        hl01, hl02, hl03, hl04 = _v(p, 1), _v(p, 2), _v(p, 3), _v(p, 4)
        txn = self._txn_key()
        current = {"PAYER_HL": self.payer_hl, "PROVIDER_HL": self.provider_hl,
                   "SUBSCRIBER_HL": self.sub_hl, "DEPENDENT_HL": self.dep_hl}
        parent = self.hl_path.get(txn + (hl02,), current) if hl02 else {}

        self._reset_benefit()
        self.nm1_done = False

        if hl03 == "20":
            path = {"PAYER_HL": hl01, "PROVIDER_HL": None,
                    "SUBSCRIBER_HL": None, "DEPENDENT_HL": None}
        elif hl03 == "21":
            path = {**parent, "PROVIDER_HL": hl01, "SUBSCRIBER_HL": None, "DEPENDENT_HL": None}
        elif hl03 == "22":
            path = {**parent, "SUBSCRIBER_HL": hl01, "DEPENDENT_HL": None}
        elif hl03 == "23":
            path = {**parent, "DEPENDENT_HL": hl01}
        else:
            self.area = None
            self.level_letter = None
            self.member_level = None
            return self._unmapped(pos, "HL", raw, f"Unknown HL level code '{hl03}'")

        self.hl_path[txn + (hl01,)] = path
        self.payer_hl = path.get("PAYER_HL")
        self.provider_hl = path.get("PROVIDER_HL")
        self.sub_hl = path.get("SUBSCRIBER_HL")
        self.dep_hl = path.get("DEPENDENT_HL")

        if hl03 == "20":
            self.member_level, self.level_letter, self.area = None, None, "2000A"
            row = self._hier(pos)
            row["PAYER_HL_CHILD_CODE"] = hl04
            self.cur_payer = self._add("PAYER", row)
        elif hl03 == "21":
            self.member_level, self.level_letter, self.area = None, None, "2000B"
            row = self._hier(pos)
            row["PROVIDER_HL_CHILD_CODE"] = hl04
            self.cur_provider = self._add("PROVIDER", row)
        else:
            self.eb_seq = 0
            if hl03 == "22":
                self.member_level, self.level_letter = "SUBSCRIBER", "C"
            else:
                self.member_level, self.level_letter = "DEPENDENT", "D"
            self.area = f"2000{self.level_letter}"
            row = self._hier(pos)
            row["MEMBER_HL_CHILD_CODE"] = hl04
            self.cur_member = self._add("MEMBER", row)

    # ── names, addresses, provider info ──────────────────────────────────────

    def _fill_name(self, row, prefix, p):
        nm101 = _v(p, 1)
        row.update({
            _pfx(prefix, "ENTITY_CODE"): nm101,
            _pfx(prefix, "ENTITY_DESC"): self._desc("NM101", nm101),
            _pfx(prefix, "ENTITY_TYPE"): _v(p, 2),
            _pfx(prefix, "LAST_NAME"): _v(p, 3),
            _pfx(prefix, "FIRST_NAME"): _v(p, 4),
            _pfx(prefix, "MIDDLE_NAME"): _v(p, 5),
            _pfx(prefix, "SUFFIX"): _v(p, 7),
            _pfx(prefix, "ID_QUALIFIER"): _v(p, 8),
            _pfx(prefix, "ID"): _v(p, 9),
        })

    def _seg_NM1(self, p, pos, raw):
        txn = self._txn_key()

        if self.in_ls:
            self.entity_seq += 1
            loop = f"2120{self.level_letter}"
            row = self._hier(pos, loop=loop, eb=True, entity=True)
            self._fill_name(row, "ENTITY_", p)
            self.cur_entity = self._add("BENEFIT_ENTITY", row)
            return

        if self.area == "2000A" and self.cur_payer is not None and not self.nm1_done:
            self._fill_name(self.cur_payer, "PAYER_", p)
            self.cur_payer["LOOP_ID"] = self.area = "2100A"
            self.ids_by_hl[txn + (self.payer_hl,)] = _v(p, 9)
            self.nm1_done = True
            return

        if self.area == "2000B" and self.cur_provider is not None and not self.nm1_done:
            self._fill_name(self.cur_provider, "PROVIDER_", p)
            self.cur_provider["LOOP_ID"] = self.area = "2100B"
            self.ids_by_hl[txn + (self.provider_hl,)] = _v(p, 9)
            self.nm1_done = True
            return

        if (self.level_letter and self.area == f"2000{self.level_letter}"
                and self.cur_member is not None and not self.nm1_done):
            self._fill_name(self.cur_member, "MEMBER_", p)
            self.cur_member["LOOP_ID"] = self.area = f"2100{self.level_letter}"
            hl = self.dep_hl if self.level_letter == "D" else self.sub_hl
            self.ids_by_hl[txn + (hl,)] = _v(p, 9)
            self.nm1_done = True
            return

        reason = ("NM1 after EB outside an entity block (LS/LE)"
                  if self.cur_eb is not None else "NM1 not expected here")
        self._unmapped(pos, "NM1", raw, reason)

    def _address_target(self):
        """Row and prefix that N3/N4/PRV belong to in the current context."""
        if self.in_ls:
            return (self.cur_entity, "ENTITY_") if self.cur_entity is not None else (None, None)
        if self.area == "2100B":
            return self.cur_provider, "PROVIDER_"
        if self.level_letter and self.area == f"2100{self.level_letter}":
            return self.cur_member, "MEMBER_"
        return None, None

    def _seg_N3(self, p, pos, raw):
        row, prefix = self._address_target()
        if row is None:
            return self._unmapped(pos, "N3", raw, "N3 not expected here")
        row[_pfx(prefix, "ADDRESS_LINE1")] = _v(p, 1)
        row[_pfx(prefix, "ADDRESS_LINE2")] = _v(p, 2)

    def _seg_N4(self, p, pos, raw):
        row, prefix = self._address_target()
        if row is None:
            return self._unmapped(pos, "N4", raw, "N4 not expected here")
        row.update({
            _pfx(prefix, "CITY"): _v(p, 1),
            _pfx(prefix, "STATE"): _v(p, 2),
            _pfx(prefix, "ZIP"): _v(p, 3),
            _pfx(prefix, "COUNTRY"): _v(p, 4),
            _pfx(prefix, "COUNTRY_SUBDIVISION"): _v(p, 7),
        })

    def _seg_PRV(self, p, pos, raw):
        row, prefix = self._address_target()
        if row is None:
            return self._unmapped(pos, "PRV", raw, "PRV not expected here")
        row.update({
            _pfx(prefix, "PROVIDER_ROLE"): _v(p, 1),
            _pfx(prefix, "TAXONOMY_QUALIFIER"): _v(p, 2),
            _pfx(prefix, "TAXONOMY_CODE"): _v(p, 3),
        })

    def _seg_PER(self, p, pos, raw):
        if self.in_ls and self.cur_entity is not None:
            row = self._hier(pos, loop=f"2120{self.level_letter}", eb=True, entity=True)
            table = "BENEFIT_ENTITY_CONTACT"
        elif self.area == "2100A":
            row = self._hier(pos)
            table = "PAYER_CONTACT"
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
        self._add(table, row)

    # ── member detail ────────────────────────────────────────────────────────

    def _member_row_or_unmapped(self, seg_id, pos, raw):
        if self.level_letter and self.area == f"2100{self.level_letter}" and self.cur_member is not None:
            return self.cur_member
        self._unmapped(pos, seg_id, raw, f"{seg_id} not expected here")
        return None

    def _seg_DMG(self, p, pos, raw):
        row = self._member_row_or_unmapped("DMG", pos, raw)
        if row is None:
            return
        gender = _v(p, 3)
        row.update({"DOB_FORMAT": _v(p, 1), "DATE_OF_BIRTH": _v(p, 2),
                    "GENDER": gender, "GENDER_DESC": self._desc("DMG03", gender)})

    def _seg_INS(self, p, pos, raw):
        row = self._member_row_or_unmapped("INS", pos, raw)
        if row is None:
            return
        rel = _v(p, 2)
        row.update({"INS_RESPONSE": _v(p, 1), "RELATIONSHIP": rel,
                    "RELATIONSHIP_DESC": self._desc("INS02", rel),
                    "MAINTENANCE_TYPE": _v(p, 3), "MAINTENANCE_REASON": _v(p, 4),
                    "BIRTH_SEQUENCE": _v(p, 17)})

    def _seg_HI(self, p, pos, raw):
        row = self._member_row_or_unmapped("HI", pos, raw)
        if row is None:
            return
        for i in range(1, 9):
            value = _v(p, i)
            if value is None:
                continue
            comps = value.split(self.comp)
            row[f"DIAGNOSIS_QUALIFIER_{i}"] = comps[0].strip() or None
            row[f"DIAGNOSIS_CODE_{i}"] = (comps[1].strip() or None) if len(comps) > 1 else None

    def _seg_MPI(self, p, pos, raw):
        row = self._member_row_or_unmapped("MPI", pos, raw)
        if row is None:
            return
        start, end = _split_range(_v(p, 7))
        row.update({"MILITARY_INFO_STATUS": _v(p, 1), "EMPLOYMENT_STATUS": _v(p, 2),
                    "GOVT_AFFILIATION": _v(p, 3), "MILITARY_DESC": _v(p, 4),
                    "MILITARY_RANK": _v(p, 5), "SERVICE_DATE_FORMAT": _v(p, 6),
                    "SERVICE_START": start, "SERVICE_END": end})

    def _seg_TRN(self, p, pos, raw):
        if not self._member_area or self.cur_eb is not None:
            return self._unmapped(pos, "TRN", raw, "TRN not expected here")
        row = self._hier(pos)
        row.update({"TRACE_TYPE": _v(p, 1), "TRACE_NUMBER": _v(p, 2),
                    "TRACE_ORIGINATOR_ID": _v(p, 3), "TRACE_ORIGINATOR_SUPP_ID": _v(p, 4)})
        self._add("MEMBER_TRACE", row)

    # ── REF / DTP / AAA (context dependent) ──────────────────────────────────

    def _seg_REF(self, p, pos, raw):
        qual = _v(p, 1)
        values = {"REF_QUALIFIER": qual, "REF_QUALIFIER_DESC": self._desc("REF01", qual),
                  "REF_ID": _v(p, 2), "REF_DESC": _v(p, 3)}

        if self.area in ("2000B", "2100B"):
            row = self._hier(pos)
            row.update(values)
            return self._add("PROVIDER_REF", row)

        if self.level_letter and self.area == f"2100{self.level_letter}":
            row = self._hier(pos)
            row.update(values)
            self._add("MEMBER_REF", row)
            return self._set_named_ref(self.cur_member, qual, values["REF_ID"])

        if self._benefit_area:
            row = self._hier(pos, eb=True)
            row.update(values)
            self._add("BENEFIT_REF", row)
            return self._set_named_ref(self.cur_eb, qual, values["REF_ID"])

        self._unmapped(pos, "REF", raw, "REF not expected here")

    def _set_named_ref(self, parent, qual, ref_id):
        col = self.named_ref.get(qual)
        if parent is not None and col and parent.get(col) is None:
            parent[col] = ref_id

    def _seg_DTP(self, p, pos, raw):
        qual = _v(p, 1)
        start, end = _split_range(_v(p, 3))
        values = {"DATE_QUALIFIER": qual, "DATE_QUALIFIER_DESC": self._desc("DTP01", qual),
                  "DATE_FORMAT": _v(p, 2), "START_DATE": start, "END_DATE": end}

        if self.level_letter and self.area == f"2100{self.level_letter}":
            row = self._hier(pos)
            row.update(values)
            self._add("MEMBER_DATE", row)
            return self._set_named_date(self.cur_member, qual, start, end)

        if self._benefit_area:
            row = self._hier(pos, eb=True)
            row.update(values)
            self._add("BENEFIT_DATE", row)
            return self._set_named_date(self.cur_eb, qual, start, end)

        self._unmapped(pos, "DTP", raw, "DTP not expected here")

    def _set_named_date(self, parent, qual, start, end):
        cols = self.named_dates.get(qual)
        if parent is None or not cols:
            return
        start_col, end_col = cols[0], (cols[1] if len(cols) > 1 else None)
        if start_col and parent.get(start_col) is None:
            parent[start_col] = start
        if end_col and parent.get(end_col) is None:
            parent[end_col] = end

    def _seg_AAA(self, p, pos, raw):
        reason, action = _v(p, 3), _v(p, 4)
        values = {"VALID_REQUEST": _v(p, 1), "REJECT_REASON": reason,
                  "REJECT_REASON_DESC": self._desc("AAA03", reason),
                  "FOLLOW_UP_ACTION": action, "FOLLOW_UP_DESC": self._desc("AAA04", action)}

        if self.area in ("2000A", "2100A"):
            table, row = "PAYER_VALIDATION", self._hier(pos)
        elif self.area in ("2000B", "2100B"):
            table, row = "PROVIDER_VALIDATION", self._hier(pos)
        elif self._member_area and self.cur_eb is None:
            table, row = "MEMBER_VALIDATION", self._hier(pos)
        elif self._benefit_area:
            table, row = "BENEFIT_VALIDATION", self._hier(pos, eb=True)
        else:
            return self._unmapped(pos, "AAA", raw, "AAA not expected here")
        row.update(values)
        self._add(table, row)

    # ── benefits ─────────────────────────────────────────────────────────────

    def _seg_EB(self, p, pos, raw):
        if self.level_letter is None or self.cur_member is None:
            return self._unmapped(pos, "EB", raw, "EB outside a subscriber/dependent")

        self._reset_benefit()
        self.eb_seq += 1
        self.area = f"2110{self.level_letter}"

        eb03 = _v(p, 3)
        if eb03 is None:
            service_types = []
        elif self.rep:
            service_types = [c.strip() for c in eb03.split(self.rep) if c.strip()]
        else:
            service_types = [eb03]

        eb13 = (_v(p, 13) or "").split(self.comp)
        eb14 = (_v(p, 14) or "").split(self.comp)
        comp = lambda lst, i: (lst[i].strip() or None) if i < len(lst) else None

        code = lambda i: _v(p, i)
        values = {
            "BENEFIT_INFO_CODE": code(1), "BENEFIT_INFO_DESC": self._desc("EB01", code(1)),
            "COVERAGE_LEVEL": code(2), "COVERAGE_LEVEL_DESC": self._desc("EB02", code(2)),
            "SERVICE_TYPE_CODES": ",".join(service_types) or None,
            "INSURANCE_TYPE": code(4), "INSURANCE_TYPE_DESC": self._desc("EB04", code(4)),
            "PLAN_DESCRIPTION": code(5),
            "TIME_PERIOD": code(6), "TIME_PERIOD_DESC": self._desc("EB06", code(6)),
            "BENEFIT_AMOUNT": code(7), "BENEFIT_PERCENT": code(8),
            "QUANTITY_QUALIFIER": code(9),
            "QUANTITY_QUALIFIER_DESC": self._desc("EB09", code(9)),
            "QUANTITY": code(10),
            "AUTH_REQUIRED": code(11), "IN_PLAN_NETWORK": code(12),
            "PROCEDURE_QUALIFIER": comp(eb13, 0), "PROCEDURE_CODE": comp(eb13, 1),
            "MODIFIER_1": comp(eb13, 2), "MODIFIER_2": comp(eb13, 3),
            "MODIFIER_3": comp(eb13, 4), "MODIFIER_4": comp(eb13, 5),
            "PROCEDURE_DESC": comp(eb13, 6),
            "DIAG_POINTER_1": comp(eb14, 0), "DIAG_POINTER_2": comp(eb14, 1),
            "DIAG_POINTER_3": comp(eb14, 2), "DIAG_POINTER_4": comp(eb14, 3),
        }

        key_parts = [code(1), code(2), code(4), code(5), code(6), code(7), code(9), code(10)]
        key_parts.append(",".join(sorted(service_types)))
        self.eb_key = "+".join(x or "" for x in key_parts)

        row = self._hier(pos, eb=True)
        row.update(values)
        self.cur_eb = self._add("BENEFIT", row)

        for st in service_types:
            st_row = self._hier(pos, eb=True)
            st_row.update({"SERVICE_TYPE_CODE": st,
                           "SERVICE_TYPE_DESC": self._desc("EB03", st)})
            self._add("BENEFIT_SERVICE_TYPE", st_row)

    def _benefit_child(self, seg_id, pos, raw):
        if not self._benefit_area:
            self._unmapped(pos, seg_id, raw, f"{seg_id} not expected here")
            return None
        return self._hier(pos, eb=True)

    def _seg_HSD(self, p, pos, raw):
        row = self._benefit_child("HSD", pos, raw)
        if row is None:
            return
        row.update({"QUANTITY_QUALIFIER": _v(p, 1), "QUANTITY": _v(p, 2),
                    "UNIT": _v(p, 3), "SAMPLE_MODULUS": _v(p, 4),
                    "PERIOD_QUALIFIER": _v(p, 5), "PERIOD_COUNT": _v(p, 6),
                    "FREQUENCY": _v(p, 7), "PATTERN_TIME": _v(p, 8)})
        self._add("BENEFIT_DELIVERY", row)

    def _seg_MSG(self, p, pos, raw):
        row = self._benefit_child("MSG", pos, raw)
        if row is None:
            return
        row["MESSAGE_TEXT"] = _v(p, 1)
        self._add("BENEFIT_MESSAGE", row)

    def _seg_III(self, p, pos, raw):
        row = self._benefit_child("III", pos, raw)
        if row is None:
            return
        row.update({"CODE_LIST_QUALIFIER": _v(p, 1), "INDUSTRY_CODE": _v(p, 2),
                    "CODE_CATEGORY": _v(p, 3), "FREE_TEXT": _v(p, 4)})
        self._add("BENEFIT_INDUSTRY_CODE", row)

    def _seg_LS(self, p, pos, raw):
        if self.cur_eb is None or self.in_ls:
            return self._unmapped(pos, "LS", raw, "LS not expected here")
        self.in_ls = True
        self.cur_entity = None
        self.area = f"2120{self.level_letter}"

    def _seg_LE(self, p, pos, raw):
        if not self.in_ls:
            return self._unmapped(pos, "LE", raw, "LE without LS")
        self.in_ls = False
        self.cur_entity = None
        self.area = f"2110{self.level_letter}"

    # ── finalize ─────────────────────────────────────────────────────────────

    def _finalize(self) -> dict:
        result = {}
        for table, data_cols in self.columns.items():
            rows = self.tables[table]
            # PAYER_ID / PROVIDER_ID are both hierarchy and name columns — keep one
            data_cols = [c for c in data_cols if c not in HIER_COLS]

            for row in rows:
                txn = (row["INTERCHANGE_CONTROL_NUMBER"], row["GROUP_CONTROL_NUMBER"],
                       row["TRANSACTION_CONTROL_NUMBER"])
                for hl_col, id_col in (("PAYER_HL", "PAYER_ID"),
                                       ("PROVIDER_HL", "PROVIDER_ID"),
                                       ("SUBSCRIBER_HL", "SUBSCRIBER_ID")):
                    if row.get(hl_col) is not None:
                        row[id_col] = self.ids_by_hl.get(txn + (row[hl_col],))

            df = pd.DataFrame(rows, columns=HIER_COLS + data_cols)
            df = df.astype(object).where(pd.notna(df), None)

            spec = _OCC_SPEC.get(table)
            if spec is None or df.empty:
                df["OCC"] = 1 if not df.empty else df["OCC"]
            else:
                key_cols, exclude = spec
                group_cols = [c for c in _PARENT_COLS if c not in exclude] + key_cols
                # None → marker string so rows with empty keys still group together
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
_CACHE_MAX = 16


def clear_cache():
    """Drops all cached parse results (they hold PHI in memory)."""
    _CACHE.clear()


def parse_271(content, filename: str = "", file_id=None,
              file_config: dict = None) -> dict:
    """
    Parses a 271 file into all standard tables.

    Args:
        content     : raw bytes or text of the file
        filename    : file name (FILE_NAME column, logging)
        file_id     : file detail ID (FILE_ID column)
        file_config : FileConfig dict — uses Encoding, CodeLists,
                      NamedRefQualifiers, NamedDateQualifiers,
                      IncludeDescriptions, NullValues

    Returns:
        dict  table name → DataFrame (every table present, possibly empty)
    """
    file_config = file_config or {}
    text = _decode(content, file_config.get("Encoding"))

    overrides = {k: file_config.get(k) for k in
                 ("CodeLists", "NamedRefQualifiers", "NamedDateQualifiers",
                  "IncludeDescriptions", "NullValues")}
    cache_key = (hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest(),
                 filename, str(file_id),
                 json.dumps(overrides, sort_keys=True, default=str))

    if cache_key in _CACHE:
        _CACHE.move_to_end(cache_key)
        return _CACHE[cache_key]

    parser = Edi271Parser(
        filename=filename,
        file_id=file_id,
        code_lists=file_config.get("CodeLists"),
        named_ref=file_config.get("NamedRefQualifiers"),
        named_dates=file_config.get("NamedDateQualifiers"),
    )
    tables = parser.parse(text)

    include_desc = file_config.get("IncludeDescriptions", True)
    null_values = [v for v in (file_config.get("NullValues") or []) if v != ""]

    for name, df in tables.items():
        if include_desc is False:
            df = df.drop(columns=[c for c in df.columns if c in DESC_COLUMNS])
        if null_values:
            df = df.replace(null_values, None)
        tables[name] = df

    counts = ", ".join(f"{k}={len(v)}" for k, v in tables.items() if len(v))
    logger.info(f"EDI271: parsed '{filename}' — {counts}")
    if len(tables["UNMAPPED"]):
        logger.warning(f"EDI271: {len(tables['UNMAPPED'])} segment(s) in '{filename}' "
                       f"could not be placed — see table UNMAPPED.")

    _CACHE[cache_key] = tables
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return tables


def parse_edi271_for_file_parser(content: bytes, file_config: dict,
                                 filename: str = "") -> tuple:
    """
    File_parser.parse_file() entry point for FileConfig.Format = "EDI271".

    FileConfig keys used:
        Table           (required) which table to return, e.g. "BENEFIT"
        FileDetailId    file detail ID for the FILE_ID column (set by the
                        fetcher when known)
        plus the keys listed in parse_271()

    Returns:
        (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    file_config = file_config or {}
    table = (file_config.get("Table") or "").upper()
    if table not in TABLE_NAMES:
        raise ValueError(f"EDI271: FileConfig.Table '{table}' is not valid. "
                         f"Valid tables: {TABLE_NAMES}")

    file_id = file_config.get("FileDetailId", file_config.get("_FileDetailId"))
    tables = parse_271(content, filename=filename, file_id=file_id,
                       file_config=file_config)

    df = tables[table].copy()
    logger.info(f"EDI271: returning table {table} — {len(df)} row(s) from '{filename}'")
    return df, [], [], []