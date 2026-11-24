import json
import re
import pdb

from Libraries.MongoDB import Collection
from Libraries.OracleDB import OracleConnection
from .Read_config import config_read, Timezone

cr = config_read()
missing = cr.missing_count("No_Of_Records_Display")
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()

# ─────────────────────────────────────────────────────────────────────────────
# MongoDB query execution — collections, views and aggregation pipelines
# ─────────────────────────────────────────────────────────────────────────────
# QueryType "PIPELINE" (new) — Query is an aggregation pipeline (JSON text, a
#   list of JSON lines, or a list of stage objects) run with aggregate() on the
#   collection named in the Source/Target "Collection" field. DataFilters fill
#   $$Name / :Name placeholders; filters with no placeholder are appended as a
#   final $match stage (the same result a VIEW gives).
# Every other QueryType behaves exactly as before this change.
# ─────────────────────────────────────────────────────────────────────────────
PIPELINE_QUERY_TYPES = ("PIPELINE",)  # the only QueryType with new behaviour
COLLECTION_QUERY_TYPES = ("VIEW", "COLLECTION", "TABLE", "")


def is_pipeline_query(query_type) -> bool:
    """True when a MongoDB QueryType means an aggregation pipeline."""
    return (query_type or "").strip().upper() in PIPELINE_QUERY_TYPES


def _json_loads(text: str):
    """MongoDB Extended JSON when bson is available ($date, $oid, $numberLong…)."""
    try:
        from bson import json_util
        return json_util.loads(text)
    except ImportError:  # pragma: no cover — pymongo is always installed in the framework
        return json.loads(text)


def console_text(text):
    """extra= for a log call: the terminal shows `text`, the log file the full message."""
    return {"console_text": text}


# extra= for the second query of a check (count after sample): the terminal
# already said the check ran for this field, so only the log file shows it.
CONSOLE_HIDE = {"console_hide": True}
# extra= for a terminal-only summary line (the log file has the full queries).
FILE_HIDE = {"file_hide": True}


def duplicate_check_console_line(sourceType, sourceDB, sourceQuery, n_fields):
    """
    The one terminal line for a whole Duplicate_Check (each field's queries
    are written to the log file only). Not written to the log file.
    """
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
    sourceQuery = str(sourceQuery or "")
    stype = (sourceType or "").upper()
    if not n_fields:
        return
    if stype == "ORACLE":
        obj = (query_object(sourceQuery) if "select " in sourceQuery.lower()
               else (sourceQuery if "." in sourceQuery else f"{sourceDB}.{sourceQuery}"))
        logger.info(f"Source [ORACLE] Duplicate_Check Oracle Query is executed on {obj} "
                    f"for {n_fields} field(s) — full query in log file", extra=FILE_HIDE)
    elif stype == "MONGODB":
        logger.info(f"Source [MONGODB PIPELINE] Duplicate_Check Mongo Pipeline is executed on "
                    f"{sourceDB}.{sourceQuery} for {n_fields} field(s) — full pipeline in log file",
                    extra=FILE_HIDE)


def query_object(sql) -> str:
    """Table / view a SQL query reads from (first FROM), for the short terminal line."""
    import re as _re
    m = _re.search(r"\bFROM\s+([A-Za-z0-9_$#.\"]+)", str(sql or ""), _re.I)
    return m.group(1).replace('"', "") if m else "Oracle"


def _json_dumps(obj) -> str:
    try:
        from bson import json_util
        return json_util.dumps(obj)
    except ImportError:  # pragma: no cover
        return json.dumps(obj, default=str)


def normalize_query_text(query):
    """
    Returns the Query as one string, whatever form the test case stores it in:
      - "text"                          → unchanged
      - ["line 1", "line 2", ...]       → joined with spaces (SQL or pipeline JSON)
      - [{"$match": ...}, {...}]        → Extended-JSON text of the pipeline
      - {"$match": ...} / {"pipeline":} → Extended-JSON text
    Strings keep DataFilter ":placeholders" intact so they can be substituted.
    """
    if query is None or isinstance(query, str):
        return query
    if isinstance(query, dict):
        return _json_dumps(query)
    if isinstance(query, (list, tuple)):
        if all(isinstance(q, str) for q in query):
            return " ".join(query)
        return _json_dumps(list(query))
    return str(query)


# ─────────────────────────────────────────────────────────────────────────────
# mongosh / Compass syntax → Extended JSON
# ─────────────────────────────────────────────────────────────────────────────

_SHELL_TYPES = {
    "NumberInt": lambda v: str(int(float(v))),
    "Int32": lambda v: str(int(float(v))),
    "NumberLong": lambda v: json.dumps({"$numberLong": str(int(float(v)))}),
    "Long": lambda v: json.dumps({"$numberLong": str(int(float(v)))}),
    "NumberDecimal": lambda v: json.dumps({"$numberDecimal": str(v)}),
    "Decimal128": lambda v: json.dumps({"$numberDecimal": str(v)}),
    "Double": lambda v: str(float(v)),
    "ISODate": lambda v: json.dumps({"$date": str(v)}),
    "Date": lambda v: json.dumps({"$date": str(v)}),
    "ObjectId": lambda v: json.dumps({"$oid": str(v)}),
}


class _ShellSyntaxError(ValueError):
    pass


def mongosh_to_json(text: str) -> str:
    """
    Rewrites a pipeline written for mongosh / Compass into JSON:
      {$count: 'N'}            → {"$count": "N"}
      NumberInt(1)             → 1
      NumberLong(5)            → {"$numberLong": "5"}
      ISODate('2026-01-01')    → {"$date": "2026-01-01"}   (also new Date(...))
      ObjectId('65f…')         → {"$oid": "65f…"}
      trailing commas, // and /* */ comments are dropped.
    Text inside quotes is never changed, so placeholders like ':Status' and
    '$$Business_ID' keep working.
    """
    out, i, n = [], 0, len(text)

    def read_string(i):
        quote = text[i]
        j, buf = i + 1, []
        while j < n:
            ch = text[j]
            if ch == "\\" and j + 1 < n:
                nxt = text[j + 1]
                buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                j += 2
                continue
            if ch == quote:
                return "".join(buf), j + 1
            buf.append(ch)
            j += 1
        raise _ShellSyntaxError(f"a quote opened at position {i} is never closed")

    def skip_space(j):
        while j < n:
            if text[j].isspace():
                j += 1
            elif text.startswith("//", j):
                j = text.find("\n", j)
                j = n if j < 0 else j
            elif text.startswith("/*", j):
                k = text.find("*/", j + 2)
                j = n if k < 0 else k + 2
            else:
                break
        return j

    while i < n:
        ch = text[i]
        if ch.isspace() or text.startswith("//", i) or text.startswith("/*", i):
            i = skip_space(i)
            out.append(" ")
            continue
        if ch in "\"'":
            value, i = read_string(i)
            out.append(json.dumps(value))
            continue
        if ch == ",":
            j = skip_space(i + 1)
            if j < n and text[j] in "]}":
                i = j  # trailing comma
                continue
            out.append(",")
            i += 1
            continue
        if ch.isalpha() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$."):
                j += 1
            word = text[i:j]
            k = skip_space(j)
            if word == "new":
                i = k
                continue
            if k < n and text[k] == ":":
                out.append(json.dumps(word))  # unquoted key
                i = j
                continue
            if word in _SHELL_TYPES and k < n and text[k] == "(":
                m = skip_space(k + 1)
                if m < n and text[m] in "\"'":
                    arg, m = read_string(m)
                else:
                    e = m
                    while e < n and text[e] not in "),":
                        e += 1
                    arg, m = text[m:e].strip(), e
                m = skip_space(m)
                if m >= n or text[m] != ")":
                    raise _ShellSyntaxError(f"{word}( at position {i} is not closed")
                if word == "Date" and arg == "":
                    raise _ShellSyntaxError("new Date() needs a fixed date, e.g. ISODate('2026-01-01')")
                out.append(_SHELL_TYPES[word](arg))
                i = m + 1
                continue
            out.append(word)  # true / false / null (JSON keeps them)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_pipeline(query) -> list:
    """
    Turns a pipeline Query into a list of stage dicts. Accepts JSON text, a list
    of text lines, a list of stage dicts, a single stage dict, or an object of
    the form {"pipeline": [...]}. Raises ValueError with a readable message.
    """
    if isinstance(query, (list, tuple)) and query and all(isinstance(q, dict) for q in query):
        stages = list(query)
    else:
        text = normalize_query_text(query)
        if not text or not str(text).strip():
            raise ValueError("Aggregation pipeline Query is empty.")
        try:
            stages = _json_loads(text)
        except Exception as exc:
            # Not strict JSON — try mongosh / Compass syntax: unquoted keys,
            # 'single quotes', NumberInt(1), ISODate("…"), ObjectId("…"),
            # trailing commas and // comments.
            try:
                stages = _json_loads(mongosh_to_json(text))
            except Exception as exc2:
                raise ValueError(
                    f"Aggregation pipeline Query could not be read ({exc2}). "
                    f"Check that every {{ [ ( has its closing }} ] ) and every quote is closed. "
                    f"Both JSON and mongosh syntax are accepted, e.g. "
                    f"[{{$match: {{Status: 'A'}}}}, {{$count: 'RECORD_COUNT'}}]. "
                    f"Placeholders must sit inside quotes, e.g. ':Status'."
                ) from exc2
    if isinstance(stages, dict):
        if "pipeline" in stages:
            stages = stages["pipeline"]
        elif (len(stages) == 1 and isinstance(next(iter(stages.values())), list)
              and not str(next(iter(stages))).startswith("$")):
            # A wrapper such as {"Select": [ ...stages... ]} copied from a job config
            stages = next(iter(stages.values()))
        else:
            stages = [stages]
    if not isinstance(stages, list) or not all(isinstance(st, dict) for st in stages):
        raise ValueError("Aggregation pipeline must be a JSON array of stage objects.")
    for i, st in enumerate(stages, 1):
        if len(st) != 1 or not str(next(iter(st))).startswith("$"):
            raise ValueError(
                f"Pipeline stage {i} must have exactly one $operator key, got {list(st)}."
            )
    return stages


# MongoDB's own variables, plus names a pipeline defines itself ("as", "let")
# are legitimate $$ references; anything else is a parameter that must come
# from a DataFilter (or -f) or MongoDB rejects the pipeline.
_MONGO_SYSTEM_VARS = {"ROOT", "CURRENT", "REMOVE", "NOW", "CLUSTER_TIME", "DESCEND",
                      "PRUNE", "KEEP", "SEARCH_META", "USER_ROLES", "value", "this"}


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _pipeline_defined_vars(stages) -> set:
    names = set()
    for node in _walk(stages):
        if isinstance(node, dict):
            if isinstance(node.get("as"), str):
                names.add(node["as"])
            if isinstance(node.get("let"), dict):  # $lookup let
                names.update(node["let"].keys())
            if isinstance(node.get("vars"), dict):  # $let: {vars: {...}, in: ...}
                names.update(node["vars"].keys())
    return names


_VAR_RE = re.compile(r"\$\$([A-Za-z_][A-Za-z0-9_]*)")


def unresolved_pipeline_params(stages) -> list:
    """$$Name references that neither MongoDB nor the pipeline itself defines."""
    defined = _pipeline_defined_vars(stages) | _MONGO_SYSTEM_VARS
    found = set()
    for node in _walk(stages):
        for v in ([node] if isinstance(node, str) else
        list(node.keys()) if isinstance(node, dict) else []):
            if isinstance(v, str):
                found.update(m.group(1) for m in _VAR_RE.finditer(v))
    return sorted(found - defined)


def _param_patterns(name):
    """Placeholder spellings for a DataFilter field: $$Name or :Name, any case
    (:LoadDate, :LOADDATE and :loaddate are the same placeholder)."""
    n = re.escape(name)
    return (re.compile(r"\$\$" + n + r"(?![A-Za-z0-9_])", re.IGNORECASE),
            re.compile(r"(?<![A-Za-z0-9_$:]):" + n + r"(?![A-Za-z0-9_])", re.IGNORECASE))


def substitute_pipeline_params(query, values: dict):
    """
    Fills DataFilter values into a pipeline. Placeholders are $$Name (the style
    used in job configs) or :Name. A string that is exactly one placeholder is
    replaced by a TYPED value — dates become BSON dates, numbers become numbers —
    so {"$gt": "$$LastLoadDate"} compares dates, not text. A placeholder inside
    a longer string is replaced as text. Pipeline variables ($$item, $$p_id …)
    are left alone because only DataFilter field names are substituted.
    Returns (pipeline_text, set_of_fields_used).
    """
    from .parameter_reading import _to_mongo_value
    stages = parse_pipeline(query)
    defined = _pipeline_defined_vars(stages) | _MONGO_SYSTEM_VARS
    defined_lower = {d.lower() for d in defined}
    params = {k: v for k, v in (values or {}).items() if k.lower() not in defined_lower}
    used = set()

    def fill(node):
        if isinstance(node, dict):
            return {k: fill(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill(v) for v in node]
        if isinstance(node, str):
            for name, val in params.items():
                dollar, colon = _param_patterns(name)
                if dollar.fullmatch(node) or colon.fullmatch(node):
                    used.add(name)
                    return _to_mongo_value(str(val))
            out = node
            for name, val in params.items():
                dollar, colon = _param_patterns(name)
                new = colon.sub(str(val), dollar.sub(str(val), out))
                if new != out:
                    used.add(name)
                    out = new
            return out
        return node

    filled = fill(stages)
    return _json_dumps(filled), used


def pipeline_display(query) -> str:
    """One stage per line — readable in the run log without 400-line dumps."""
    try:
        stages = parse_pipeline(query)
    except Exception:
        return str(query)
    return "[\n  " + ",\n  ".join(_json_dumps(st) for st in stages) + "\n]"


def _flatten_doc(doc, prefix="", out=None):
    """Nested sub-documents → dotted keys (SourceIdentity.MCPMember_ID); arrays kept."""
    out = {} if out is None else out
    for k, v in doc.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            _flatten_doc(v, key + ".", out)
        else:
            out[key] = v
    return out


def run_mongo_query(db, query, environment, query_type, mongo_filter=None,
                    collection=None, count_only=False, side="Source"):
    """
    Runs a MongoDB aggregation for QueryType "PIPELINE" and returns the rows as
    a list (nested sub-documents flattened to dotted keys), or the row count
    when count_only=True. Only called for QueryType PIPELINE.
    """
    mongo_filter = mongo_filter or {}
    if not collection:
        raise ValueError(
            f"{side}: QueryType 'PIPELINE' needs a 'Collection' field naming the "
            f"collection the pipeline runs on (Query holds the pipeline)."
        )
    stages = parse_pipeline(query)
    missing = unresolved_pipeline_params(stages)
    if missing:
        # Not fatal: MongoDB reports a truly undefined variable itself.
        logger.warning(
            f"{side}: pipeline references {', '.join('$$' + m for m in missing)} "
            f"which is not defined in the pipeline. If it is a run-time value, add "
            f"a DataFilter with that SourceField/TargetField and a DefaultValue."
        )
    if mongo_filter:
        stages = stages + [{"$match": mongo_filter}]
    coll = Collection(db, collection, environment)
    if count_only:
        res = list(coll.getPipeData(stages + [{"$count": "_n"}]))
        return int(res[0]["_n"]) if res else 0
    # Flatten nested sub-documents so JoinFields / CompareFields / CheckFields
    # can name them with dots, e.g. "SourceIdentity.MCPMember_ID".
    return [_flatten_doc(d) for d in coll.getPipeData(stages)]


def get_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
             targetType, targetDB, targetQuery, targetEnvKey,
             sourceQueryType, targetQueryType,
             source_filter=None, target_filter=None,
             source_def=None, target_def=None,
             source_collection=None, target_collection=None):
    """
    Fetches data from source and target.
    Query can be a string or list of strings — list joined with space.

    New optional params (zero impact on existing calls):
        source_def : Full Source dict from test case — used when sourceType
                     is a file type (SFTP, CSV, LOCAL_PATH etc.)
        target_def : Full Target dict from test case — used when targetType
                     is a file type.

    File types auto-routed to File_parser.fetch_and_parse().
    Result returned as list of dicts — same format as Oracle/MongoDB.
    """
    source_data, target_data = None, None
    source_filter = source_filter or {}
    target_filter = target_filter or {}

    # File type detection
    from .File_fetcher import FILE_TYPES as _FILE_TYPES

    # ── Normalise query — list → string (SQL lines or pipeline JSON) ──────
    sourceQuery = normalize_query_text(sourceQuery)
    targetQuery = normalize_query_text(targetQuery)

    # ── Source ────────────────────────────────────────────────────────────
    try:
        src_type_upper = (sourceType or "").upper()

        if src_type_upper in _FILE_TYPES and source_def:
            # File source — fetch and parse
            source_data = _fetch_file_data(source_def)

        elif src_type_upper == "MONGODB":
            sourceEnvKey = sourceEnvKey.upper()
            if is_pipeline_query(sourceQueryType):
                source_data = run_mongo_query(
                    sourceDB, sourceQuery, sourceEnvKey, sourceQueryType,
                    source_filter, collection=source_collection, side="Source"
                )
            elif sourceQueryType.upper() in ("VIEW", "COLLECTION"):
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = source_view.getData(source_filter)
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                execute_js_from_query(sourceQuery, 125)

        elif src_type_upper == "ORACLE":
            sourceEnvKey = sourceEnvKey.upper()
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery or "").lower():
                if "." in (sourceQuery or ""):
                    sourceQuery = "Select * from " + sourceQuery
                else:
                    sourceQuery = "Select * from " + sourceDB + "." + sourceQuery
            source_data = source_connection.fetchAllDict(sourceQuery)

    except Exception as e:
        logger.issue(f"Error fetching source data ({sourceType}): {e}")

    # ── Target ────────────────────────────────────────────────────────────
    try:
        tgt_type_upper = (targetType or "").upper()

        if tgt_type_upper in _FILE_TYPES and target_def:
            # File target — fetch and parse
            target_data = _fetch_file_data(target_def)

        elif tgt_type_upper == "MONGODB":
            targetEnvKey = targetEnvKey.upper()
            if is_pipeline_query(targetQueryType):
                target_data = run_mongo_query(
                    targetDB, targetQuery, targetEnvKey, targetQueryType,
                    target_filter, collection=target_collection, side="Target"
                )
            else:
                target_view = Collection(targetDB, targetQuery, targetEnvKey)
                target_data = target_view.getData(target_filter)

        elif tgt_type_upper == "ORACLE":
            targetEnvKey = targetEnvKey.upper()
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery or "").lower():
                if "." in (targetQuery or ""):
                    targetQuery = "Select * from " + targetQuery
                else:
                    targetQuery = "Select * from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchAllDict(targetQuery)

    except Exception as e:
        logger.issue(f"Error fetching target data ({targetType}): {e}")

    return source_data, target_data


def _apply_file_filter_to_path(source_def: dict) -> dict:
    """
    Substitutes filter values into :FieldName placeholders in the Path,
    FileName and FilePattern of a file-type source definition.

    Two placeholder syntaxes are supported:

    1. Simple replacement
       :FieldName  →  resolved_value

       Path "004/shared/:Business_ID/:Date/"
       →    "004/shared/IND/20240801/"

    2. Concatenation with  ||
       :FieldName||literal   →  resolved_value + literal
       :FieldName||:Field2   →  resolved_value1 + resolved_value2

       Path "004/shared/:Business_ID||HV/:Date/"
       →    "004/shared/INDHV/20240801/"

       Multiple  ||  are allowed in one token:
       Path "reports/:Year||Q||:Quarter/"
       →    "reports/2024Q3/"

    The || syntax is preferred over embedding the field name inside a longer
    word (e.g. :Business_IDHV vs :Business_ID||HV) because it is explicit and
    never ambiguous when multiple filter fields have overlapping prefixes.

    Both styles work; there is no need to migrate existing templates.

    Longer field names are tried first so :Business_ID is never eaten by a
    hypothetical shorter :Business field.  The original dict is never mutated.
    """
    import re

    filters = source_def.get("_file_filters") or []
    if not filters:
        return source_def

    # Build a lookup: field_name (upper) → resolved value
    value_map = {flt["field"].upper(): flt["value"] for flt in filters}

    # Match a token that starts with :FieldName and is optionally followed by
    # one or more   ||part   segments where each part is either another
    # :FieldName or a run of characters that are not  : | / \  (literal text).
    _PART = r"(?::[A-Za-z_][A-Za-z0-9_]*|[^:|/|]+)"
    _TOKEN = re.compile(r":[A-Za-z_][A-Za-z0-9_]*(?:\|\|" + _PART + r")*")

    def _resolve_token(m: re.Match) -> str:
        """Replace a single matched token with its resolved value."""
        token = m.group(0)  # e.g.  ":Business_ID||HV"  or  ":Date"

        # Split on || to get the individual parts of the concatenation.
        raw_parts = re.split(r"\|\|", token)
        result_parts = []

        for part in raw_parts:
            if part.startswith(":"):
                field_upper = part[1:].upper()
                if field_upper in value_map:
                    result_parts.append(value_map[field_upper])
                    logger.info(
                        f"File DataFilter: resolved '{part}' "
                        f"→ '{value_map[field_upper]}'"
                    )
                else:
                    # Unknown field — leave the placeholder as-is so it is
                    # visible rather than silently dropped.
                    result_parts.append(part)
                    logger.warning(
                        f"File DataFilter: no value for '{part}' "
                        f"— placeholder kept."
                    )
            else:
                result_parts.append(part)  # literal suffix

        return "".join(result_parts)

    def _substitute(text: str) -> tuple:
        """
        Returns (substituted_text, set_of_field_names_consumed).

        Two passes run in order:

        Pass 1 — explicit  ||  tokens  (regex-driven)
          ":Business_ID||HV" → "INDHV"
          ":Year||Q||:Quarter" → "2024Q3"
          Handled entirely by the _TOKEN regex.

        Pass 2 — embedded field names  (substring, backward-compat)
          ":Business_IDHV" → "INDHV"   (Business_ID embedded in longer word)
          Only runs if Pass 1 left any   :   characters in the text.
          Longest known field name tried first to avoid prefix collisions.

        A field is "consumed" when its :FieldName reference was resolved in
        either pass; consumed fields are excluded from the DataFrame row-filter
        step so path-routing fields are never treated as column filters.
        """
        if not text:
            return text, set()

        consumed = set()

        # ── Pass 1: explicit || syntax ──────────────────────────────────────
        def _replace(m: re.Match):
            token = m.group(0)
            result = _resolve_token(m)
            # Only mark as consumed when the field was actually resolved
            # (i.e. found in value_map). An unrecognised field like
            # ":Business_IDHV" stays in the text and is handled by pass 2;
            # recording it as consumed here would suppress that pass.
            for part in re.split(r"[|][|]", token):
                if part.startswith(":"):
                    field_upper = part[1:].upper()
                    if field_upper in value_map:
                        consumed.add(field_upper)
            return result

        text = _TOKEN.sub(_replace, text)

        # ── Pass 2: embedded placeholders (:Business_IDHV → INDHV) ─────────
        # Only needed when a colon remains (Pass 1 resolved all || tokens).
        if ":" in text:
            # Sort longest field name first to avoid shorter prefix stealing.
            for flt in sorted(filters, key=lambda f: len(f["field"]), reverse=True):
                placeholder = f":{flt['field']}"
                if placeholder in text:
                    text = text.replace(placeholder, flt["value"])
                    consumed.add(flt["field"].upper())
                    logger.info(
                        f"File DataFilter (embedded): replaced '{placeholder}' "
                        f"→ '{flt['value']}'"
                    )

        return text, consumed

    copy = dict(source_def)
    consumed = set()

    for key in ("Path", "FileName", "FilePattern"):
        if copy.get(key):
            copy[key], used = _substitute(copy[key])
            consumed |= used

    # Remove path-consumed filters so they are not applied as row filters.
    if consumed:
        copy["_file_filters"] = [
            f for f in filters if f["field"].upper() not in consumed
        ]

    # Remove consumed filters so the DataFrame pass does not try to match
    # path-routing fields against column headers.
    if consumed:
        copy["_file_filters"] = [
            f for f in filters if f["field"] not in consumed
        ]

    return copy


def _apply_file_filter_to_dataframe(df, source_def: dict):
    """
    Applies resolved file filters as pandas row filters to df AFTER parsing.

    Only filters whose field name matches a column in df are applied; filters
    used for path substitution may not correspond to any column and are silently
    skipped. Supports operators: =, !=, >, >=, <, <=, BETWEEN.

    Returns the filtered DataFrame.
    """
    import pandas as pd

    filters = source_def.get("_file_filters") or []
    if not filters or df is None or df.empty:
        return df

    for flt in filters:
        field = flt["field"]
        operator = flt["operator"].upper()
        value = flt["value"]

        if field not in df.columns:
            logger.debug(
                f"File DataFilter '{field}' not found in DataFrame columns "
                f"({list(df.columns)}) — skipping row filter."
            )
            continue

        col = df[field]

        try:
            if operator == "=":
                df = df[col.astype(str) == str(value)]
            elif operator in ("!=", "<>"):
                df = df[col.astype(str) != str(value)]
            elif operator == ">":
                df = df[pd.to_numeric(col, errors="coerce") > float(value)]
            elif operator == ">=":
                df = df[pd.to_numeric(col, errors="coerce") >= float(value)]
            elif operator == "<":
                df = df[pd.to_numeric(col, errors="coerce") < float(value)]
            elif operator == "<=":
                df = df[pd.to_numeric(col, errors="coerce") <= float(value)]
            elif operator == "BETWEEN":
                parts = [v.strip() for v in value.split(",")]
                if len(parts) == 2:
                    lo, hi = float(parts[0]), float(parts[1])
                    numeric = pd.to_numeric(col, errors="coerce")
                    df = df[(numeric >= lo) & (numeric <= hi)]
            else:
                logger.warning(
                    f"File DataFilter '{field}': "
                    f"unsupported operator '{operator}' — skipping."
                )
                continue

            logger.info(
                f"File DataFilter applied: {field} {operator} {value!r} "
                f"→ {len(df)} row(s) remaining."
            )
        except Exception as exc:
            logger.warning(
                f"File DataFilter '{field} {operator} {value}' failed: {exc}"
            )

    return df


def _fetch_file_data(source_def: dict) -> list:
    """
    Fetches and parses a file source definition.
    DataFilter support for file types
    ----------------------------------
    Before fetching, :FieldName placeholders in Path / FileName / FilePattern
    are replaced with the resolved filter values (runtime -f first, then
    DefaultValue). This lets a single test case select different folders at
    runtime:

        Source.Path   = "004/shared/:Business_IDHV/:Date/"
        -f Business_ID=CORP Date=20250101
        → fetches from "004/shared/CORPHV/20250101/"

    After parsing, any filter whose FieldName matches a column header in the
    resulting DataFrame is applied as a pandas row filter.
    Returns list of dicts — same format as Oracle/MongoDB results.
    Raises on parse errors so test case is marked FAIL.
    """
    from .File_parser import fetch_and_parse

    # Step 1 — path substitution (:FieldName → resolved value)
    source_def = _apply_file_filter_to_path(source_def)
    result = fetch_and_parse(
        source=source_def,
        retry_attempts=_get_file_config_int("retry_attempts", 2),
        retry_wait_minutes=_get_file_config_float("retry_wait_minutes", 5),
        add_source_file_column=source_def.get("AddSourceFileColumn", False)
    )

    # Log parse errors and fail if any
    # Step 2 — post-parse row filtering (columns that match filter fields)
    df = result.get("dataframe")
    if df is not None:
        df = _apply_file_filter_to_dataframe(df, source_def)
        result["dataframe"] = df
    if result["parse_errors"]:
        for err in result["parse_errors"]:
            logger.issue(
                f"File parse error — Row {err.get('RowNumber', '?')}: "
                f"Column '{err.get('Column', '?')}': {err.get('Reason', '')} "
                f"| Raw: {str(err.get('RawContent', ''))[:100]}"
            )

    if result["schema_errors"]:
        for err in result["schema_errors"]:
            logger.issue(f"File schema error — {err}")
        raise ValueError(
            f"Schema validation failed — "
            f"{len(result['schema_errors'])} error(s). "
            f"Cannot proceed with comparison."
        )

    if result["footer_errors"]:
        for err in result["footer_errors"]:
            logger.issue(f"File footer validation error — {err}")

    df = result["dataframe"]
    if df.empty:
        return []

    # Convert DataFrame to list of dicts — same as Oracle/MongoDB
    return df.to_dict(orient="records")


def _get_file_config_int(key: str, default: int) -> int:
    """Reads integer file processing config from environment.ini."""
    try:
        val = cr.get_conf_data("FILE_PROCESSING", key)
        return int(val) if val else default
    except Exception:
        return default


def _get_file_config_float(key: str, default: float) -> float:
    """Reads float file processing config from environment.ini."""
    try:
        val = cr.get_conf_data("FILE_PROCESSING", key)
        return float(val) if val else default
    except Exception:
        return default


def get_count(sourceType, sourceDB, sourceQuery, sourceEnvKey,
              targetType, targetDB, targetQuery, targetEnvKey,
              sourceQueryType, targetQueryType,
              source_filter=None, target_filter=None,
              source_def=None, target_def=None,
              source_collection=None, target_collection=None):
    """
    Fetches counts from source and target.
    Query can be a string or list of strings — list joined with space.

    New optional params (zero impact on existing calls):
        source_def : Full Source dict — used when sourceType is a file type.
        target_def : Full Target dict — used when targetType is a file type.

    File row count returned as integer directly.
    """
    source_data, target_data = None, None
    source_filter = source_filter or {}
    target_filter = target_filter or {}

    from .File_fetcher import FILE_TYPES as _FILE_TYPES

    # ── Normalise query — list → string (SQL lines or pipeline JSON) ──────
    sourceQuery = normalize_query_text(sourceQuery)
    targetQuery = normalize_query_text(targetQuery)

    # ── Source ────────────────────────────────────────────────────────────
    try:
        src_type_upper = (sourceType or "").upper()

        if src_type_upper in _FILE_TYPES and source_def:
            # File source — count rows
            rows = _fetch_file_data(source_def)
            source_data = len(rows) if rows is not None else 0
            logger.info(
                f"File_fetcher: Source file row count = {source_data}"
            )

        elif src_type_upper == "MONGODB":
            sourceEnvKey = sourceEnvKey.upper()
            if is_pipeline_query(sourceQueryType):
                source_data = run_mongo_query(
                    sourceDB, sourceQuery, sourceEnvKey, sourceQueryType,
                    source_filter, collection=source_collection,
                    count_only=True, side="Source"
                )
            elif sourceQueryType.upper() in ("VIEW", "COLLECTION"):
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = source_view.getDataCount(source_filter)
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                execute_js_from_query(sourceQuery, 101)

        elif src_type_upper == "ORACLE":
            sourceEnvKey = sourceEnvKey.upper()
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery or "").lower():
                if "." in (sourceQuery or ""):
                    sourceQuery = "Select count(*) from " + sourceQuery
                else:
                    sourceQuery = "Select count(*) from " + sourceDB + "." + sourceQuery
            source_data = source_connection.fetchOne(sourceQuery)

    except Exception as e:
        logger.issue(f"Error fetching source count ({sourceType}): {e}")

    # ── Target ────────────────────────────────────────────────────────────
    try:
        tgt_type_upper = (targetType or "").upper()

        if tgt_type_upper in _FILE_TYPES and target_def:
            # File target — count rows
            rows = _fetch_file_data(target_def)
            target_data = len(rows) if rows is not None else 0
            logger.info(
                f"File_fetcher: Target file row count = {target_data}"
            )

        elif tgt_type_upper == "MONGODB":
            targetEnvKey = targetEnvKey.upper()
            if is_pipeline_query(targetQueryType):
                target_data = run_mongo_query(
                    targetDB, targetQuery, targetEnvKey, targetQueryType,
                    target_filter, collection=target_collection,
                    count_only=True, side="Target"
                )
            else:
                target_view = Collection(targetDB, targetQuery, targetEnvKey)
                target_data = target_view.getDataCount(target_filter)

        elif tgt_type_upper == "ORACLE":
            targetEnvKey = targetEnvKey.upper()
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery or "").lower():
                if "." in (targetQuery or ""):
                    targetQuery = "Select count(*) from " + targetQuery
                else:
                    targetQuery = "Select count(*) from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchOne(targetQuery)

    except Exception as e:
        logger.issue(f"Error fetching target count ({targetType}): {e}")

    return source_data, target_data


def get_next_sequence_id(environment):
    try:
        sequence_collection = Collection("Quality", "ValidationSequenceNums", environment)
        seq_record = sequence_collection.find_one({"SequenceCode": "DataValidationLog_ID"})
        if not seq_record:
            raise Exception("Sequence record not found for DataValidationLog_ID")
        current_id = seq_record.get("SequenceNbr", 0)
        new_id = current_id + 1
        sequence_collection.UpdateData({"SequenceCode": "DataValidationLog_ID"}, {"$set": {"SequenceNbr": new_id}})
        return new_id
    except Exception as e:
        logger.issue(f"Error fetching target data : {e}")


def execute_js_from_query(js_code_string, payment_event_id):
    js_path = None  # Define early to avoid reference error

    try:
        # Step 1: Write JS code to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.js', mode='w', encoding='utf-8') as temp_js_file:
            temp_js_file.write(js_code_string)
            js_path = temp_js_file.name

        # Step 2: Construct the mongosh command
        command = [
            r"C:\Program Files\mongosh\mongosh.exe",
            "--eval", f"var paymentEventID={payment_event_id}",
            js_path
        ]

        # Step 3: Execute the command
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60  # avoid hanging
        )

        # Step 4: Output or handle result
        print("MongoDB Script Output:\n", result.stdout.strip())
        if result.stderr.strip():
            print("Errors:\n", result.stderr.strip())

    except FileNotFoundError as e:
        print(f"[ERROR] mongosh not found: {e}")

    except subprocess.TimeoutExpired:
        print("[ERROR] Script timed out while executing.")

    except Exception as e:
        print(f"[ERROR] Unexpected failure: {e}")

    finally:
        # Step 5: Cleanup
        if js_path and os.path.exists(js_path):
            os.remove(js_path)


def _detect_array_paths(collection, field_path: str) -> list:
    """
    Determines which path segments need $unwind by sampling documents
    that contain the top-level key and checking for list values.

    Samples up to 5 documents to handle mixed schemas (some docs store
    a field as array, others as dict — takes the first array hit).

    Returns list of dot-notation paths that need $unwind, in order.
    Returns [] for flat fields (no arrays anywhere in the path).
    """
    try:
        top_key = field_path.split(".")[0]
        parts = field_path.split(".")

        # Sample docs that have the top-level key
        docs = list(collection.find({top_key: {"$exists": True}}).limit(5))
        if not docs:
            logger.debug(
                f"_detect_array_paths: '{top_key}' not found in any sampled "
                f"document — treating '{field_path}' as flat."
            )
            return []

        # Walk each sampled doc — take first one that reveals an array
        for sample in docs:
            current = sample
            path_so_far = []
            unwind_paths = []

            for part in parts[:-1]:
                if isinstance(current, dict):
                    current = current.get(part)
                elif isinstance(current, list):
                    current = (
                        current[0].get(part)
                        if current and isinstance(current[0], dict)
                        else None
                    )
                else:
                    current = None
                    break

                if current is None:
                    break

                path_so_far.append(part)

                if isinstance(current, list):
                    unwind_paths.append(".".join(path_so_far))
                    current = current[0] if current and isinstance(current[0], dict) else {}

            if unwind_paths:
                logger.debug(
                    f"_detect_array_paths: '{field_path}' → unwind paths: {unwind_paths}"
                )
                return unwind_paths

        # No sample showed an array — treat as flat
        logger.debug(
            f"_detect_array_paths: '{field_path}' → no arrays detected in "
            f"{len(docs)} sampled doc(s) — treating as flat."
        )
        return []

    except Exception as e:
        logger.warning(f"Could not detect array paths for '{field_path}': {e}")
        return []


def get_null_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                  sourceQueryType, check_fields, data_filters=None):
    """
    Fetches only records where any of the specified fields are null or empty.
    Query can be a string or list of strings — list joined with space.

    For flat fields    : uses .find() with $or null filter.
    For array-nested   : uses .aggregate() with $unwind + $match pushed to DB,
                         so MongoDB filters before sending data across the wire.
    Mixed (flat+array) : always uses pipeline path for consistency.
    """
    source_data = None
    sourceEnvKey = sourceEnvKey.upper()
    data_filters = data_filters or {}

    # ── Normalise query — list → string joined with space
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)

    # ── Build null field list ─────────────────────────────────────────────────
    null_fields = []
    for f in check_fields:
        src = f.get("SourceField", "")
        if isinstance(src, list):
            null_fields.extend([s for s in src if s])
        elif src:
            null_fields.append(src)

    # ── Build result fields ───────────────────────────────────────────────────
    result_fields = set()
    for f in check_fields:
        res = f.get("ResultFields", [])
        if isinstance(res, str):
            result_fields.add(res)
        elif isinstance(res, list):
            result_fields.update(res)
    result_fields.update(null_fields)
    result_fields = [f for f in result_fields if f]

    try:
        # ── MongoDB ───────────────────────────────────────────────────────────
        if sourceType and sourceType.upper() == "MONGODB":
            if sourceQueryType.upper() in ("VIEW", "COLLECTION"):

                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)

                # ── Classify fields: flat vs array-nested ─────────────────────
                flat_fields = []
                array_nested_fields = []  # list of (field, [unwind_paths])

                for field in null_fields:
                    array_paths = _detect_array_paths(source_view.DbCollection, field)

                    if not array_paths and "." in field:
                        # Sampling returned flat but field is dotted —
                        # do one extra check: is the top-level key ever a list?
                        top_key = field.split(".")[0]
                        probe = source_view.DbCollection.find_one(
                            {top_key: {"$type": "array"}}
                        )
                        if probe is not None:
                            array_paths = [top_key]
                            logger.debug(
                                f"_detect_array_paths fallback: '{field}' — "
                                f"'{top_key}' confirmed as array via $type probe. "
                                f"Using unwind: {array_paths}"
                            )

                    if array_paths:
                        array_nested_fields.append((field, array_paths))
                    else:
                        flat_fields.append(field)

                # ── Projection — top-level keys only ──────────────────────────
                projection = {}
                for f in result_fields:
                    projection[f.split(".")[0]] = 1
                projection["_id"] = 0

                if not array_nested_fields:
                    # ── Fast path — all fields are flat, use .find() ──────────
                    null_filter = {
                        "$or": (
                                [{field: None} for field in flat_fields] +
                                [{field: ""} for field in flat_fields]
                        )
                    }
                    mongo_filter = (
                        {"$and": [data_filters, null_filter]}
                        if data_filters else null_filter
                    )
                    logger.info(
                        f"Source [MONGODB]: db.{sourceQuery}.find(\n"
                        f"{json.dumps(mongo_filter, indent=2, default=str)}\n)",
                        extra=console_text(f"Source [MONGODB] Null_Check Mongo Query is executed on "
                                           f"{sourceQuery} — full query in log file")
                    )
                    source_data = source_view.getDataView(mongo_filter, projection)

                else:
                    # ── Pipeline path — array-nested fields need $unwind ──────
                    # Pushing the null filter past $unwind means MongoDB only
                    # returns documents where the nested field is actually null —
                    # no full collection scan into Python.

                    pipeline = []

                    # Stage 1: apply DataFilters early to cut the scan
                    if data_filters:
                        pipeline.append({"$match": data_filters})

                    # Stage 2: $unwind every array path found (deduplicated)
                    seen_unwinds = []
                    for _field, unwind_paths in array_nested_fields:
                        for path in unwind_paths:
                            stage = {"$unwind": f"${path}"}
                            if stage not in seen_unwinds:
                                seen_unwinds.append(stage)
                                pipeline.append(stage)

                    # Stage 3: $match — any field is null / empty / missing
                    null_conditions = []
                    for field in flat_fields:
                        null_conditions.append({field: None})
                        null_conditions.append({field: ""})
                    for field, _ in array_nested_fields:
                        null_conditions.append({field: None})
                        null_conditions.append({field: ""})
                        null_conditions.append({field: {"$exists": False}})

                    if null_conditions:
                        pipeline.append({"$match": {"$or": null_conditions}})

                    # Stage 4: project only the fields we need
                    pipeline.append({"$project": projection})

                    pipeline_str = json.dumps(pipeline, indent=2, default=str)
                    logger.info(
                        f"Source [MONGODB PIPELINE]: "
                        f"db.{sourceQuery}.aggregate(\n{pipeline_str}\n)",
                        extra=console_text(f"Source [MONGODB PIPELINE] Null_Check Mongo Pipeline is executed on "
                                           f"{sourceQuery} — full pipeline in log file")
                    )
                    source_data = source_view.getPipeData(pipeline)

        # ── Oracle ────────────────────────────────────────────────────────────
        elif sourceType and sourceType.upper() == "ORACLE":
            source_connection = OracleConnection(sourceEnvKey)

            if "select " in sourceQuery.lower():
                base_query = f"SELECT * FROM ({sourceQuery})"
            elif "." in sourceQuery:
                base_query = f"SELECT * FROM {sourceQuery}"
            else:
                base_query = f"SELECT * FROM {sourceDB}.{sourceQuery}"

            null_conditions = []
            for field in null_fields:
                null_conditions.append(
                    f"({field} IS NULL OR TRIM(TO_CHAR({field})) = '')"
                )

            null_where = " OR ".join(null_conditions)

            if data_filters:
                where_clause = f"{data_filters} AND ({null_where})"
            else:
                where_clause = null_where

            final_query = f"{base_query} WHERE {where_clause}"

            logger.info(f"Oracle Null_Check query: {final_query}",
                        extra=console_text(f"Source [ORACLE] Null_Check Oracle Query is executed on "
                                           f"{query_object(base_query)} — full query in log file"))
            source_data = source_connection.fetchAllDict(final_query)

    except Exception as e:
        logger.issue(f"Error fetching null check data ({sourceType}): {e}")

    return source_data


def get_duplicate_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                       sourceQueryType, check_fields, data_filters=None):
    """
    Fetches duplicate records per field independently.
    Each field is queried separately:
      - Sample  : capped at `missing` records/groups — for display
      - Count   : exact total duplicate value + record count — for reporting

    Returns dict:
        {
            field_label: {
                "sample"      : list of records (Oracle) or list of groups (MongoDB),
                "total_count" : int — total number of duplicate values,
                "total_recs"  : int — total records involved in duplicates,
                "capped"      : bool
            }
        }
    """
    sourceEnvKey = sourceEnvKey.upper()
    results = {}
    data_filters = data_filters or {}
    max_records = int(missing)

    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)

    for field_def in check_fields:
        src_field = field_def.get("SourceField", "")
        result_fields = field_def.get("ResultFields", [])

        if isinstance(src_field, str):
            src_fields = [src_field] if src_field else []
        else:
            src_fields = [f for f in src_field if f]

        if isinstance(result_fields, str):
            result_fields = [result_fields] if result_fields else []

        for sf in src_fields:
            if sf not in result_fields:
                result_fields = [sf] + result_fields

        if not src_fields:
            continue

        field_label = ", ".join(src_fields)

        try:
            # ── Oracle ────────────────────────────────────────────────────────
            if sourceType and sourceType.upper() == "ORACLE":
                source_connection = OracleConnection(sourceEnvKey)

                if "select " in sourceQuery.lower():
                    table_ref = f"({sourceQuery})"
                elif "." in sourceQuery:
                    table_ref = sourceQuery
                else:
                    table_ref = f"{sourceDB}.{sourceQuery}"

                select_cols = ", ".join(result_fields)
                group_by_cols = ", ".join(src_fields)

                null_exclusion = " AND ".join(
                    f"{sf} IS NOT NULL" for sf in src_fields
                )

                subquery_where = (
                    f"{data_filters} AND {null_exclusion}"
                    if data_filters else null_exclusion
                )

                dup_subquery = f"""
                    SELECT {group_by_cols}
                    FROM {table_ref}
                    WHERE {subquery_where}
                    GROUP BY {group_by_cols}
                    HAVING COUNT(*) > 1
                """

                # ── Sample query — capped at max_records ──────────────────────
                sample_query = f"""
                    SELECT {select_cols}
                    FROM {table_ref}
                    WHERE {subquery_where}
                    AND ({group_by_cols}) IN (
                        {dup_subquery}
                    )
                    AND ROWNUM <= {max_records}
                """

                logger.info(
                    f"Source [ORACLE] Duplicate_Check sample for '{field_label}':\n"
                    f"{sample_query.strip()}",
                    extra=CONSOLE_HIDE
                )

                sample_data = source_connection.fetchAllDict(sample_query)

                if not sample_data:
                    results[field_label] = {
                        "sample": [],
                        "total_count": 0,
                        "total_recs": 0,
                        "capped": False
                    }
                    continue

                # ── Count query — exact number of duplicate values ─────────────
                count_query = f"""
                    SELECT COUNT(*) AS DUP_COUNT
                    FROM (
                        {dup_subquery}
                    )
                """

                logger.info(
                    f"Source [ORACLE] Duplicate_Check count for '{field_label}':\n"
                    f"{count_query.strip()}",
                    extra=CONSOLE_HIDE
                )

                count_result = source_connection.fetchAllDict(count_query)
                total_count = (
                    count_result[0].get("DUP_COUNT", 0)
                    if count_result else 0
                )

                # ── Records count — total rows involved in duplicates ──────────
                recs_query = f"""
                    SELECT COUNT(*) AS REC_COUNT
                    FROM {table_ref}
                    WHERE {subquery_where}
                    AND ({group_by_cols}) IN (
                        {dup_subquery}
                    )
                """

                recs_result = source_connection.fetchAllDict(recs_query)
                total_recs = (
                    recs_result[0].get("REC_COUNT", 0)
                    if recs_result else 0
                )

                results[field_label] = {
                    "sample": sample_data,
                    "total_count": total_count,
                    "total_recs": total_recs,
                    "capped": total_count >= COUNT_CAP
                }

            # ── MongoDB ───────────────────────────────────────────────────────
            elif sourceType and sourceType.upper() == "MONGODB":
                if sourceQueryType.upper() in ("VIEW", "COLLECTION"):

                    source_col = Collection(sourceDB, sourceQuery, sourceEnvKey)

                    if len(src_fields) == 1:
                        group_id = f"${src_fields[0]}"
                    else:
                        group_id = {
                            sf.replace(".", "_"): f"${sf}"
                            for sf in src_fields
                        }

                    push_doc = {}
                    for rf in result_fields:
                        top_key = rf.split(".")[0]
                        push_doc[top_key] = f"${top_key}"

                    null_conditions = []
                    for sf in src_fields:
                        null_conditions.extend([
                            {sf: {"$ne": None}},
                            {sf: {"$ne": ""}},
                            {sf: {"$exists": True}}
                        ])

                    match_conditions = null_conditions.copy()
                    if data_filters:
                        match_conditions.append(data_filters)

                    # ── Sample pipeline — capped at max_records groups ─────────
                    sample_pipeline = [
                        {"$match": {"$and": match_conditions}},
                        {
                            "$group": {
                                "_id": group_id,
                                "count": {"$sum": 1},
                                "docs": {"$push": push_doc}
                            }
                        },
                        {"$match": {"count": {"$gt": 1}}},
                        {"$sort": {"count": -1}},
                        {"$limit": max_records}
                    ]

                    logger.info(
                        f"Source [MONGODB PIPELINE] Duplicate_Check sample "
                        f"for '{field_label}':\n"
                        f"db.{sourceQuery}.aggregate(\n"
                        f"{json.dumps(sample_pipeline, indent=2, default=str)}\n)",
                        extra=CONSOLE_HIDE
                    )

                    sample_data = list(source_col.getPipeData(sample_pipeline))

                    if not sample_data:
                        results[field_label] = {
                            "sample": [],
                            "total_count": 0,
                            "total_recs": 0,
                            "capped": False
                        }
                        continue

                    # ── Count pipeline — exact dup value + record counts ───────
                    count_pipeline = [
                        {"$match": {"$and": match_conditions}},
                        {
                            "$group": {
                                "_id": group_id,
                                "count": {"$sum": 1}
                            }
                        },
                        {"$match": {"count": {"$gt": 1}}},
                        {
                            "$group": {
                                "_id": None,
                                "dup_vals": {"$sum": 1},
                                "dup_recs": {"$sum": "$count"}
                            }
                        }
                    ]

                    logger.info(
                        f"Source [MONGODB PIPELINE] Duplicate_Check count "
                        f"for '{field_label}':\n"
                        f"db.{sourceQuery}.aggregate(\n"
                        f"{json.dumps(count_pipeline, indent=2, default=str)}\n)",
                        extra=CONSOLE_HIDE
                    )

                    count_result = list(source_col.getPipeData(count_pipeline))
                    total_count = (
                        count_result[0].get("dup_vals", 0)
                        if count_result else 0
                    )
                    total_recs = (
                        count_result[0].get("dup_recs", 0)
                        if count_result else 0
                    )

                    results[field_label] = {
                        "sample": sample_data,
                        "total_count": total_count,
                        "total_recs": total_recs,
                        "capped": total_count >= COUNT_CAP
                    }

        except Exception as e:
            logger.issue(
                f"Error fetching duplicate data for '{field_label}' ({sourceType}): {e}"
            )
            results[field_label] = None

    return results


FORMAT_DATE_REGEX_MAP = {
    "UTC": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
    "EST": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
    "YYYY-MM-DD": r"^\d{4}-\d{2}-\d{2}$",
    "MM/DD/YYYY": r"^\d{2}/\d{2}/\d{4}$",
    "DD/MM/YYYY": r"^\d{2}/\d{2}/\d{4}$",
    "YYYY/MM/DD": r"^\d{4}/\d{2}/\d{2}$",
    "MM-DD-YYYY": r"^\d{2}-\d{2}-\d{4}$",
    "YYYYMMDD": r"^\d{8}$",
}

COUNT_CAP = 100000


def _build_oracle_format_filter(field, format_type, pattern, value) -> str:
    format_type = (format_type or "").upper()
    if format_type == "REGEX":
        return f"NOT REGEXP_LIKE({field}, '{pattern}')"
    elif format_type == "DATE_FORMAT":
        date_regex = FORMAT_DATE_REGEX_MAP.get((value or "").upper(), "")
        if date_regex:
            return f"NOT REGEXP_LIKE(TO_CHAR({field}), '{date_regex}')"
        return f"TO_DATE(TO_CHAR({field}), 'YYYY-MM-DD') IS NULL"
    elif format_type == "NUMERIC_RANGE":
        try:
            parts = (value or "").split("-")
            low, high = float(parts[0].strip()), float(parts[1].strip())
            return f"({field} < {low} OR {field} > {high})"
        except Exception:
            return "1=0"
    elif format_type == "LENGTH":
        try:
            return f"LENGTH(TRIM(TO_CHAR({field}))) != {int(value)}"
        except Exception:
            return "1=0"
    return "1=0"


def _build_mongo_format_filter(field, format_type, pattern, value) -> dict:
    format_type = (format_type or "").upper()
    if format_type == "REGEX":
        return {field: {"$not": {"$regex": pattern}}}
    elif format_type == "DATE_FORMAT":
        date_regex = FORMAT_DATE_REGEX_MAP.get((value or "").upper(), "")
        if date_regex:
            return {field: {"$not": {"$regex": date_regex}}}
        return {field: {"$not": {"$type": "date"}}}
    elif format_type == "NUMERIC_RANGE":
        try:
            parts = (value or "").split("-")
            low, high = float(parts[0].strip()), float(parts[1].strip())
            return {"$or": [{field: {"$lt": low}}, {field: {"$gt": high}}]}
        except Exception:
            return {}
    elif format_type == "LENGTH":
        try:
            return {
                "$expr": {
                    "$ne": [
                        {"$strLenCP": {"$toString": f"${field}"}},
                        int(value)
                    ]
                }
            }
        except Exception:
            return {}
    return {}


def _dedup_fields(fields: list) -> list:
    seen = set()
    result = []
    for f in fields:
        if f.lower() not in seen:
            seen.add(f.lower())
            result.append(f)
    return result


def get_format_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                    sourceQueryType, check_fields, data_filters=None):
    """
    Fetches only records that FAIL format rules.
    Query can be a string or list of strings — list joined with space.
    """
    sourceEnvKey = sourceEnvKey.upper()
    results = {}
    data_filters = data_filters or {}
    max_records = int(missing)

    # ── Normalise query — list → string joined with space
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)

    for field_def in check_fields:
        field = field_def.get("SourceField", "")
        format_type = field_def.get("FormatType", "")
        pattern = field_def.get("Pattern", "")
        value = field_def.get("Value", "")
        result_fields = field_def.get("ResultFields", [])

        if isinstance(result_fields, str):
            result_fields = [result_fields]
        else:
            result_fields = list(result_fields)

        if field and field.lower() not in [rf.lower() for rf in result_fields]:
            result_fields = [field] + result_fields

        result_fields = _dedup_fields(result_fields)

        if not field:
            continue

        try:
            # ── Oracle ────────────────────────────────────────────────────────
            if sourceType and sourceType.upper() == "ORACLE":
                source_connection = OracleConnection(sourceEnvKey)

                if "select " in sourceQuery.lower():
                    table_ref = f"({sourceQuery})"
                elif "." in sourceQuery:
                    table_ref = sourceQuery
                else:
                    table_ref = f"{sourceDB}.{sourceQuery}"

                select_cols = ", ".join(result_fields)
                format_where = _build_oracle_format_filter(
                    field, format_type, pattern, value
                )

                conditions = []
                if data_filters:
                    conditions.append(data_filters)
                conditions.append(f"{field} IS NOT NULL")
                conditions.append(format_where)

                where_clause = ' AND '.join(conditions)

                sample_query = f"""
                    SELECT {select_cols}
                    FROM {table_ref}
                    WHERE {where_clause}
                    AND ROWNUM <= {max_records}
                """

                logger.info(
                    f"Oracle Format_Check query for '{field}' [{format_type}]: "
                    f"{sample_query.strip()}",
                    extra=console_text(f"Source [ORACLE] Format_Check Oracle Query is executed for "
                                       f"'{field}' [{format_type}] — full query in log file")
                )

                sample_data = source_connection.fetchAllDict(sample_query)

                if not sample_data:
                    results[field] = {"sample": [], "total_count": 0, "capped": False}
                    continue

                count_query = f"""
                    SELECT COUNT(*) AS FAIL_COUNT
                    FROM (
                        SELECT 1
                        FROM {table_ref}
                        WHERE {where_clause}
                        AND ROWNUM <= {COUNT_CAP}
                    )
                """

                logger.info(
                    f"Oracle Format_Check count query for '{field}' [{format_type}]: "
                    f"{count_query.strip()}",
                    extra=CONSOLE_HIDE
                )

                count_result = source_connection.fetchAllDict(count_query)
                raw_count = (
                    count_result[0].get("FAIL_COUNT", len(sample_data))
                    if count_result else len(sample_data)
                )
                capped = raw_count >= COUNT_CAP

                results[field] = {
                    "sample": sample_data,
                    "total_count": raw_count,
                    "capped": capped
                }

            # ── MongoDB ───────────────────────────────────────────────────────
            elif sourceType and sourceType.upper() == "MONGODB":
                if sourceQueryType.upper() in ("VIEW", "COLLECTION"):

                    format_filter = _build_mongo_format_filter(
                        field, format_type, pattern, value
                    )

                    if not format_filter:
                        logger.warning(
                            f"MongoDB Format_Check: empty filter for '{field}' — skipping."
                        )
                        results[field] = {"sample": [], "total_count": 0, "capped": False}
                        continue

                    and_conditions = []
                    if data_filters:
                        and_conditions.append(data_filters)
                    and_conditions.extend([
                        {field: {"$ne": None}},
                        {field: {"$ne": ""}},
                        {field: {"$exists": True}},
                        format_filter
                    ])

                    mongo_filter = {"$and": and_conditions}

                    projection = {}
                    for rf in result_fields:
                        projection[rf.split(".")[0]] = 1
                    projection["_id"] = 0

                    logger.info(
                        f"Source [MONGODB]: db.{sourceQuery}.find(\n"
                        f"{json.dumps(mongo_filter, indent=2, default=str)}\n)",
                        extra=console_text(f"Source [MONGODB] Mongo Query is executed on "
                                           f"{sourceQuery} — full query in log file")
                    )

                    source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)

                    sample_data = list(
                        source_view.getDataView(mongo_filter, projection).limit(max_records)
                    )

                    if not sample_data:
                        results[field] = {"sample": [], "total_count": 0, "capped": False}
                        continue

                    total_count = source_view.getDataCount(mongo_filter)
                    capped = total_count >= COUNT_CAP

                    results[field] = {
                        "sample": sample_data,
                        "total_count": total_count,
                        "capped": capped
                    }

        except Exception as e:
            logger.issue(
                f"Error fetching format check data for '{field}' ({sourceType}): {e}"
            )
            results[field] = None

    return results