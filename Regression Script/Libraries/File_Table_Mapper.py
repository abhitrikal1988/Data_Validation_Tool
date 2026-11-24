"""
File_Table_Mapper.py
====================
Test case generation for file formats whose parser returns several tables —
EDI271 (EDI271_Parser: MEMBER, BENEFIT, BENEFIT_DATE …), EDI834 (Edi834_parser:
MEMBER, COVERAGE, COB …) and EDI837 (Edi837_parser: CLAIM, LINE …).
The counterpart of Array_Profiler, which does the same for MongoDB arrays.

One EDI file is loaded into several database tables. For each database table:

  1. match_table   — which 271 table it holds: by column names (abbreviations
                     allowed: MBR_LST_NM ~ MEMBER_LAST_NAME, ELIG_START_DT ~
                     ELIGIBILITY_START_DATE, DOB ~ DATE_OF_BIRTH), by the
                     hierarchy columns it carries (EB_SEQ → a benefit table)
                     and by its table name (…_BENEFIT_DATE → BENEFIT_DATE).
                     --map TABLE=271_TABLE overrides it, SKIP leaves it out.
  2. map_columns   — pairs its columns with the 271 table's columns.
  3. join keys     — the smallest set of paired columns that identifies every
                     row of that 271 table in the file (Data_Profiler
                     find_unique_key); for a table with no rows in the sample,
                     the table's natural key (transaction, subscriber,
                     dependent, EB sequence, qualifier, occurrence).
  4. build_child_testcase — a ready SRC_TGT (file table vs Oracle SELECT).

Values from the file are read in memory for this run only (key search); no
value is written to the test case or sent to the model.
"""

import copy
import logging
import re

import pandas as pd

from Libraries.Array_Profiler import name_tokens, squash, tokens_match


def edi271():
    """The 271 parser module — EDI271_Parser.py (or the older name Edi271_parser.py)."""
    try:
        from Libraries import EDI271_Parser as module
    except ModuleNotFoundError as exc:
        if not str(getattr(exc, "name", "")).endswith("EDI271_Parser"):
            raise                           # a real error inside the parser
        from Libraries import Edi271_parser as module
    return module


# Multi-table EDI formats: parser module, its parse function, and the short
# label used in test case descriptions and the run log ("271 BENEFIT").
FORMATS = {
    "EDI271": {"label": "271", "parse": "parse_271"},
    "EDI834": {"label": "834", "parse": "parse_834", "module": "Edi834_parser"},
    "EDI837": {"label": "837", "parse": "parse_837", "module": "Edi837_parser"},
}
# Columns that identify a row's parent, in order — the key of a table that has
# no rows in the sample file (the 271 uses its own rule, _natural_key).
_PARENT_KEYS = {
    "EDI834": ["MEMBER_KEY", "COVERAGE_KEY", "COB_SEQ", "LOOP_SEQ"],
    "EDI837": ["CLAIM_ID", "LINE_NUMBER", "OTHER_SUBSCRIBER_SEQ", "LOOP_SEQ"],
}
# Business identity of a record, tried in order before any other key: the
# database keeps these (subscriber number, claim ID …), not file positions.
_IDENTITY = {
    "EDI834": [["MEMBER_ID"],
               ["SUBSCRIBER_ID", "MEMBER_FIRST_NAME", "DATE_OF_BIRTH"],
               ["SUBSCRIBER_NUMBER", "MEMBER_FIRST_NAME", "DATE_OF_BIRTH"],
               ["SUBSCRIBER_ID", "RELATIONSHIP", "DATE_OF_BIRTH"],
               ["SUBSCRIBER_ID", "RELATIONSHIP"],
               ["MEMBER_KEY"]],
    "EDI837": [["CLAIM_ID"]],
}
# What identifies a row below its parent record, per table.
_TABLE_KEYS = {
    "EDI834": {"COVERAGE": ["INSURANCE_LINE", "PLAN_COVERAGE_DESCRIPTION"],
               "COB": ["PAYER_RESPONSIBILITY", "COB_POLICY_NUMBER"]},
    "EDI837": {},
}


def parser_module(fmt: str = "EDI271"):
    """The parser module of a multi-table format."""
    fmt = str(fmt or "EDI271").upper()
    if fmt == "EDI271":
        return edi271()
    if fmt not in FORMATS:
        raise ValueError(f"{fmt} files have a single table")
    import importlib
    return importlib.import_module(f"Libraries.{FORMATS[fmt]['module']}")


def label(fmt: str = "EDI271") -> str:
    """'271', '834', '837' — as in "271 BENEFIT"."""
    return FORMATS.get(str(fmt or "EDI271").upper(), {}).get("label", str(fmt))

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

def file_format(definition: dict) -> str:
    return str(((definition or {}).get("FileConfig") or {}).get("Format") or "").upper()


def read_file_tables(source_def: dict) -> dict:
    """{table name: DataFrame} for every table of every file the definition selects."""
    from Libraries.File_fetcher import fetch_file
    fmt = file_format(source_def)
    if fmt not in FORMATS:
        raise ValueError(f"{fmt} files have a single table")
    module = parser_module(fmt)
    parse, TABLE_NAMES = getattr(module, FORMATS[fmt]["parse"]), module.TABLE_NAMES
    file_config = dict(source_def.get("FileConfig") or {})
    parts = {name: [] for name in TABLE_NAMES}
    for filename, content in fetch_file(source_def, retry_attempts=1, retry_wait_minutes=0):
        tables = parse(content, filename=filename,
                       file_id=file_config.get("FileDetailId"), file_config=file_config)
        for name, df in tables.items():
            parts[name].append(df)
    return {name: (pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else
                   (dfs[0] if dfs else pd.DataFrame()))
            for name, dfs in parts.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Column names
# ─────────────────────────────────────────────────────────────────────────────

# Database spellings that abbreviation matching cannot work out.
SYNONYMS = {
    "dob": ["date", "of", "birth"],
    "bdate": ["date", "of", "birth"], "sex": ["gender"],
    "npi": ["id"], "txn": ["transaction"], "trx": ["transaction"],
    "no": ["number"], "nbr": ["number"], "num": ["number"],
    "eb": ["benefit"], "ben": ["benefit"], "elig": ["eligibility"],
    "eff": ["start"], "effective": ["start"], "term": ["end"], "termination": ["end"],
    "exp": ["end"], "begin": ["start"], "beg": ["start"], "fname": ["first", "name"],
    "lname": ["last", "name"], "mname": ["middle", "name"],
    # 834 / 837
    "dx": ["diagnosis"], "pos": ["place", "of", "service"], "dos": ["service", "date"],
    "ssn": ["ssn"], "rel": ["relationship"], "rltn": ["relationship"],
}
# Suffixes a database adds to a code or flag column (REJECT_REASON_CD, VALID_REQUEST_IND).
SUFFIX_WORDS = {"cd", "code", "ind", "indicator", "flg", "flag", "val", "value", "txt", "number",
                "amt", "amount"}
# Prefixes the 271 tables put on their columns (MEMBER_LAST_NAME, PAYER_ID …).
TABLE_PREFIXES = (["member"], ["payer"], ["provider"], ["entity"], ["subscriber"],
                  ["billing"], ["payto"], ["patient"])

HIER_COMMON = {"FILE_ID", "FILE_NAME", "INTERCHANGE_CONTROL_NUMBER", "GROUP_CONTROL_NUMBER",
               "TRANSACTION_CONTROL_NUMBER", "LOOP_ID", "OCC", "SEG_POS"}


def _family_hier(table: str, fmt: str = "EDI271", frame: pd.DataFrame = None) -> set:
    """Hierarchy columns that carry values in a table (EB_SEQ only on benefit tables …).
    834/837: the hierarchy columns that have values in the parsed table (all of
    them when the table has no rows)."""
    fmt = str(fmt or "EDI271").upper()
    if fmt != "EDI271":
        hier = list(parser_module(fmt).HIER_COLS)
        if frame is None or frame.empty:
            return set(hier)
        return {c for c in hier if c in frame.columns and frame[c].notna().any()}
    cols = set()
    if table.startswith(("PAYER", "PROVIDER", "MEMBER", "BENEFIT")):
        cols |= {"PAYER_HL", "PAYER_ID"}
    if table.startswith(("PROVIDER", "MEMBER", "BENEFIT")):
        cols |= {"PROVIDER_HL", "PROVIDER_ID"}
    if table.startswith(("MEMBER", "BENEFIT")):
        cols |= {"SUBSCRIBER_HL", "SUBSCRIBER_ID", "DEPENDENT_HL", "MEMBER_LEVEL"}
    if table.startswith("BENEFIT"):
        cols |= {"EB_SEQ", "EB_KEY"}
    if table.startswith("BENEFIT_ENTITY"):
        cols |= {"ENTITY_SEQ"}
    return cols


def _all_hier(fmt: str = "EDI271") -> set:
    return set(parser_module(fmt).HIER_COLS)


def _expand(tokens: list) -> list:
    out = []
    for t in tokens:
        out += SYNONYMS.get(t, [t])
    return out


def _same_words(a: list, b: list) -> bool:
    return len(a) == len(b) and all(tokens_match(x, y) for x, y in zip(a, b))


def _tier(column: str, field: str) -> int:
    """How well a database column name matches a 271 column name; None = no match."""
    c, f = name_tokens(column), name_tokens(field)
    if not c or not f:
        return None
    if c == f or squash(column) == squash(field):
        return 0
    ce, fe = _expand(c), _expand(f)
    if ce == fe or _same_words(ce, fe):
        return 1
    # Prefix dropped on one side: LAST_NAME ~ MEMBER_LAST_NAME, MBR_DOB ~ DATE_OF_BIRTH
    def strip(tokens):
        for prefix in TABLE_PREFIXES:
            if len(tokens) > len(prefix) and all(tokens_match(x, y) for x, y in zip(tokens, prefix)):
                return tokens[len(prefix):], True
        return tokens, False
    c_rest, c_pre = strip(ce)
    f_rest, f_pre = strip(fe)
    if c_pre != f_pre and _same_words(c_rest, f_rest):
        return 2
    # Same words in another order: BIRTH_DATE ~ DATE_OF_BIRTH
    stop = {"of", "the"}
    cs, fs = [x for x in c_rest if x not in stop], [x for x in f_rest if x not in stop]
    if len(cs) > 1 and sorted(cs) == sorted(fs) and c_pre == f_pre:
        return 2
    # Different prefixes, same rest: SUBSCRIBER_LAST_NAME ~ MEMBER_LAST_NAME
    if c_pre and f_pre and _same_words(c_rest, f_rest):
        return 3
    # Code / indicator suffix on one side only: REJECT_REASON_CD ~ REJECT_REASON
    def drop_suffix(tokens):
        return tokens[:-1] if len(tokens) > 1 and tokens[-1] in SUFFIX_WORDS else tokens
    if _same_words(drop_suffix(c_rest), drop_suffix(f_rest)) and \
            (drop_suffix(c_rest) != c_rest) != (drop_suffix(f_rest) != f_rest):
        return 2
    # An organisation's name is X12's "last name" (NM103): PAYER_NAME ~ PAYER_LAST_NAME
    c_prefix, f_prefix = ce[:len(ce) - len(c_rest)], fe[:len(fe) - len(f_rest)]
    if c_rest == ["name"] and f_rest == ["last", "name"] and f_prefix in (["payer"], ["provider"], ["entity"]) \
            and _same_words(c_prefix, f_prefix):
        return 3
    return None


def map_columns(columns: list, fields: list, file_table: str = None) -> tuple:
    """
    Pairs database columns with file table columns, best match first, each
    used once. Returns ([{"column", "field", "tier"}], [unmatched columns]).
    With file_table (834/837), a one-word field is also tried with the
    table's last word in front: CODE of CLAIM_DIAGNOSIS ~ DIAGNOSIS_CD.
    """
    qualifier = name_tokens(file_table)[-1] if file_table else None
    options = []
    for col in columns:
        for field in fields:
            tier = _tier(col, field)
            if tier is None and qualifier and len(name_tokens(field)) == 1 \
                    and name_tokens(field) != [qualifier]:
                q = _tier(col, f"{qualifier}_{field}")
                tier = None if q is None else min(q + 1, 3)
            if tier is not None:
                options.append((tier, field.endswith("_DESC"), col, field))
    options.sort(key=lambda o: (o[0], o[1]))
    used_c, used_f, pairs = set(), set(), []
    for tier, _, col, field in options:
        if col in used_c or field in used_f:
            continue
        used_c.add(col)
        used_f.add(field)
        pairs.append({"column": col, "field": field, "tier": tier})
    order = {c: i for i, c in enumerate(columns)}
    pairs.sort(key=lambda p: order[p["column"]])
    return pairs, [c for c in columns if c not in used_c]


# ─────────────────────────────────────────────────────────────────────────────
# Table matching
# ─────────────────────────────────────────────────────────────────────────────

def _table_words(table: str) -> list:
    return name_tokens(str(table).split(".")[-1])


def _name_score(db_table: str, file_table: str) -> float:
    words = _table_words(db_table)
    wanted = name_tokens(file_table)
    hits = sum(any(tokens_match(w, x) for x in _expand(words)) for w in wanted)
    score = 2.0 * hits
    if hits == len(wanted):
        score += 2.0 + 0.5 * len(wanted)          # BENEFIT_DATE beats BENEFIT for …_BENEFIT_DATE
    return score


def score_table(db_table: str, columns: list, file_table: str, frame: pd.DataFrame,
                fmt: str = "EDI271") -> tuple:
    """(score, pairs) — how well a database table matches one file table."""
    all_hier, family = _all_hier(fmt), _family_hier(file_table, fmt, frame)
    pairs, _ = map_columns(columns, [str(c) for c in frame.columns],
                           None if str(fmt).upper() == "EDI271" else file_table)
    score, kept = 0.0, []
    for p in pairs:
        field = p["field"]
        if field in HIER_COMMON:
            weight = 0.3
        elif field in all_hier:
            if field not in family:
                score -= 1.0                       # e.g. EB_SEQ on a member table: wrong level
                continue
            weight = 1.0
        else:
            weight = 2.0 if p["tier"] < 2 else 1.5
        score += weight
        kept.append(p)
    score += _name_score(db_table, file_table)
    return score, kept


def match_table(db_table: str, columns: list, tables: dict, forced: str = None,
                fmt: str = "EDI271") -> tuple:
    """(file table name or None, reason)."""
    if forced:
        f = forced.strip().upper()
        if f == "SKIP":
            return None, "skipped (--map)"
        if f not in tables:
            return None, f"--map names {f}, which is not a {label(fmt)} table"
        return f, "--map"
    best = None
    for name, frame in tables.items():
        if name == "UNMAPPED":
            continue
        score, pairs = score_table(db_table, columns, name, frame, fmt)
        data_pairs = [p for p in pairs if p["field"] not in _all_hier(fmt)]
        rank = (score, len(frame) > 0)
        if best is None or rank > best[0]:
            best = (rank, name, len(data_pairs), score)
    if best is None:
        return None, f"no {label(fmt)} table has matching columns"
    named = _name_score(db_table, best[1]) >= 4.0         # every word of the 271 table name
    if best[2] < 2 and not (named and best[2] >= 1) and best[3] < 6:
        return None, f"no {label(fmt)} table has matching columns"
    return best[1], f"{best[2]} matching column(s)"


# ─────────────────────────────────────────────────────────────────────────────
# Keys and test case
# ─────────────────────────────────────────────────────────────────────────────

def _natural_key(file_table: str, fmt: str = "EDI271") -> list:
    """Key of a file table when the file has no rows to search: hierarchy + qualifier + OCC."""
    fmt = str(fmt or "EDI271").upper()
    _OCC_SPEC = parser_module(fmt)._OCC_SPEC
    key = ["TRANSACTION_CONTROL_NUMBER"]
    if fmt == "EDI271":
        family = _family_hier(file_table)
        parents = ("SUBSCRIBER_ID", "DEPENDENT_HL", "EB_SEQ", "ENTITY_SEQ")
    else:
        family, parents = _all_hier(fmt), _PARENT_KEYS.get(fmt, [])
    for col in parents:
        if col in family:
            key.append(col)
    spec = _OCC_SPEC.get(file_table)
    if spec:
        key += [c for c in spec[0] if c not in key]
        key.append("OCC")
    return key


def choose_join(frame: pd.DataFrame, pairs: list, file_table: str, fmt: str = "EDI271") -> list:
    """Join pairs: the smallest unique set of paired columns (or the natural key)."""
    from Libraries.Data_Profiler import find_unique_key
    by_field = {p["field"]: p for p in pairs}
    if str(fmt).upper() != "EDI271" and frame is not None and len(frame):
        key = _business_key(frame, by_field, file_table, fmt)
        if key:
            return [by_field[k] for k in key]
    key = find_unique_key(frame, list(by_field)) if frame is not None and len(frame) else []
    if not key:
        key = [c for c in _natural_key(file_table, fmt) if c in by_field]
    return [by_field[k] for k in key]


def _unique(frame: pd.DataFrame, cols: list) -> bool:
    """Every column has values and together they identify every row."""
    part = frame[cols]
    if part.isna().any().any():
        return False
    return not part.astype(str).duplicated().any()


def _business_key(frame: pd.DataFrame, by_field: dict, file_table: str, fmt: str) -> list:
    """
    834 / 837: the record's business identity (member, claim) plus what
    identifies the row under it (line number, qualifier, insurance line …),
    when those columns are paired with the database table and unique in the
    file. [] when none fits — the caller then searches the data.
    """
    fmt = str(fmt).upper()
    have = lambda c: c in by_field and c in frame.columns
    spec = getattr(parser_module(fmt), "_OCC_SPEC", {}).get(file_table)
    # Always part of the key: the line number on line tables and the parser's
    # own row key (qualifier, code …) — a sample file with one code per claim
    # must not decide that a claim has only one.
    always = (["LINE_NUMBER"] if file_table.startswith("LINE") else []) + (list(spec[0]) if spec else [])
    always = [c for c in dict.fromkeys(always) if have(c)]
    extra = [c for c in _TABLE_KEYS.get(fmt, {}).get(file_table, []) if have(c) and c not in always]
    for identity in _IDENTITY.get(fmt, []):
        if not all(have(c) for c in identity):
            continue
        key = list(dict.fromkeys(identity + always))
        if _unique(frame, key):
            return key
        for col in extra:                       # added one at a time, only as needed
            key.append(col)
            if _unique(frame, key):
                return key
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Exact duplicate rows ("all fields" Duplicate_Check)
# ─────────────────────────────────────────────────────────────────────────────
# A parsed EDI row carries columns that only record where it sits in the file.
# Two copies of the same record differ there, so they are left out when rows
# are compared field by field. A child row keeps its PARENT's sequence: the
# same date under two different benefits is not a duplicate, the same date
# twice under one benefit is.
_POSITION_ALWAYS = {"SEG_POS", "OCC", "LOOP_SEQ"}
_OWN_POSITION = {
    "EDI271": {"PAYER": ["PAYER_HL"], "PROVIDER": ["PROVIDER_HL"],
               "MEMBER": ["SUBSCRIBER_HL", "DEPENDENT_HL"],
               "BENEFIT": ["EB_SEQ"], "BENEFIT_ENTITY": ["ENTITY_SEQ"]},
    "EDI834": {"MEMBER": ["MEMBER_SEQ"], "COVERAGE": ["COVERAGE_SEQ"], "COB": ["COB_SEQ"]},
    "EDI837": {"BILLING_PROVIDER": ["BILLING_PROVIDER_HL"], "SUBSCRIBER": ["SUBSCRIBER_HL"],
               "PATIENT": ["PATIENT_HL"], "CLAIM": ["CLAIM_SEQ"], "LINE": ["LINE_SEQ"],
               "OTHER_SUBSCRIBER": ["OTHER_SUBSCRIBER_SEQ"],
               "CLAIM_DIAGNOSIS": ["HI_SEQ", "POSITION"],
               "OTHER_SUBSCRIBER_ADJUSTMENT": ["CAS_SEQ", "TRIPLET"],
               "LINE_ADJUSTMENT": ["CAS_SEQ", "TRIPLET"]},
}
# Bookkeeping columns any file can carry (File_parser / Data_Validation).
FILE_BOOKKEEPING = {"SourceFile", "_chunk_seq", "_row_seq"}


def row_position_columns(file_config: dict, columns) -> set:
    """
    Columns left out when whole rows are compared: file bookkeeping columns,
    and for EDI271 / EDI834 / EDI837 the position columns of the parsed table
    (SEG_POS, OCC, LOOP_SEQ, …_OCC counters and the table's own sequence).
    """
    columns = [str(c) for c in columns]
    out = {c for c in columns if c in FILE_BOOKKEEPING}
    fmt = str((file_config or {}).get("Format") or "").upper()
    if fmt not in FORMATS:
        return out
    table = str((file_config or {}).get("Table") or
                {"EDI834": "MEMBER", "EDI837": "CLAIM"}.get(fmt, "")).upper()
    own = set(_OWN_POSITION.get(fmt, {}).get(table, []))
    out |= {c for c in columns if c in _POSITION_ALWAYS or c in own or c.endswith("_OCC")}
    return out


def has_child_rows(file_config: dict) -> bool:
    """True for an EDI table whose rows have segments under them (benefit, member, claim …)."""
    fmt = str((file_config or {}).get("Format") or "").upper()
    table = str((file_config or {}).get("Table") or "").upper()
    return table in _OWN_POSITION.get(fmt, {})


def child_signatures(source_def: dict, rows: pd.DataFrame) -> list:
    """
    For EDI rows that have segments under them: one text per row describing
    everything under it (the data columns of every child row, in any order).
    Two copies of an EB, a member or a claim are only duplicates when these
    are equal too — the same EB segment followed by different service types
    or messages is a different benefit.
    """
    file_config = (source_def or {}).get("FileConfig") or {}
    fmt = file_format(source_def)
    table = str(file_config.get("Table") or "").upper()
    own = _OWN_POSITION.get(fmt, {}).get(table, [])
    tables = read_file_tables(source_def)
    hier = set(parser_module(fmt).HIER_COLS) | FILE_BOOKKEEPING
    skip = _POSITION_ALWAYS | {"LOOP_ID"}

    def text(v):
        if v is None or (isinstance(v, float) and v != v):
            return ""
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        return str(v).strip()

    children = [(n, t) for n, t in tables.items()
                if n not in (table, "UNMAPPED") and not t.empty and all(c in t.columns for c in own)]
    out = []
    for _, prow in rows.iterrows():
        keys = [c for c in parser_module(fmt).HIER_COLS
                if c in rows.columns and c not in skip and not c.endswith("_OCC") and text(prow[c]) != ""]
        parts = []
        for name, child in children:
            k = [c for c in keys if c in child.columns]
            if not set(own) <= set(k):
                continue
            mask = pd.Series(True, index=child.index)
            for c in k:
                mask &= child[c].map(text) == text(prow[c])
            sub = child[mask]
            if sub.empty:
                continue
            body = sub[[c for c in sub.columns if c not in hier]].map(text)
            parts.append((name, sorted(map(tuple, body.values.tolist()))))
        out.append(repr(sorted(parts)))
    return out


def _ident(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", name) else '"' + name + '"'


def build_child_testcase(db_table: str, columns: list, file_table: str, frame: pd.DataFrame,
                         source_def: dict, oracle_env: str, fmt: str = None) -> tuple:
    """(test_case, info) — one SRC_TGT: the file table vs the database table."""
    fmt = fmt or file_format(source_def) or "EDI271"
    pairs, unmatched = map_columns(columns, [str(c) for c in frame.columns],
                                   None if str(fmt).upper() == "EDI271" else file_table)
    family, all_hier = _family_hier(file_table, fmt, frame), _all_hier(fmt)
    pairs = [p for p in pairs if p["field"] not in all_hier or p["field"] in family
             or p["field"] in HIER_COMMON]
    joins = choose_join(frame, pairs, file_table, fmt)
    join_fields = {p["field"] for p in joins}
    unique = bool(joins) and (frame is None or frame.empty or
                              not frame[[p["field"] for p in joins]].astype(str).duplicated().any())
    compares = [p for p in pairs if p["field"] not in join_fields]
    source = copy.deepcopy(source_def)
    source.setdefault("FileConfig", {})["Table"] = file_table
    cols = [p["column"] for p in joins + compares] or ["*"]
    owner = db_table.split(".")[0] if "." in db_table else ""
    case = {
        "TestCaseNumber":  None,
        "Module":          "",
        "Stage":           "",
        "ValidationType":  "SRC_TGT",
        "TestDescription": f"{db_table.split('.')[-1]} matches {label(fmt)} {file_table}",
        "Viceversa":       True,
        "Source":          source,
        "Target": {
            "Type": "ORACLE", "Environment": oracle_env, "DB": owner, "QueryType": "QUERY",
            "Query": f"SELECT {', '.join(_ident(c) for c in cols)} FROM {db_table}",
        },
        "JoinFields":    [{"SourceField": p["field"], "TargetField": p["column"]} for p in joins],
        "CompareFields": [{"SourceField": p["field"], "TargetField": p["column"]} for p in compares],
        "DataFilters":   [],
        "ExpectedResult": "Source and target should match: no missing records and no mismatched values",
    }
    info = {"joins": joins, "compares": compares, "not_compared": unmatched,
            "rows": int(len(frame)), "join_unique": unique}
    return case, info


def file_duplicate_case(child: dict) -> dict:
    """Duplicate_Check on the file table's key (file CheckFields = one combined key)."""
    keys = [f["SourceField"] for f in child.get("JoinFields") or []]
    table = child["Source"]["FileConfig"]["Table"]
    fmt = file_format(child["Source"]) or "EDI271"
    return {
        "TestCaseNumber":  None,
        "Module":          child.get("Module", ""),
        "Stage":           child.get("Stage", ""),
        "ValidationType":  "Duplicate_Check",
        "TestDescription": f"{label(fmt)} {table}: no duplicate {', '.join(keys)}",
        "Viceversa":       False,
        "Source":          copy.deepcopy(child["Source"]),
        "CheckFields":     [{"SourceField": k, "ResultFields": [k]} for k in keys],
        "DataFilters":     [],
        "ExpectedResult":  0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Mapping file (Config/EDI271_RECON_Map.json): tables linked by surrogate keys
# ─────────────────────────────────────────────────────────────────────────────
# A database that stores the file in parent/child tables joined by generated
# IDs (…_ID_NH) has no business key on its child rows. The mapping file names
# each table's parent; the query built here joins every table up to the root
# (the transaction header) to carry the transaction control number down, and
# numbers the rows under each parent in ID order — EB_SEQ, ENTITY_SEQ, OCC —
# which is how the parser numbers them in file order.

PER_COLUMNS = {"TE": "PHONE", "EX": "PHONE_EXT", "FX": "FAX", "EM": "EMAIL", "UR": "URL"}


def load_map(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    tables = spec.get("tables") or {}
    if not isinstance(tables, dict) or not tables:
        raise ValueError(f"{path}: no 'tables' in the mapping file")
    spec["tables"] = {k.upper(): v for k, v in tables.items()}
    spec["root"] = str(spec.get("root") or "").upper()
    for name, t in spec["tables"].items():
        parent = t.get("parent")
        if parent and parent.get("table", "").upper() not in spec["tables"]:
            raise ValueError(f"{path}: parent {parent.get('table')} of {name} is not in the file")
    return spec


def _parent_chain_sql(spec: dict, table: str, file_filter: bool) -> tuple:
    """(SELECT …, carried aliases) — the table's ID plus the values carried down from its ancestors."""
    t = spec["tables"][table]
    tid = t["id"]
    cols, carried, joins, where = [f"t.{tid} AS {tid}"], [], "", []
    parent = t.get("parent")
    if parent:
        p_table = parent["table"].upper()
        p_sql, p_carried = _parent_chain_sql(spec, p_table, file_filter)
        joins = f" JOIN ({p_sql}) p ON p.{spec['tables'][p_table]['id']} = t.{parent['fk']}"
        cols += [f"p.{c} AS {c}" for c in p_carried]
        carried += p_carried
    for alias, col in (t.get("carries") or {}).items():
        cols.append(f"t.{col} AS {alias}")
        carried.append(alias)
    if t.get("sequence") and parent:
        cols.append(f"ROW_NUMBER() OVER (PARTITION BY t.{parent['fk']} ORDER BY t.{tid}) AS {t['sequence']}")
        carried.append(t["sequence"])
    if table == spec.get("root") and file_filter and spec.get("file_filter_column"):
        col = spec["file_filter_column"]
        where.append(f"t.{col} = :{col}")
    sql = f"SELECT {', '.join(cols)} FROM {table} t{joins}" + (f" WHERE {' AND '.join(where)}" if where else "")
    return sql, carried


def build_spec_testcase(spec: dict, table: str, test: dict, source_def: dict, oracle_env: str,
                        frame: pd.DataFrame, file_filter_value=None) -> tuple:
    """(SRC_TGT test case, info) for one entry of a table's 'tests' in the mapping file."""
    t = spec["tables"][table]
    tid, parent = t["id"], t.get("parent")
    file_table = test["file_table"].upper()
    linked = bool(parent) or table == spec.get("root")
    use_filter = linked and file_filter_value not in (None, "")

    inner, by_file = [], {}                 # SELECT items; 271 column -> RECON alias
    carried = []
    joins = ""
    if parent:
        p_table = parent["table"].upper()
        p_sql, carried = _parent_chain_sql(spec, p_table, use_filter)
        joins = f" JOIN ({p_sql}) p ON p.{spec['tables'][p_table]['id']} = t.{parent['fk']}"
        inner += [f"p.{c} AS {c}" for c in carried]
    for alias, col in (t.get("carries") or {}).items():
        inner.append(f"t.{col} AS {alias}")
        carried = carried + [alias]
    if t.get("sequence") and parent:
        inner.append(f"ROW_NUMBER() OVER (PARTITION BY t.{parent['fk']} ORDER BY t.{tid}) AS {t['sequence']}")
        carried = carried + [t["sequence"]]
    for alias in carried:
        by_file[alias] = alias               # carried aliases are named after the 271 column
    if "occ" in test:
        parts = ([f"t.{parent['fk']}"] if parent else []) + [f"t.{c}" for c in test["occ"]]
        over = (f"PARTITION BY {', '.join(parts)} " if parts else "") + f"ORDER BY t.{tid}"
        inner.append(f"ROW_NUMBER() OVER ({over}) AS OCC")
        by_file["OCC"] = "OCC"
    for alias, value in (test.get("constants") or {}).items():
        inner.append(f"{int(value) if isinstance(value, (int, float)) else repr(str(value))} AS {alias}")
        by_file[alias] = alias
    for db_col, file_col in test.get("columns") or []:
        inner.append(f"t.{db_col} AS {db_col}")
        by_file.setdefault(file_col, db_col)
    for alias, sql, file_col in test.get("expressions") or []:
        inner.append(f"{sql} AS {alias}")
        by_file.setdefault(file_col, alias)
    pairs = test.get("per") or []
    for qual, file_col in PER_COLUMNS.items():
        if not pairs:
            break
        whens = " ".join(f"WHEN t.{q} = '{qual}' THEN t.{n}" for q, n in pairs)
        alias = f"PER_{file_col}"
        inner.append(f"CASE {whens} END AS {alias}")
        by_file.setdefault(file_col, alias)

    # Rows are numbered (EB_SEQ, ENTITY_SEQ, OCC) over ALL rows of the parent;
    # the test's own 'where' is applied after that, in the outer query —
    # filtering first would renumber the rows that remain.
    where = []
    if table == spec.get("root") and use_filter:
        where.append(f"t.{spec['file_filter_column']} = :{spec['file_filter_column']}")
    keep = ""
    if test.get("where"):
        inner.append(f"CASE WHEN ({test['where']}) THEN 1 ELSE 0 END AS KEEP_ROW")
        keep = " WHERE q.KEEP_ROW = 1"
    inner_sql = (f"SELECT {', '.join(inner)} FROM {table} t{joins}"
                 + (f" WHERE {' AND '.join(where)}" if where else ""))

    frame_cols = set(str(c) for c in frame.columns) if frame is not None else set()
    missing = [k for k in test["key"] if k not in by_file]
    if missing:
        raise ValueError(f"{table}: key column(s) {missing} have no RECON column in the mapping")
    unknown = [f for f in by_file if frame_cols and f not in frame_cols]
    joins_ = [{"SourceField": k, "TargetField": by_file[k]} for k in test["key"]]
    key_set = set(test["key"])
    compares = [{"SourceField": f, "TargetField": a} for f, a in by_file.items()
                if f not in key_set and f not in carried and f != "OCC" and f in frame_cols]
    outer = [j["TargetField"] for j in joins_] + [c["TargetField"] for c in compares]
    query = f"SELECT {', '.join(dict.fromkeys(outer))} FROM ({inner_sql}) q{keep}"

    source = copy.deepcopy(source_def)
    source.setdefault("FileConfig", {})["Table"] = file_table
    filters = copy.deepcopy(test.get("source_filters") or [])
    if use_filter:
        filters.append({"TargetField": spec["file_filter_column"], "DefaultValue": str(file_filter_value),
                        "OperatorType": "="})
    short = table.split(".")[-1]
    case = {
        "TestCaseNumber":  None,
        "Module":          "",
        "Stage":           "",
        "ValidationType":  "SRC_TGT",
        "TestDescription": f"{short} matches {label(file_format(source_def) or 'EDI271')} {file_table}"
                           + ("" if linked else " (presence only)"),
        "Viceversa":       bool(test.get("viceversa", True)),
        "Source":          source,
        "Target": {"Type": "ORACLE", "Environment": oracle_env, "DB": table.split(".")[0],
                   "QueryType": "QUERY", "Query": query},
        "JoinFields":    joins_,
        "CompareFields": compares,
        "DataFilters":   filters,
        "ExpectedResult": "Source and target should match: no missing records and no mismatched values",
    }
    info = {"rows": int(len(frame)) if frame is not None else 0, "linked": linked,
            "filtered": use_filter, "unknown": unknown,
            "note": test.get("note") or t.get("note") or "",
            "joins": ", ".join(f"{j['SourceField']}={j['TargetField']}" for j in joins_),
            "compared": len(compares)}
    return case, info


def spec_target_duplicate(case: dict) -> dict:
    """Duplicate_Check on the RECON side of a linked case: its key, read through the same query."""
    keys = [j["TargetField"] for j in case["JoinFields"]]
    target = copy.deepcopy(case["Target"])
    filters = [copy.deepcopy(f) for f in case.get("DataFilters") or [] if f.get("TargetField")]
    for f in filters:
        f["SourceField"] = f.pop("TargetField")
    table = case["TestDescription"].split(" matches ")[0]
    return {
        "TestCaseNumber":  None, "Module": "", "Stage": "",
        "ValidationType":  "Duplicate_Check",
        "TestDescription": f"Target {table}: no duplicate {', '.join(keys)}",
        "Viceversa":       False,
        "Source":          target,
        "CheckFields":     [{"SourceField": list(keys), "ResultFields": list(keys)}] if len(keys) > 1
                           else [{"SourceField": keys[0], "ResultFields": [keys[0]]}],
        "DataFilters":     filters,
        "ExpectedResult":  0,
    }


def spec_columns_missing(spec: dict, table: str, db_columns: list) -> list:
    """RECON columns named in the mapping file that the table does not have."""
    have = {c.upper() for c in db_columns}
    t = spec["tables"][table]
    named = [t["id"]] + ([t["parent"]["fk"]] if t.get("parent") else [])
    named += list((t.get("carries") or {}).values())
    for test in t.get("tests") or []:
        named += [c for c, _ in test.get("columns") or []]
        named += list(test.get("occ") or [])
        named += [x for pair in test.get("per") or [] for x in pair]
    return [c for c in dict.fromkeys(named) if c.upper() not in have]