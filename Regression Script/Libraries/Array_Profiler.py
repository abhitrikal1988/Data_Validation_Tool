"""
Array_Profiler.py
=================
One MongoDB collection, several Oracle tables.

A collection such as MCP.Contract keeps its child records in arrays
(DefaultPay[], AdditionalAttribute[], ContractConfigX.PricingSources[] and,
inside it, ListGrouper[] and PriorityOrder[]). The data warehouse stores each
array in its own table (CONTRACT_DEFAULT_PAY, CONTRACT_ADD_ATTR, ...).

This module:
  1. discover_levels()  — finds every "level" of a collection: the document
     itself and every array (arrays of objects, arrays of plain values,
     arrays inside arrays), with the fields at each level. It reads sampled
     documents and, when the collection has one, its $jsonSchema validator,
     so arrays that are empty in the sample are still found.
  2. match_table()      — decides which level an Oracle table holds, from the
     table name (abbreviations allowed: ADD_ATTR ~ AdditionalAttribute,
     LST ~ List) and from how many of its columns match the level's fields.
  3. map_columns()      — pairs each table column with a field (same name
     ignoring case/underscores, or the same words, abbreviations allowed) and
     picks the join keys: the document key (Contract_ID) plus the ID of each
     array on the way down (PricingSources_ID, ListGrouper_ID).
  4. build_child_testcase() — one SRC_TGT test case per child table whose
     MongoDB side is an aggregation that $unwinds the array path and projects
     the mapped fields, and whose Oracle side SELECTs the mapped columns.

Nothing here sends data values anywhere; only field names, counts and types
are kept.
"""

import copy
import difflib
import json
import logging
import re

logger = logging.getLogger(__name__)

# Bookkeeping sub-documents never tested, at any depth (Audit, Notes[].Audit).
IGNORED_SEGMENTS = {"audit"}
SAMPLE_SIZE = 1000
_DISTINCT_CAP = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────────────────────

def name_tokens(name) -> list:
    """'ContractConfigX.PricingSources_ID' → ['contract','config','x','pricing','sources','id']"""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if t]


def squash(name) -> str:
    return "".join(name_tokens(name))


def _is_abbrev(short: str, long: str) -> bool:
    """ADD ~ ADDITIONAL, ATTR ~ ATTRIBUTE, LST ~ LIST, PCT ~ PERCENT."""
    if len(short) < 2 or len(short) >= len(long) or short[0] != long[0]:
        return False
    it = iter(long)
    return all(ch in it for ch in short)


def tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.rstrip("s") == b.rstrip("s") and len(a.rstrip("s")) >= 2:
        return True                                   # SOURCE ~ SOURCES
    if _is_abbrev(a, b) or _is_abbrev(b, a):
        return True
    if min(len(a), len(b)) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True                                   # ATRRIBUTE ~ ATTRIBUTE (typo)
    return False


def _seq_match(xs: list, ys: list) -> bool:
    """Same words in the same order (abbreviations allowed)."""
    return len(xs) == len(ys) and all(tokens_match(x, y) for x, y in zip(xs, ys))


def is_ignored(path: str) -> bool:
    return any(seg.lower() in IGNORED_SEGMENTS for seg in str(path or "").split("."))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Levels
# ─────────────────────────────────────────────────────────────────────────────

def _new_level(path, unwind, parent):
    return {"path": path, "unwind": list(unwind), "parent": parent, "scalar": None,
            "elements": 0, "fields": {}, "required": set()}


def _field(level, name):
    return level["fields"].setdefault(name, {"present": 0, "values": set(), "types": set()})


def _type_name(v) -> str:
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "DECIMAL"
    if hasattr(v, "isoformat"):
        return "DATETIME"
    return "STRING"


def _record(level, name, value):
    f = _field(level, name)
    if value is None or value == "":
        return
    f["present"] += 1
    f["types"].add(_type_name(value))
    if len(f["values"]) < _DISTINCT_CAP:
        try:
            f["values"].add(value if not isinstance(value, (dict, list)) else str(value))
        except TypeError:
            f["values"].add(str(value))


def _walk_doc(value, path, level_path, levels):
    level = levels[level_path]
    if isinstance(value, dict):
        for key, sub in value.items():
            if level_path == "" and path == "" and key == "_id":
                continue
            child = f"{path}.{key}" if path else key
            if is_ignored(child):
                continue
            _walk_doc(sub, child, level_path, levels)
    elif isinstance(value, list):
        arr = levels.get(path)
        if arr is None:
            arr = levels[path] = _new_level(path, level["unwind"] + [path], level_path)
        arr["elements"] += len(value)
        for item in value:
            if isinstance(item, dict):
                arr["scalar"] = False
                _walk_doc(item, path, path, levels)
            elif isinstance(item, list):
                continue                              # arrays of arrays: not supported
            else:
                if arr["scalar"] is None:
                    arr["scalar"] = True
                _record(arr, path, item)
    else:
        _record(level, path, value)


def _schema_types(node) -> list:
    t = node.get("bsonType") or node.get("type") or []
    return [t] if isinstance(t, str) else list(t)


def _walk_schema(node, path, level_path, levels):
    """Adds fields / arrays declared in $jsonSchema (no counts)."""
    if not isinstance(node, dict):
        return
    types = _schema_types(node)
    level = levels[level_path]
    if "array" in types and isinstance(node.get("items"), dict):
        arr = levels.get(path)
        if arr is None:
            arr = levels[path] = _new_level(path, level["unwind"] + [path], level_path)
        items = node["items"]
        item_types = _schema_types(items)
        if "object" in item_types or "properties" in items:
            arr["scalar"] = False
            for req in items.get("required", []) or []:
                arr["required"].add(f"{path}.{req}")
            for key, sub in (items.get("properties") or {}).items():
                child = f"{path}.{key}"
                if not is_ignored(child):
                    _walk_schema(sub, child, path, levels)
        else:
            if arr["scalar"] is None:
                arr["scalar"] = True
            _field(arr, path)
        return
    if "object" in types or "properties" in node:
        for req in node.get("required", []) or []:
            level["required"].add(f"{path}.{req}" if path else req)
        for key, sub in (node.get("properties") or {}).items():
            if level_path == "" and path == "" and key == "_id":
                continue
            child = f"{path}.{key}" if path else key
            if not is_ignored(child):
                _walk_schema(sub, child, level_path, levels)
        return
    if path:
        _field(level, path)
        for t in types:
            if t != "null":
                level["fields"][path]["types"].add(
                    {"number": "DECIMAL", "int": "INTEGER", "long": "INTEGER", "double": "DECIMAL",
                     "bool": "BOOLEAN", "date": "DATETIME"}.get(t, "STRING"))


def discover_levels(docs: list, schema: dict = None) -> dict:
    """
    {path: level}. path "" is the document itself; every other path is an
    array, e.g. "ContractConfigX.PricingSources.ListGrouper". Each level has:
      unwind  — array paths to $unwind, outermost first
      parent  — path of the enclosing level
      scalar  — True for an array of plain values (PriorityOrder: ["A","B"])
      fields  — {full dotted path: {present, values, types}}
      elements— number of elements seen in the sample
    """
    levels = {"": _new_level("", [], None)}
    docs = list(docs or [])
    levels[""]["elements"] = len(docs)
    for doc in docs:
        _walk_doc(doc, "", "", levels)
    if isinstance(schema, dict):
        if "$jsonSchema" in schema:
            schema = schema["$jsonSchema"]
        _walk_schema(schema, "", "", levels)
    # A path that was seen both as a sub-document field and as an array is an array.
    for path in list(levels):
        if path:
            parent = levels[levels[path]["parent"]]
            parent["fields"].pop(path, None)
    return levels


def _unique_in_sample(level: dict, field: str) -> bool:
    """True when every sampled element has the field and no value repeats,
    e.g. BNCContractDetails_ID; False for Contract_ID when the same contract
    appears in two Details elements (two date spans)."""
    info = level["fields"].get(field) or {}
    present = info.get("present", 0)
    return (present > 0 and present == level.get("elements", 0)
            and len(info.get("values", ())) == min(present, _DISTINCT_CAP))


def _repeats_in_sample(level: dict, field: str) -> bool:
    info = level["fields"].get(field) or {}
    present = info.get("present", 0)
    return 0 < len(info.get("values", ())) < min(present, _DISTINCT_CAP)


def level_key_candidates(level: dict) -> list:
    """
    Fields that can identify one element (…_ID / …ID), best first:
      1. unique across the sampled elements (BNCContractDetails_ID, not a
         Contract_ID that repeats within the same document)
      2. required in the schema
      3. named after the array (DefaultPay_ID, BNCContractDetails_ID for Details)
      4. shorter name
    """
    path = level["path"]
    if level["scalar"]:
        return [path]
    own = [f for f in level["fields"] if _relative(f, path).count(".") == 0]
    ids = [f for f in own if name_tokens(_relative(f, path))[-1:] == ["id"]]
    last = squash(path.split(".")[-1]) if path else ""
    names = [n for n in {last, last.rstrip("s")} if n]
    def rank(f):
        rel = squash(_relative(f, path))
        return (not _unique_in_sample(level, f),
                f not in level["required"],
                not any(rel.startswith(n) or n in rel for n in names),
                len(rel))
    return sorted(ids, key=rank)


def level_key(level: dict):
    """The field that identifies one element (see level_key_candidates)."""
    candidates = level_key_candidates(level)
    return candidates[0] if candidates else None


def _relative(field: str, level_path: str) -> str:
    if level_path and field.startswith(level_path + "."):
        return field[len(level_path) + 1:]
    return field


def level_chain(levels: dict, path: str) -> list:
    """[level, parent, grandparent, ..., root]"""
    chain = []
    while path is not None:
        chain.append(levels[path])
        path = levels[path]["parent"]
    return chain


def describe_level(path: str, collection: str) -> str:
    return f"{collection}" if not path else f"{collection}.{path}[]"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Table → level
# ─────────────────────────────────────────────────────────────────────────────

def _table_tokens(table: str, collection: str) -> list:
    """CONTRACT_PRICING_SOURCE_LST_GROUPER for collection Contract → pricing source lst grouper."""
    tokens = name_tokens(table.split(".")[-1])
    coll = name_tokens(collection)
    while tokens and coll and any(tokens_match(tokens[0], c) for c in coll):
        tokens.pop(0)
    return tokens


def _name_score(table_tokens: list, path: str) -> float:
    if not path:
        return 3.0 if not table_tokens else 0.0
    if not table_tokens:
        return 0.0
    path_tokens = name_tokens(path)
    last = name_tokens(path.split(".")[-1])
    hit_t = sum(any(tokens_match(t, p) for p in path_tokens) for t in table_tokens) / len(table_tokens)
    hit_last = sum(any(tokens_match(l, t) for t in table_tokens) for l in last) / len(last)
    return 2 * hit_t + hit_last


def match_table(table: str, columns: list, levels: dict, collection: str,
                forced: str = None) -> tuple:
    """
    Returns (level_path or None, explanation). `forced` (from --map) wins.
    """
    if forced is not None:
        forced = forced.strip().rstrip("[]")
        if forced.upper() in ("SKIP", "-"):
            return None, "skipped with --map"
        if forced.upper() in ("", "ROOT", "."):
            return "", "set with --map"
        if forced in levels:
            return forced, "set with --map"
        lower = {p.lower(): p for p in levels}
        if forced.lower() in lower:
            return lower[forced.lower()], "set with --map"
        return None, f"--map names '{forced}', which is not an array in {collection}"
    t_tokens = _table_tokens(table, collection)
    scored = []
    for path, level in levels.items():
        ns = _name_score(t_tokens, path)
        pairs, _ = map_columns(columns, levels, path)
        own = [p for p in pairs if p["level"] == path]
        overlap = len(own) / max(1, len(columns))
        scored.append((ns + overlap, ns, overlap, path))
    scored.sort(reverse=True)
    best = scored[0] if scored else None
    if not best or (best[1] < 2.0 and best[2] < 0.5):
        return None, "no array of the collection matches the table name or columns"
    runner = scored[1] if len(scored) > 1 else None
    note = f"name score {best[1]:.1f}, {best[2]:.0%} of columns match"
    if runner and best[0] - runner[0] < 0.3:
        note += f" — close to {describe_level(runner[3], collection)}, check it"
    return best[3], note


# ─────────────────────────────────────────────────────────────────────────────
# 3. Columns ↔ fields
# ─────────────────────────────────────────────────────────────────────────────

def map_columns(columns: list, levels: dict, path: str) -> tuple:
    """
    Pairs Oracle columns with fields of the level and its parents.
    Returns ([{"field", "column", "level"}], [unmatched columns]).
    Preference: same words for the whole relative name > same last name;
    own level > parent > … > root; exact words > abbreviations.
    """
    chain = level_chain(levels, path)
    candidates = []                      # (depth, field, level_path, rel_tokens, last_tokens)
    for depth, level in enumerate(chain):
        for field in level["fields"]:
            rel = _relative(field, level["path"])
            rel_tokens = name_tokens(rel) or name_tokens(field)
            last_tokens = name_tokens(rel.split(".")[-1]) or rel_tokens
            candidates.append((depth, field, level["path"], rel_tokens, last_tokens))
        if level["scalar"]:
            # The plain value of a scalar array is matched by the array's name.
            candidates.append((depth, level["path"], level["path"],
                               name_tokens(level["path"].split(".")[-1]),
                               name_tokens(level["path"].split(".")[-1])))
    used, pairs, unmatched = set(), [], []
    for col in columns:
        c_tokens = name_tokens(col)
        best = None
        for depth, field, lpath, rel_tokens, last_tokens in candidates:
            if field in used:
                continue
            if rel_tokens == c_tokens:
                tier = 0
            elif last_tokens == c_tokens:
                tier = 1
            elif _seq_match(c_tokens, rel_tokens):
                tier = 2
            elif _seq_match(c_tokens, last_tokens):
                tier = 3
            elif squash(col) == "".join(rel_tokens):
                tier = 0
            else:
                continue
            rank = (tier, depth)
            if best is None or rank < best[0]:
                best = (rank, field, lpath)
        if best:
            used.add(best[1])
            pairs.append({"field": best[1], "column": col, "level": best[2]})
        else:
            unmatched.append(col)
    return pairs, unmatched


def join_and_compare(pairs: list, levels: dict, path: str, flattened=()) -> tuple:
    """Join keys: the key of every level from the root down to this one.
    A flattened inner array with no ID field (ValueRange[]: FromVal, ToVal,
    IncludeExclude) is identified by all of its mapped fields together."""
    by_field = {p["field"]: p for p in pairs}
    joins = []
    for level in reversed(level_chain(levels, path)):
        # Best key that has a matching target column
        key = next((k for k in level_key_candidates(level) if k in by_field), None)
        if not key and level["path"] in flattened:
            joins.extend(p for p in pairs if p["level"] == level["path"])
            continue
        if key:
            joins.append(by_field[key])
            if level["path"] and _repeats_in_sample(level, key):
                logger.warning(
                    f"Join key {key} repeats within the sampled "
                    f"{level['path']} elements and no unique ID column was "
                    f"matched in the target — check JoinFields for this test case.")
    if not joins:
        return [], pairs
    join_fields = {j["field"] for j in joins}
    return joins, [p for p in pairs if p["field"] not in join_fields]


def flatten_level(columns: list, levels: dict, path: str) -> tuple:
    """
    A table can hold an array together with the arrays inside it, one row per
    inner element: PA_GROUP_DOMAINS keeps Domains[] and Domains[].ValueRange[]
    (FROM_VAL, TO_VAL, INCLUDE_EXCLUDE) in the same row. When the table's
    columns cover most fields of an inner array, the test case unwinds down to
    it. Returns (path to unwind to, [inner array paths flattened]).
    """
    if not path:
        return path, []                     # the document table is never flattened
    base, _ = map_columns(columns, levels, path)
    best = (len(base), path, [])
    for inner in levels:
        if inner == path or not inner.startswith(path + "."):
            continue
        below = []
        for lvl in level_chain(levels, inner):
            if lvl["path"] == path:
                break
            below.append(lvl["path"])
        pairs, _ = map_columns(columns, levels, inner)
        new = [p for p in pairs if p["level"] in below]
        n_fields = sum(len(levels[b]["fields"]) for b in below) or 1
        if (len(new) >= 2 and len(new) >= 0.5 * n_fields
                and all(any(p["level"] == b for p in new) for b in below)
                and len(pairs) > best[0]):
            best = (len(pairs), inner, list(reversed(below)))
    return best[1], best[2]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Test case
# ─────────────────────────────────────────────────────────────────────────────

def _ident(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", name) else '"' + name + '"'


def build_child_testcase(table: str, columns: list, levels: dict, path: str,
                         source_def: dict, oracle_env: str, collection: str,
                         flattened=()) -> tuple:
    """
    Returns (test_case, info) for one child table. info has the mapping for
    the run log: joins, compared, not_compared. `flattened` lists inner
    arrays held in the same rows (see flatten_level); they are unwound
    keeping elements whose inner array is empty, as the table does.
    """
    pairs, unmatched = map_columns(columns, levels, path)
    joins, compares = join_and_compare(pairs, levels, path, flattened)
    fields = [p["field"] for p in joins + compares]
    projection = {"_id": 0}
    for f in fields:
        if not any(f.startswith(other + ".") for other in fields if other != f):
            projection[f] = 1
    pipeline = [{"$unwind": {"path": f"${p}", "preserveNullAndEmptyArrays": True}}
                if p in flattened else {"$unwind": f"${p}"}
                for p in levels[path]["unwind"]]
    pipeline.append({"$project": projection})
    cols = [p["column"] for p in joins + compares] or ["*"]
    owner = table.split(".")[0] if "." in table else ""
    case = {
        "TestCaseNumber":  None,
        "Module":          "",
        "Stage":           "",
        "ValidationType":  "SRC_TGT",
        "TestDescription": f"{table.split('.')[-1]} matches "
                           f"{describe_level(levels[flattened[0]]['parent'] if flattened else path, collection)}"
                           + (f" with {', '.join(f + '[]' for f in flattened)} flattened" if flattened else ""),
        "Viceversa":       True,
        "Source": {
            "Type":        "MONGODB",
            "Environment": source_def.get("Environment"),
            "DB":          source_def.get("DB"),
            "Collection":  source_def.get("Query") or source_def.get("Collection"),
            "QueryType":   "PIPELINE",
            "Query":       json.dumps(pipeline),
        },
        "Target": {
            "Type":        "ORACLE",
            "Environment": oracle_env,
            "DB":          owner,
            "QueryType":   "QUERY",
            "Query":       f"SELECT {', '.join(_ident(c) for c in cols)} FROM {table}",
        },
        "JoinFields":    [{"SourceField": p["field"], "TargetField": p["column"]} for p in joins],
        "CompareFields": [{"SourceField": p["field"], "TargetField": p["column"]} for p in compares],
        "DataFilters":   [],
        "ExpectedResult": "Source and target should match: no missing records and no mismatched values",
    }
    info = {"joins": joins, "compares": compares, "not_compared": unmatched}
    return case, info


# ─────────────────────────────────────────────────────────────────────────────
# Reading the collection
# ─────────────────────────────────────────────────────────────────────────────

def read_collection(source_def: dict, sample_size: int = SAMPLE_SIZE) -> tuple:
    """(sample documents, $jsonSchema or None) from the source collection."""
    from .MongoDB import Collection
    coll = Collection(source_def.get("DB"), source_def.get("Query") or source_def.get("Collection"),
                      str(source_def.get("Environment") or "").upper())
    try:
        cursor = coll.getDataSort({}, [("_id", 1)])
        docs = list(cursor.limit(sample_size))
    except Exception:
        docs = list(coll.getDataLimit({}, sample_size))
    schema = None
    try:
        options = coll.DbCollection.options() or {}
        schema = (options.get("validator") or {}).get("$jsonSchema")
    except Exception as exc:
        logger.debug(f"No $jsonSchema read: {exc}")
    return docs, schema