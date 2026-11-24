"""
TestCase_Loader.py
==================
Loads generated test cases into the DataRegressionSet collection — one
document per test case — without creating duplicates on re-runs.

Test_Case_Generator.py calls this automatically after every generation, and
Libraries/Load_Test_Cases.py calls it to load a reviewed or corrected file.

For every incoming test case the loader looks for an existing document of the
same module that tests the same thing:

    same Module + same ValidationType + same Source object + same Target object

(the Source/Target "object" is the environment + database/collection, table,
or file location — not the query details, so a changed pipeline or filter
still counts as the same test case). When a module has two test cases of the
same type on the same source/target, they are paired up in TestCaseNumber
order.

    no match                          → INSERT as a new document, next free
                                        TestCaseNumber for the module
    match, nothing different          → UNCHANGED, nothing written
    match, something different        → UPDATE the existing document in place
                                        (keeps its _id and TestCaseNumber)

Test cases that would not run correctly in Data_Validation are never written:

    INCOMPLETE   a field the ValidationType needs is missing or empty
                 (ApprovedValues, CheckFields, JoinFields …)
    INVALID      a value is wrong (unknown ValidationType, pipeline that is
                 not valid JSON, bad regex, Source without a Type …)
    ERROR        MongoDB rejected the write for that one test case

Each result says exactly what is wrong. annotate_testcases() writes the
result onto each test case as "_LoadStatus" so it can be saved into the JSON
file; fields starting with "_" are never loaded into DataRegressionSet, so a
corrected file can be loaded again as it is.

RegressionSet_ID
  Every inserted document gets a RegressionSet_ID: unique across the whole
  collection and incremental (highest existing RegressionSet_ID + 1, also
  respecting a "RegressionSet_ID" sequence in ValidationSequenceNums if one
  exists). Updates keep the existing RegressionSet_ID.

Query types
  MongoDB sides must use QueryType PIPELINE (Collection + aggregation in
  Query), VIEW, JAVA_SCRIPT or AGGREGATE; Oracle sides QUERY, TABLE or VIEW.
  Every :NAME in an Oracle query and every $$Name in a pipeline needs a
  DataFilter for that side with a DefaultValue.

Safety rules
  - Empty values in the new test case never overwrite values already in the
    database, so fixes made by hand after an earlier load are kept. For the
    same reason, a test case that matches an existing document is checked for
    completeness after merging with that document.
  - Existing test cases written by hand (not marked as generated) are never
    modified unless overwrite_manual=True; the run reports them instead.
  - preview=True reports every decision and writes nothing.
"""

import copy
import getpass
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

GENERATED_BY = "Test_Case_Generator"

# Fields that identify or describe a document rather than what it tests.
# TestDescription is free text the model words differently on every run, and
# people often refine it by hand: a new wording alone is not a change.
_NOT_COMPARED = {"_id", "RegressionSet_ID", "TestCaseNumber", "Module", "GeneratedBy", "Audit",
                 "TestDescription"}

# Data_Validation matches ValidationType with ==, so the spelling must be exact.
VALIDATION_TYPES = [
    "SRC_TGT", "Record_Count", "Null_Check", "Duplicate_Check",
    "Format_Check", "InApprovedValue",
    "EDI835_Format", "EDI835_Balance", "EDI835_Data",
]
_DB_TYPES = {"MONGODB", "ORACLE"}
# QueryTypes Data_Validation can read for each database.
_QUERY_TYPES = {
    "MONGODB": ("PIPELINE", "VIEW", "JAVA_SCRIPT", "AGGREGATE"),
    "ORACLE":  ("QUERY", "TABLE", "VIEW"),
}
_MONGO_SYSTEM_VARS = {"ROOT", "CURRENT", "REMOVE", "NOW", "CLUSTER_TIME", "DESCEND",
                      "PRUNE", "KEEP", "SEARCH_META", "USER_ROLES", "value", "this"}
_FORMAT_TYPES = {"REGEX", "DATE_FORMAT", "NUMERIC_RANGE", "LENGTH"}

# Result codes
INSERT, UPDATE, UNCHANGED = "INSERT", "UPDATE", "UNCHANGED"
SKIP_MANUAL, INCOMPLETE, INVALID, ERROR = "SKIP_MANUAL", "INCOMPLETE", "INVALID", "ERROR"
NOT_LOADED = {INCOMPLETE, INVALID, ERROR}


# ─────────────────────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────────────────────

def _canon(value) -> str:
    """Stable text form for comparing values (key order and dates don't matter)."""
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


_FROM_RE = re.compile(r'\bFROM\s+("?[A-Za-z0-9_$#]+"?(?:\s*\.\s*"?[A-Za-z0-9_$#]+"?)?)',
                      re.IGNORECASE)


def side_object(side: dict) -> str:
    """
    The table / collection a Source or Target reads, however it is queried:
    MongoDB PIPELINE → Collection; Oracle SELECT → first table after FROM;
    otherwise the Query (a collection, view or table name).
    """
    stype = (side.get("Type") or "").upper()
    qt = (side.get("QueryType") or "").upper()
    query = side.get("Query")
    if qt == "PIPELINE":
        obj = side.get("Collection") or ""
    elif isinstance(query, str):
        obj = query.strip()
    else:
        obj = side.get("Collection") or _canon(query)
    if stype == "ORACLE":
        m = _FROM_RE.search(obj) if "select" in obj.lower() else None
        if m:
            obj = re.sub(r'[\s"]', "", m.group(1))
        if obj and "." not in obj and " " not in obj and side.get("DB"):
            obj = f"{side['DB']}.{obj}"
        obj = obj.upper()
    return obj


def _side_identity(side):
    """What a Source/Target points at — not how it is queried."""
    if side in (None, "", {}, []):
        return None
    if isinstance(side, list):                       # multi-step source (EDI835 etc.)
        return _canon(side)
    if not isinstance(side, dict):
        return _canon(side)
    return _canon({
        "Type":        (side.get("Type") or "").upper(),
        "Environment": (side.get("Environment") or "").upper(),
        "DB":          (side.get("DB") or "").upper() if (side.get("Type") or "").upper() == "ORACLE"
                       else side.get("DB") or "",
        "Object":      side_object(side),
        "Path":        side.get("Path") or "",
        "File":        side.get("FileName") or side.get("FilePattern") or "",
    })


def _base_key(tc: dict) -> tuple:
    return (
        (tc.get("Module") or "").strip().upper(),
        (tc.get("ValidationType") or "").strip().upper(),
        _side_identity(tc.get("Source")),
        _side_identity(tc.get("Target")),
    )


def _with_occurrence(items: list, get=lambda x: x) -> list:
    """[(key + (n,), item)] — n-th test case of the same base key, by TestCaseNumber."""
    seen = {}
    out = []
    for item in sorted(items, key=lambda x: _num(get(x).get("TestCaseNumber"))):
        base = _base_key(get(item))
        seen[base] = seen.get(base, 0) + 1
        out.append((base + (seen[base],), item))
    return out


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 10 ** 9


def describe(tc) -> str:
    """Short label: 'SRC_TGT  MCP.Contract -> DW.CONTRACT'."""
    if not isinstance(tc, dict):
        return "(not a test case object)"

    def side(s):
        if not isinstance(s, dict):
            return "steps" if s else "-"
        obj = s.get("FilePattern") or s.get("FileName") or side_object(s) or "?"
        if len(obj) > 40:
            obj = obj[:37] + "..."
        db = s.get("DB")
        return f"{db}.{obj}" if db and "." not in obj else obj
    tgt = tc.get("Target")
    return f"{tc.get('ValidationType', '?'):<16} {side(tc.get('Source'))}" + (f" -> {side(tgt)}" if tgt else "")


# ─────────────────────────────────────────────────────────────────────────────
# Validation — would this test case run correctly in Data_Validation?
# ─────────────────────────────────────────────────────────────────────────────

def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == [] or v == {}


def _field_name(f) -> str:
    return (f or {}).get("SourceField") or (f or {}).get("Field") or "" if isinstance(f, dict) else ""


def _parse_pipeline(query):
    """Same parser Data_Validation uses; plain JSON as a fallback."""
    try:
        from .data_fetcher import parse_pipeline
    except Exception:                               # driver not installed etc.
        parse_pipeline = None
    if parse_pipeline:
        return parse_pipeline(query)
    stages = json.loads(query) if isinstance(query, str) else query
    if isinstance(stages, dict):
        stages = stages.get("pipeline") or next(iter(stages.values()))
    if not isinstance(stages, list):
        raise ValueError("not a list of stages")
    return stages


def _filter_defaults(tc: dict, label: str) -> dict:
    """{field: DefaultValue} of the DataFilters that apply to this side."""
    key = "TargetField" if label == "Target" else "SourceField"
    out = {}
    for f in tc.get("DataFilters") or []:
        if isinstance(f, dict) and f.get(key):
            out[str(f[key])] = f.get("DefaultValue")
    return out


def _check_placeholders(names, tc, label, where, missing):
    filters = _filter_defaults(tc, label)
    key = "TargetField" if label == "Target" else "SourceField"
    for name in names:
        if name not in filters:
            missing.append(f"DataFilter with {key} '{name}' (used as {where} in {label}.Query)")
        elif _empty(filters[name]):
            missing.append(f"DefaultValue of DataFilter {key} '{name}' (used as {where} in {label}.Query)")


_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_SQL_PARAM_RE = re.compile(r"(?<![A-Za-z0-9_:]):([A-Za-z_][A-Za-z0-9_$#]*)")


def _oracle_placeholders(sql: str) -> list:
    body = _SQL_STRING_RE.sub("''", sql)
    return list(dict.fromkeys(_SQL_PARAM_RE.findall(body)))


def _pipeline_placeholders(stages) -> list:
    """$$Name strings that are neither MongoDB nor pipeline variables."""
    defined = set(_MONGO_SYSTEM_VARS)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("as"), str):
                defined.add(node["as"])
            for k in ("let", "vars"):
                if isinstance(node.get(k), dict):
                    defined.update(node[k].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for m in re.finditer(r"\$\$([A-Za-z_][A-Za-z0-9_.]*)", node):
                found.append(m.group(1))
    walk(stages)
    return list(dict.fromkeys(n for n in found if n.split(".")[0] not in defined))


def _check_side(side, label, missing, invalid, tc=None):
    if _empty(side):
        missing.append(label)
        return
    if isinstance(side, list):                      # multi-step (EDI835 download steps)
        return
    if not isinstance(side, dict):
        invalid.append(f"{label} must be an object")
        return
    stype = (side.get("Type") or "").upper()
    if not stype:
        missing.append(f"{label}.Type")
        return
    if stype not in _DB_TYPES:
        return                                      # file sources: Path/FileName checked at run time
    tc = tc or {}
    if _empty(side.get("Environment")):
        missing.append(f"{label}.Environment")
    qt = (side.get("QueryType") or "").upper()
    if not qt:
        missing.append(f"{label}.QueryType")
    elif qt not in _QUERY_TYPES[stype]:
        hint = ("use PIPELINE (Collection + aggregation in Query) or VIEW"
                if stype == "MONGODB" else "use QUERY (a SELECT statement)")
        invalid.append(f"{label}.QueryType '{side.get('QueryType')}' is not supported "
                       f"for {stype} — {hint}")
    if stype == "ORACLE" and qt == "QUERY" and isinstance(side.get("Query"), str):
        _check_placeholders(_oracle_placeholders(side["Query"]), tc, label, ":name", missing)
    if qt == "PIPELINE":
        if stype != "MONGODB":
            invalid.append(f"{label}.QueryType PIPELINE is only for MONGODB")
        if _empty(side.get("DB")):
            missing.append(f"{label}.DB")
        if _empty(side.get("Collection")):
            missing.append(f"{label}.Collection (the collection the pipeline runs on)")
        if _empty(side.get("Query")):
            missing.append(f"{label}.Query (the pipeline)")
        else:
            try:
                stages = _parse_pipeline(side["Query"])
                if not stages:
                    invalid.append(f"{label}.Query pipeline has no stages")
                else:
                    _check_placeholders(_pipeline_placeholders(stages), tc, label, "$$name", missing)
            except Exception as exc:
                invalid.append(f"{label}.Query is not a valid aggregation pipeline ({exc})")
    elif _empty(side.get("Query")):
        missing.append(f"{label}.Query")


def _check_pairs(tc, name, missing, invalid, need_target=True):
    items = tc.get(name)
    if _empty(items):
        missing.append(name)
        return
    if not isinstance(items, list):
        invalid.append(f"{name} must be a list")
        return
    for i, f in enumerate(items, start=1):
        if not isinstance(f, dict):
            invalid.append(f"{name}[{i}] must be an object")
            continue
        if _empty(f.get("SourceField")):
            missing.append(f"{name}[{i}].SourceField")
        if need_target and _empty(f.get("TargetField")):
            missing.append(f"{name}[{i}].TargetField")


def _check_fields(tc, missing, invalid, format_check=False):
    items = tc.get("CheckFields")
    if _empty(items):
        missing.append("CheckFields")
        return
    if not isinstance(items, list):
        invalid.append("CheckFields must be a list")
        return
    for i, f in enumerate(items, start=1):
        if not isinstance(f, dict):
            invalid.append(f"CheckFields[{i}] must be an object")
            continue
        name = _field_name(f)
        where = f"CheckFields[{i}]" + (f" ({name})" if name else "")
        if not name:
            missing.append(f"CheckFields[{i}].SourceField")
        if not format_check:
            continue
        ftype = (f.get("FormatType") or "").upper()
        pattern = f.get("Pattern") or f.get("Regex")
        if not ftype and not pattern:
            missing.append(f"{where}.FormatType")
            continue
        if ftype and ftype not in _FORMAT_TYPES:
            invalid.append(f"{where}.FormatType '{f.get('FormatType')}' — use one of "
                           + ", ".join(sorted(_FORMAT_TYPES)))
        elif ftype in ("", "REGEX"):
            if _empty(pattern):
                missing.append(f"{where}.Pattern (regular expression)")
            else:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    invalid.append(f"{where}.Pattern is not a valid regular expression ({exc})")
        elif _empty(f.get("Value")):
            hint = {"DATE_FORMAT": "e.g. YYYY-MM-DD", "NUMERIC_RANGE": "e.g. 0-100",
                    "LENGTH": "e.g. 10"}[ftype]
            missing.append(f"{where}.Value ({hint})")
        elif ftype == "NUMERIC_RANGE":
            try:
                low, high = (float(p.strip()) for p in str(f["Value"]).split("-"))
            except Exception:
                invalid.append(f"{where}.Value '{f['Value']}' must look like LOW-HIGH, e.g. 0-100")
        elif ftype == "LENGTH":
            try:
                int(f["Value"])
            except (TypeError, ValueError):
                invalid.append(f"{where}.Value '{f['Value']}' must be a whole number")


def validate_testcase(tc) -> tuple:
    """
    Returns (missing, invalid): lists of plain-language problems. Both empty
    means the test case can be loaded.
    """
    missing, invalid = [], []
    if not isinstance(tc, dict):
        return [], ["not a JSON object"]

    for f in ("Module", "ValidationType"):
        if _empty(tc.get(f)):
            missing.append(f)
    vt = tc.get("ValidationType") or ""
    if vt and vt not in VALIDATION_TYPES:
        close = [t for t in VALIDATION_TYPES if t.upper() == str(vt).upper()]
        invalid.append(f"ValidationType '{vt}' is not recognised"
                       + (f" — spell it exactly '{close[0]}'" if close else
                          " — use one of " + ", ".join(VALIDATION_TYPES)))

    _check_side(tc.get("Source"), "Source", missing, invalid, tc)

    v = str(vt).upper()
    if v == "SRC_TGT":
        _check_side(tc.get("Target"), "Target", missing, invalid, tc)
        _check_pairs(tc, "JoinFields", missing, invalid)
        if not _empty(tc.get("CompareFields")):
            _check_pairs(tc, "CompareFields", missing, invalid)
    elif v == "RECORD_COUNT":
        # Data_Validation always reads ApprovedValues[0] as a whole number:
        # the expected source count (or, with a Target, the allowed count
        # when the target is empty).
        approved = tc.get("ApprovedValues")
        if _empty(approved):
            missing.append("ApprovedValues (expected record count)")
        elif not isinstance(approved, list):
            invalid.append("ApprovedValues must be a list, e.g. [2412]")
        else:
            try:
                int(str(approved[0]).strip())
            except (TypeError, ValueError):
                invalid.append(f"ApprovedValues[0] '{approved[0]}' must be a whole "
                               f"number (the expected record count)")
        if not _empty(tc.get("Target")):
            _check_side(tc.get("Target"), "Target", missing, invalid, tc)
    elif v == "INAPPROVEDVALUE":
        if _empty(tc.get("ApprovedValues")):
            missing.append("ApprovedValues")
        elif not isinstance(tc.get("ApprovedValues"), list):
            invalid.append("ApprovedValues must be a list")
        _check_pairs(tc, "CompareFields", missing, invalid, need_target=False)
    elif v in ("NULL_CHECK", "DUPLICATE_CHECK"):
        _check_fields(tc, missing, invalid)
    elif v == "FORMAT_CHECK":
        _check_fields(tc, missing, invalid, format_check=True)
    return missing, invalid


def missing_fields(tc: dict) -> list:
    """Backward-compatible: every problem as one list."""
    missing, invalid = validate_testcase(tc)
    return missing + invalid


# ─────────────────────────────────────────────────────────────────────────────
# Change detection
# ─────────────────────────────────────────────────────────────────────────────

def changed_fields(new: dict, old: dict) -> dict:
    """
    Fields whose new value differs from the stored one. Empty new values are
    ignored so a later hand-fix (e.g. ApprovedValues) is never blanked out.
    Fields that exist only in the stored document are left alone.
    """
    out = {}
    for field, value in new.items():
        if field in _NOT_COMPARED or field.startswith("_") or _empty(value):
            continue
        if _canon(value) != _canon(old.get(field)):
            out[field] = value
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Load
# ─────────────────────────────────────────────────────────────────────────────

def open_regression_collection(environment: str = None):
    """DataRegressionSet from [REGRESSION_DATA] in Config/environment.ini."""
    from .Read_config import config_read
    from .MongoDB import Collection
    cr = config_read()
    return Collection(cr.db_name("REGRESSION_DATA"),
                      cr.regression_collection_name("REGRESSION_DATA"),
                      environment or cr.test_data_environment("REGRESSION_DATA"))


def open_sequence_collection(environment: str = None):
    """ValidationSequenceNums in the same database as DataRegressionSet."""
    from .Read_config import config_read
    from .MongoDB import Collection
    cr = config_read()
    return Collection(cr.db_name("REGRESSION_DATA"), "ValidationSequenceNums",
                      environment or cr.test_data_environment("REGRESSION_DATA"))


REGRESSION_SET_SEQUENCE = "RegressionSet_ID"


class RegressionSetIds:
    """
    Hands out RegressionSet_ID values: unique across the whole collection and
    incremental. The next ID is one more than the highest RegressionSet_ID
    already in DataRegressionSet. If ValidationSequenceNums holds a
    "RegressionSet_ID" sequence, it is taken into account and kept up to date
    so IDs assigned elsewhere never collide with these.
    """

    def __init__(self, collection, sequence_collection=None, preview=False):
        self.collection = collection
        self.sequence = sequence_collection
        self.preview = preview
        self._next = None
        self._has_sequence = False

    def _highest_in_collection(self) -> int:
        cursor = self.collection.getDataSort({"RegressionSet_ID": {"$exists": True}},
                                             [("RegressionSet_ID", -1)])
        try:
            cursor = cursor.limit(1)
        except AttributeError:
            pass
        for doc in cursor:
            value = _num(doc.get("RegressionSet_ID"))
            return value if value < 10 ** 9 else 0
        return 0

    def _sequence_value(self) -> int:
        if self.sequence is None:
            return 0
        try:
            record = self.sequence.find_one({"SequenceCode": REGRESSION_SET_SEQUENCE})
        except Exception as exc:
            logger.warning(f"Could not read ValidationSequenceNums: {exc}")
            return 0
        if not record:
            return 0
        self._has_sequence = True
        value = _num(record.get("SequenceNbr"))
        return value if value < 10 ** 9 else 0

    def take(self) -> int:
        if self._next is None:
            self._next = max(self._highest_in_collection(), self._sequence_value()) + 1
        value = self._next
        self._next += 1
        if self._has_sequence and not self.preview:
            self.sequence.UpdateData({"SequenceCode": REGRESSION_SET_SEQUENCE},
                                     {"$set": {"SequenceNbr": value}})
        return value


def _result(index, action, tc, number=None, detail="", problems=None, rs_id=None):
    return {"index": index, "action": action,
            "file_number": tc.get("TestCaseNumber") if isinstance(tc, dict) else None,
            "test_case_number": number, "regression_set_id": rs_id, "label": describe(tc),
            "detail": detail, "problems": problems or []}


def upsert_testcases(testcases: list, collection=None, environment: str = None,
                     preview: bool = False, include_incomplete: bool = False,
                     overwrite_manual: bool = False, user: str = None,
                     sequence_collection=None) -> list:
    """
    Applies the rules above and returns one result per incoming test case, in
    the same order as `testcases`:
        {"index", "action", "file_number", "test_case_number", "label",
         "detail", "problems"}
    action is INSERT | UPDATE | UNCHANGED | SKIP_MANUAL | INCOMPLETE | INVALID | ERROR.
    `test_case_number` is the TestCaseNumber in DataRegressionSet.
    `regression_set_id` is the document's RegressionSet_ID.
    `collection` is a Libraries.MongoDB.Collection; when None it is opened from
    [REGRESSION_DATA] in Config/environment.ini (and ValidationSequenceNums
    alongside it, for the RegressionSet_ID sequence).
    Raises only when DataRegressionSet itself cannot be reached.
    """
    if collection is None:
        collection = open_regression_collection(environment)
        if sequence_collection is None:
            try:
                sequence_collection = open_sequence_collection(environment)
            except Exception as exc:
                logger.warning(f"ValidationSequenceNums not available: {exc}")
    ids = RegressionSetIds(collection, sequence_collection, preview)

    user = user or getpass.getuser()
    now = datetime.utcnow()
    results = [None] * len(testcases)

    by_module = {}
    for index, tc in enumerate(testcases):
        if not isinstance(tc, dict):
            results[index] = _result(index, INVALID, tc, detail="not loaded",
                                     problems=["not a JSON object"])
            continue
        clean = {k: v for k, v in copy.deepcopy(tc).items() if not k.startswith("_")}
        by_module.setdefault((clean.get("Module") or "").strip(), []).append((index, clean))

    for module, items in by_module.items():
        existing = list(collection.getDataSort({"Module": module}, [("TestCaseNumber", 1)])) if module else []
        existing_by_key = dict(_with_occurrence(existing))
        next_number = max([_num(d.get("TestCaseNumber")) for d in existing
                           if _num(d.get("TestCaseNumber")) < 10 ** 9] or [0]) + 1

        for key, (index, tc) in _with_occurrence(items, get=lambda it: it[1]):
            old = existing_by_key.get(key)

            # Check what would actually be stored: for an existing document,
            # the stored values fill any gaps the new test case leaves.
            effective = tc
            if old is not None:
                effective = {k: v for k, v in old.items() if k != "_id"}
                effective.update({k: v for k, v in tc.items() if not _empty(v)})
            missing, invalid = validate_testcase(effective)

            if invalid or (missing and not include_incomplete):
                action = INVALID if invalid else INCOMPLETE
                where = (f"not loaded; DataRegressionSet TC {old.get('TestCaseNumber')} left as it is"
                         if old is not None else "not loaded")
                results[index] = _result(index, action, tc,
                                         old.get("TestCaseNumber") if old else None,
                                         where, invalid + [f"missing {m}" for m in missing],
                                         old.get("RegressionSet_ID") if old else None)
                continue

            if old is None:
                try:
                    rs_id = ids.take()
                    doc = {"RegressionSet_ID": rs_id}
                    doc.update({k: v for k, v in tc.items() if k != "RegressionSet_ID"})
                    doc["Module"] = module
                    doc["TestCaseNumber"] = next_number
                    doc["GeneratedBy"] = GENERATED_BY
                    # Same shape as the existing test cases' Audit.
                    doc["Audit"] = {"InsertedByID": 0, "InsertedName": user, "InsertedDate": now,
                                    "ModifiedByID": 0, "ModifiedName": user, "ModifiedDate": now}
                    if not preview:
                        collection.InsertData(doc)
                except Exception as exc:
                    results[index] = _result(index, ERROR, tc, detail="insert failed",
                                             problems=[str(exc)])
                    continue
                results[index] = _result(index, INSERT, tc, next_number, "new test case",
                                         rs_id=rs_id)
                next_number += 1
                continue

            number = old.get("TestCaseNumber")
            rs_id = old.get("RegressionSet_ID")
            diff = changed_fields(tc, old)
            generated = old.get("GeneratedBy") == GENERATED_BY
            # A generated document loaded before RegressionSet_ID was assigned
            # gets one now.
            needs_id = rs_id in (None, "") and generated
            if not diff and not needs_id:
                results[index] = _result(index, UNCHANGED, tc, number, "same as stored",
                                         rs_id=rs_id)
                continue
            if diff and not generated and not overwrite_manual:
                results[index] = _result(index, SKIP_MANUAL, tc, number,
                                         "hand-written test case differs in "
                                         + ", ".join(sorted(diff)) + " — not modified",
                                         rs_id=rs_id)
                continue

            update = dict(diff)
            try:
                if needs_id:
                    rs_id = ids.take()
                    update["RegressionSet_ID"] = rs_id
                update["GeneratedBy"] = GENERATED_BY
                update["Audit.ModifiedByID"] = 0
                update["Audit.ModifiedName"] = user
                update["Audit.ModifiedDate"] = now
                if not preview:
                    collection.UpdateData({"_id": old["_id"]}, {"$set": update})
            except Exception as exc:
                results[index] = _result(index, ERROR, tc, number, "update failed",
                                         problems=[str(exc)], rs_id=old.get("RegressionSet_ID"))
                continue
            changed = sorted(diff) + (["RegressionSet_ID added"] if needs_id else [])
            results[index] = _result(index, UPDATE, tc, number,
                                     "changed: " + ", ".join(changed), rs_id=rs_id)
    return results


def failed_results(testcases: list, reason: str) -> list:
    """Results for every test case when DataRegressionSet could not be reached."""
    return [_result(i, ERROR, tc, detail="not loaded", problems=[reason])
            for i, tc in enumerate(testcases)]


# ─────────────────────────────────────────────────────────────────────────────
# Reporting — JSON file annotation and run log
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_TEXT = {
    INSERT:      "INSERTED",
    UPDATE:      "UPDATED",
    UNCHANGED:   "UNCHANGED",
    SKIP_MANUAL: "NOT UPDATED - HAND-WRITTEN",
    INCOMPLETE:  "NOT LOADED - INCOMPLETE",
    INVALID:     "NOT LOADED - INVALID",
    ERROR:       "NOT LOADED - ERROR",
}
_PREVIEW_TEXT = {INSERT: "WOULD INSERT", UPDATE: "WOULD UPDATE"}


def annotate_testcases(testcases: list, results: list, preview: bool = False) -> None:
    """
    Writes each result onto its test case as "_LoadStatus" (in place), so the
    saved JSON file shows what happened to every test case and why.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in results:
        tc = testcases[r["index"]]
        if not isinstance(tc, dict):
            continue
        status = {"Status": (_PREVIEW_TEXT.get(r["action"]) if preview else None)
                            or _STATUS_TEXT[r["action"]]}
        if r.get("regression_set_id") not in (None, ""):
            status["RegressionSet_ID"] = r["regression_set_id"]
        if r["test_case_number"] is not None:
            status["DataRegressionSetTestCaseNumber"] = r["test_case_number"]
        if r["detail"]:
            status["Detail"] = r["detail"]
        if r["problems"]:
            status["Errors"] = r["problems"]
        status["LoadedOn"] = stamp
        tc.pop("_LoadStatus", None)
        tc["_LoadStatus"] = status


def summary_counts(results: list) -> dict:
    counts = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return counts


def print_report(results: list, preview: bool = False, json_path: str = None) -> None:
    title = "DataRegressionSet — preview, nothing written" if preview else "DataRegressionSet load"
    print(f"\n{title}   (-> TC = TestCaseNumber, ID = RegressionSet_ID)")
    words = {INSERT: "Inserted", UPDATE: "Updated", UNCHANGED: "Unchanged",
             SKIP_MANUAL: "Kept (hand-written)", INCOMPLETE: "NOT LOADED (incomplete)",
             INVALID: "NOT LOADED (invalid)", ERROR: "NOT LOADED (error)"}
    if preview:
        words.update({INSERT: "Would insert", UPDATE: "Would update"})
    for r in results:
        file_tc = f"TC {r['file_number']}" if r["file_number"] is not None else "TC -"
        db_tc = ""
        if r["test_case_number"] is not None and r["action"] not in NOT_LOADED:
            db_tc = f"-> TC {r['test_case_number']}"
            if r.get("regression_set_id") not in (None, ""):
                db_tc += f" / ID {r['regression_set_id']}"
        line = f"  {file_tc:<6} {words[r['action']]:<24} {db_tc:<20} {r['label']}"
        if r["action"] in (UPDATE, SKIP_MANUAL):
            line += f"  — {r['detail']}"
        print(line)
        for p in r["problems"]:
            print(f"           ! {p}")
            logger.debug(f"TC {r['file_number']}: {p}")

    counts = summary_counts(results)
    order = [INSERT, UPDATE, UNCHANGED, SKIP_MANUAL, INCOMPLETE, INVALID, ERROR]
    print("  " + ", ".join(f"{words[a]}: {counts[a]}" for a in order if a in counts))
    not_loaded = sum(counts.get(a, 0) for a in NOT_LOADED)
    if not_loaded:
        where = f" in {json_path}" if json_path else " in the JSON file"
        print(f"  {not_loaded} test case(s) were not loaded. Each one is marked under "
              f"\"_LoadStatus\"{where}. Correct them in the file and load it again with:")
        print(f"      python ..\\Libraries\\Load_Test_Cases.py \"{json_path or '<file.json>'}\"")
    if counts.get(SKIP_MANUAL):
        print("  Hand-written test cases are never changed automatically; "
              "use --overwrite-manual with Load_Test_Cases.py to replace them.")