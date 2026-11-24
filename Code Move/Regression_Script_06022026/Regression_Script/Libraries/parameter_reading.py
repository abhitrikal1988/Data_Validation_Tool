import argparse
import logging
from datetime import datetime, timezone
from .Read_config import config_read 
cr = config_read()
config_max_workers = int(cr.max_threads("Max_Threads"))
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Existing: CLI argument parsing — exact copy of original, only -f/--filters added
# ─────────────────────────────────────────────────────────────────────────────
def argparse_():
    parser = argparse.ArgumentParser(
        prog="Data Validation Process",
        description="This process will validate the Data at multiple stages",
        epilog="Nirvana health",
        formatter_class=argparse.RawTextHelpFormatter
    )
    # Module Name
    parser.add_argument(
        "-m", "--module",
        type=str,
        help="""Module name  - (Required)  
                M3P""",
        required=True,
    )
    # Stage Type
    parser.add_argument(
        "-s", "--stage",
        type=str,
        help="""Validation Stage Type
            Initial Testing """,
        default=None,
    )
    # Cycle Type
    parser.add_argument(
        "-c", "--cycle",
        type=str,
        help="""Validation Cycle Types  - (Optional)  
                M3P Enrollment """,
        default=None
    )
    # Test case Order
    parser.add_argument(
        "-t", "--testcasenumber",
        type=str,
        help=f"""To the specific order
                0 = all
                0-N = run for the specific order
        """,
        default="0"
    )
    # Environment
    parser.add_argument(
        "-e", "--environment",
        type=str,
        help=f"""Environment where Data_Regression_Set collection located
                   MCPQA1,EHQA,IMPL
           """,
        default="MONGODB_MCPQA1"
    )
    # Runtime DataFilters — NEW, optional, does not affect existing jobs
    parser.add_argument(
        "-f", "--filters",
        type=str,
        help="""Runtime filter overrides for DataFilters in test cases (Optional)
                Separator  : Use ; to separate multiple filters
                BETWEEN    : Use , to separate start and end values
                IN         : Use , to separate multiple values
                Format     : 'FIELD=value;FIELD2=value2'
                Examples:
                  Single        : -f "ENCOUNTER_DT=2026-03-01"
                  Multiple      : -f "ENCOUNTER_DT=2026-03-01;PLAN_ID=PBM001"
                  BETWEEN       : -f "ENCOUNTER_DT=2025-01-01,2025-12-31"
                  IN            : -f "CLAIM_STATUS=A,P,C"
                  Skip all      : -f "SKIP"
                If not provided DefaultValue from test case is used.
                If DataFilters not defined in test case program runs as-is.""",
        default=None
    )
    #RunType
    parser.add_argument(
        "-rt", "--runtype",
        choices = ['s','p'],
        type=str,
        help=f"""Execution Mode:
                's': seqeuntial,
                'p': parallel
                (default: 'p')
        """,
        default='p'
    )
 
    #ThreadCount
    parser.add_argument(
        "-n", "--threads",
        type=int,
        help=f"""Number of threads for parallel execution (Max(default)={config_max_workers})
        """,
        default=config_max_workers
    )
 
    return parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# Existing: unchanged
# ─────────────────────────────────────────────────────────────────────────────
def parse_multi_value(value):
    if "," in value:
        return {"$in": [v.strip() for v in value.split(",")]}
    return value.strip()

def resolve_parameters(query: str, parameters: list) -> str:
    """
    Substitutes :param_name placeholders in a query string using the
    DefaultValue defined in the Parameters array of the test case document.
    DefaultValue in MongoDB is never modified — substitution is in-memory only.
    """
    if not parameters or not query:
        return query
    resolved_query = query
    for param in parameters:
        name    = param.get("Name", "")
        default = param.get("DefaultValue")
        if not name:
            continue
        value = default
        if value is None:
            logger.warning(
                f"Parameter '{name}' has no DefaultValue — "
                f"placeholder ':{name}' left unresolved."
            )
            continue
        placeholder    = f":{name}"
        resolved_query = resolved_query.replace(placeholder, str(value))
        logger.info(f"Parameter resolved: {placeholder} = '{value}'")
    return resolved_query

# ─────────────────────────────────────────────────────────────────────────────
# New: Runtime filter parsing
# ─────────────────────────────────────────────────────────────────────────────
def parse_runtime_filters(filters_str: str) -> dict:
    """
    Parses -f/--filters CLI string into a dict for in-memory use.
    DefaultValue in MongoDB test case documents is never modified.
    Separator rules:
      ; separates multiple filters   e.g. "ENCOUNTER_DT=2026-03-01;PLAN_ID=PBM001"
      , within a value is kept as-is e.g. "ENCOUNTER_DT=2025-01-01,2025-12-31" (BETWEEN)
                                      or  "CLAIM_STATUS=A,P,C" (IN)
    Args:
        filters_str : CLI string e.g. "ENCOUNTER_DT=2026-03-01;PLAN_ID=PBM001"
                      or "SKIP" to bypass all DataFilters for this run.
    Returns:
        {"__SKIP__": True} if "SKIP" passed
        {"ENCOUNTER_DT": "2026-03-01", "PLAN_ID": "PBM001"} otherwise
        {} if None or empty
    """
    if not filters_str:
        return {}
    if filters_str.strip().upper() == "SKIP":
        return {"__SKIP__": True}
    result = {}
    for pair in filters_str.split(";"):     # ← ; separates multiple filters
        pair = pair.strip()
        if "=" in pair:
            key, val = pair.split("=", 1)   # ← split on first = only
            result[key.strip()] = val.strip()
    return result

# ─────────────────────────────────────────────────────────────────────────────
# New: Date value helpers
# ─────────────────────────────────────────────────────────────────────────────
def _detect_and_parse_date(value: str):
    """
    Detects date format and parses into a datetime object.
    Handles multiple input formats automatically.
    Returns:
        (datetime, has_time) — datetime with UTC tzinfo, bool for time component
        (None, False) if not a recognizable date
    """
    formats = [
        ("%Y-%m-%dT%H:%M:%S.%f", True),
        ("%Y-%m-%dT%H:%M:%S",    True),
        ("%Y-%m-%d",             False),
        ("%m/%d/%Y %H:%M:%S",    True),
        ("%m/%d/%Y",             False),
        ("%d/%m/%Y",             False),
        ("%Y/%m/%d",             False),
        ("%m-%d-%Y",             False),
        ("%d-%b-%Y",             False),   # 06-APR-2026 Oracle DD-MON-YYYY
        ("%d-%b-%y",             False),   # 06-APR-26  Oracle DD-MON-YY
    ]
    for fmt, has_time in formats:
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc), has_time
        except ValueError:
            continue
    return None, False

def _to_oracle_value(value: str) -> str:
    """
    Converts a filter value to Oracle SQL representation.
    Date strings       → TO_DATE() or TO_TIMESTAMP()
    Oracle functions   → unquoted
    Numbers            → unquoted
    Strings            → quoted
    """
    oracle_functions = ["SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE", "CURRENT_TIMESTAMP"]
    if value.strip().upper() in oracle_functions:
        return value.strip().upper()
    dt, has_time = _detect_and_parse_date(value)
    if dt:
        if has_time:
            return (f"TO_TIMESTAMP('{dt.strftime('%Y-%m-%d %H:%M:%S')}', "
                    f"'YYYY-MM-DD HH24:MI:SS')")
        return f"TO_DATE('{dt.strftime('%Y-%m-%d')}', 'YYYY-MM-DD')"
    try:
        float(value)
        return value.strip()
    except ValueError:
        pass
    return f"'{value}'"

def _to_mongo_value(value: str):
    """
    Converts a filter value to MongoDB Python representation.
    Date strings → datetime object with UTC tzinfo
    Numbers      → int or float
    Strings      → string as-is
    """
    dt, _ = _detect_and_parse_date(value)
    if dt:
        return dt
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        pass
    return value

# ─────────────────────────────────────────────────────────────────────────────
# New: DataFilters resolution
# ─────────────────────────────────────────────────────────────────────────────
def _build_oracle_filter_clause(source_filters: list) -> str:
    """
    Builds Oracle WHERE clause string from resolved DataFilters.
    Returns AND-joined conditions or empty string if no filters.
    """
    conditions = []
    for f in source_filters:
        field    = f.get("_clause_field", "")
        operator = (f.get("Operator", "=") or "=").upper()
        value    = f.get("_resolved_value", "")
        if not field or not value:
            continue
        if operator == "BETWEEN":
            parts = [v.strip() for v in value.split(",")]
            if len(parts) == 2:
                conditions.append(
                    f"{field} BETWEEN {_to_oracle_value(parts[0])} "
                    f"AND {_to_oracle_value(parts[1])}"
                )
        elif operator == "IN":
            vals = ", ".join(_to_oracle_value(v.strip()) for v in value.split(","))
            conditions.append(f"{field} IN ({vals})")
        elif operator == "NOT IN":
            vals = ", ".join(_to_oracle_value(v.strip()) for v in value.split(","))
            conditions.append(f"{field} NOT IN ({vals})")
        elif operator == "LIKE":
            conditions.append(f"{field} LIKE '{value}'")
        elif operator in ("=", "!=", ">", ">=", "<", "<="):
            conditions.append(f"{field} {operator} {_to_oracle_value(value)}")
        else:
            logger.warning(
                f"Unknown operator '{operator}' for Oracle field '{field}' — skipping."
            )
    return " AND ".join(conditions)

def _build_mongo_filter_dict(target_filters: list) -> dict:
    """
    Builds MongoDB filter dict from resolved DataFilters.
    Returns filter dict or empty dict if no filters.
    """
    mongo_filter = {}
    for f in target_filters:
        field    = f.get("_clause_field", "")
        operator = (f.get("Operator", "=") or "=").upper()
        value    = f.get("_resolved_value", "")
        if not field or not value:
            continue
        if operator == "BETWEEN":
            parts = [v.strip() for v in value.split(",")]
            if len(parts) == 2:
                mongo_filter[field] = {
                    "$gte": _to_mongo_value(parts[0]),
                    "$lte": _to_mongo_value(parts[1])
                }
        elif operator == "IN":
            mongo_filter[field] = {
                "$in": [_to_mongo_value(v.strip()) for v in value.split(",")]
            }
        elif operator == "NOT IN":
            mongo_filter[field] = {
                "$nin": [_to_mongo_value(v.strip()) for v in value.split(",")]
            }
        elif operator == "LIKE":
            mongo_filter[field] = {"$regex": value}
        elif operator == "=":
            mongo_filter[field] = _to_mongo_value(value)
        elif operator == "!=":
            mongo_filter[field] = {"$ne": _to_mongo_value(value)}
        elif operator == ">":
            mongo_filter[field] = {"$gt": _to_mongo_value(value)}
        elif operator == ">=":
            mongo_filter[field] = {"$gte": _to_mongo_value(value)}
        elif operator == "<":
            mongo_filter[field] = {"$lt": _to_mongo_value(value)}
        elif operator == "<=":
            mongo_filter[field] = {"$lte": _to_mongo_value(value)}
        else:
            logger.warning(
                f"Unknown operator '{operator}' for MongoDB field '{field}' — skipping."
            )
    return mongo_filter

def resolve_data_filters(data_filters: list, source_type: str,
                          runtime_filters: dict = None,
                          is_target: bool = False):        # ← added is_target
    """
    Resolves DataFilters array into platform-specific filter representation.
    Priority per filter (in-memory only — DefaultValue in MongoDB never modified):
      1. CLI -f/--filters runtime value  e.g. -f "ENCOUNTER_DT=2026-03-01"
      2. DefaultValue from test case document
      3. Skip filter if both are empty
    Args:
        data_filters    : DataFilters array from test case document.
        source_type     : "ORACLE" or "MONGODB"
        runtime_filters : Parsed -f/--filters dict from parse_runtime_filters().
        is_target       : If True, prefer TargetField for field name resolution.
    Returns:
        ORACLE  : SQL WHERE clause string or empty string
        MONGODB : filter dict or empty dict
    """
    source_type     = (source_type or "").upper()
    runtime_filters = runtime_filters or {}
    # ── SKIP flag — bypass all DataFilters for this run ──────────────────────
    if runtime_filters.get("__SKIP__"):
        logger.info("DataFilters skipped for this run — -f SKIP was passed.")
        return "" if source_type == "ORACLE" else {}
    # ── No DataFilters in test case — run as-is ───────────────────────────────
    if not data_filters:
        return "" if source_type == "ORACLE" else {}
    # ── Resolve value per filter ──────────────────────────────────────────────
    resolved_filters = []
    for f in data_filters:
        # SourceFilter applies only to source; TargetFilter only to target.
        # Silently skip filters that don't apply to the requested side.
        if is_target:
            clause_field = f.get("TargetField", "")
        else:
            clause_field = f.get("SourceField", "")
 
        if not clause_field:
            continue   # filter not applicable to this side
 
        # Runtime value lookup uses the filter's own field name
        default        = f.get("DefaultValue", "")
        resolved_value = runtime_filters.get(clause_field, "") or default
 
        if resolved_value in (None, ""):
            logger.info(
                f"DataFilter '{clause_field}': "
                f"no value and no DefaultValue — skipping filter."
            )
            continue
 
        resolved_filters.append({
            **f,
            "_clause_field"   : clause_field,   # used by _build_oracle_filter_clause
                                                 # and _build_mongo_filter_dict
            "_resolved_value" : resolved_value
        })
        # Fix — single log only here, not duplicated in _apply_data_filter
        # logger.info(
        #     f"DataFilter '{clause_field}': resolved value = '{resolved_value}' "
        #     f"({'RuntimeFilter' if runtime_filters.get(rt_key) else 'DefaultValue'})"
        # )
    if not resolved_filters:
        return "" if source_type == "ORACLE" else {}
    if source_type == "ORACLE":
        return _build_oracle_filter_clause(resolved_filters)
    elif source_type == "MONGODB":
        return _build_mongo_filter_dict(resolved_filters)
    return "" if source_type == "ORACLE" else {}