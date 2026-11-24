import os
import pdb
import time
import os
from concurrent.futures import ProcessPoolExecutor
import sys
import re
import pandas as pd
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo.cursor import Cursor

scripts_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
project_path = os.path.abspath(os.path.join(scripts_path, '..'))
sys.path.append(scripts_path)
sys.path.append(project_path)

from Libraries.MongoDB import Collection
from Libraries.Comparison_vice_versa_with_log_info import (compare_data, compare_data_in_process, is_cursor_empty,
                                                           run_row_count_validation, inapproved_validation,
                                                           run_null_check,
                                                           run_duplicate_check, run_format_check)
from Libraries.Edi835_validator import (run_edi835_format_check, run_edi835_balance_check, run_edi835_data_check)
from Libraries.parameter_reading import (argparse_, parse_multi_value, resolve_parameters, parse_runtime_filters,
                                         resolve_data_filters, _to_oracle_value, _to_mongo_value, inject_file_filters,
                                         filter_value)
from Libraries.data_fetcher import _param_patterns
from Libraries.Regression_Log_Helper import *
from Libraries.multithreading import run_parallel
from Libraries.data_fetcher import (get_next_sequence_id, get_data, get_count,
                                    is_pipeline_query, normalize_query_text,
                                    substitute_pipeline_params, pipeline_display,
                                    console_text, query_object)

FILE_TYPES = {
    "CSV", "PIPE", "TSV", "FIXED_WIDTH", "EXCEL",
    "JSON", "XML", "HL7", "NCPDP_D0", "NCPDP_SCRIPT",
    "LOCAL_PATH", "UNC_PATH", "SFTP", "FTP",
    "S3", "AZURE_BLOB", "ORACLE_CLOB", "MONGODB_GRIDFS",
    "AUTO"
}
from Libraries.parameter_reading import *
from Libraries.send_email_report import *
import logging
from Libraries.Log_Config import Logger_
from Libraries.Read_config import config_read

cr = config_read()
test_data_environment = cr.test_data_environment("REGRESSION_DATA")
config_max_workers = int(cr.max_threads("Max_Threads"))
missing = cr.missing_count("No_Of_Records_Display")
logger = logging.getLogger(__name__)


class Regression_inputs:
    def __init__(self, module_name, stage, cycle, order, environment, runtime_filters=None,
                 view_rows=None, view_format="table"):
        self._module_name = module_name
        self._stage = stage
        self._cycle = cycle
        self._order = order
        self.env_label = environment
        self._runtime_filters = runtime_filters or {}
        # DataView: runtime -v value, or config default when not supplied
        self._view_rows = (view_rows if view_rows is not None
                           else cr.view_row_count())
        self._view_format = (view_format or "table").lower()
        db_name = cr.db_name("REGRESSION_DATA")
        rg_collection_data = cr.regression_collection_name("REGRESSION_DATA")
        log_collection = cr.log_collection_name("REGRESSION_DATA")
        self.ValidationList = Collection(db_name, rg_collection_data, environment)
        self.log_collection = Collection(db_name, log_collection, environment)
        self.ErrorCount = 0
        self.rules = []
        self.test_logs = []

    def testcase_details(self):
        try:
            sort = [("TestCaseNumber", 1)]

            # ── Build common filters (TC number, Cycle) ──────────────
            common = {}
            if self._cycle:
                common["Cycle"] = parse_multi_value(self._cycle)
            if self._order != "0":
                if isinstance(self._order, str) and "," in self._order:
                    common["TestCaseNumber"] = {
                        "$in": [int(v.strip()) for v in self._order.split(",")]
                    }
                else:
                    common["TestCaseNumber"] = int(self._order)

            modules = (
                [m.strip() for m in self._module_name.split(",") if m.strip()]
                if self._module_name else []
            )
            stage = parse_multi_value(self._stage) if self._stage else None

            if modules and stage:
                # ── Per-module query with stage fallback ──────────────
                # For each module: try with stage filter first.
                # If the module has no TCs for that stage, fall back
                # without stage so it still contributes its TC results.
                all_rules = []
                for module in modules:
                    q_with_stage = {"Module": module, "Stage": stage, **common}
                    results = list(self.ValidationList.getDataSort(q_with_stage, sort))
                    if results:
                        all_rules.extend(results)
                        logger.info(f"Module '{module}': {len(results)} TC(s) matched stage '{self._stage}'.")
                    else:
                        # Module has no TCs for this stage — run without stage filter
                        q_no_stage = {"Module": module, **common}
                        results = list(self.ValidationList.getDataSort(q_no_stage, sort))
                        all_rules.extend(results)
                        if results:
                            logger.info(
                                f"Module '{module}': no TCs for stage '{self._stage}' — "
                                f"running {len(results)} TC(s) without stage filter."
                            )
                if not all_rules:
                    logger.warning(
                        "No validation rules returned from MongoDB with the given filter. "
                        "Skipping execution."
                    )
                else:
                    # Sort combined results by TC number
                    self.rules.extend(
                        sorted(all_rules, key=lambda r: int(r.get("TestCaseNumber", 0)))
                    )
            else:
                # ── Original single query (no stage or single module) ─
                raw_query = {}
                if self._module_name:
                    raw_query["Module"] = parse_multi_value(self._module_name)
                if stage:
                    raw_query["Stage"] = stage
                raw_query.update(common)

                data = self.ValidationList.getDataSort(raw_query, sort)
                if isinstance(data, Cursor):
                    first_item = next(data.clone(), None)
                    if first_item is None:
                        logger.warning(
                            "No validation rules returned from MongoDB with the given filter. "
                            "Skipping execution."
                        )
                    else:
                        for e in data:
                            self.rules.append(e)

        except Exception as e:
            logger.exception("Exception in getting validation rules")
            raise

    def _apply_data_filter(self, query, db, source_type, data_filters_raw,
                           query_type=None, is_target=False, silent=False):
        """
        Applies DataFilters to a query based on QueryType.
        TABLE / VIEW / COLLECTION  → WHERE clause injection (Oracle) or $match filter (MongoDB)
        QUERY / PIPELINE / AGGREGATE / JAVA_SCRIPT → :placeholder substitution in query string
        Special case: query=None with MONGODB → $match filter only (used by EDI835 target path)
         """
        if not data_filters_raw or not source_type:
            return query, {}
        source_type = source_type.upper()
        query_type = (query_type or "").upper()
        # ── Fix #6 — query=None early-exit ──────────────────────────────────────────
        # File-type sources (UNC_PATH, SFTP, LOCAL_PATH, …) have no query string.
        # Any "in query" test with query=None raises TypeError. Return early:
        # - MONGODB  → return the $match filter so EDI835 target path works
        # - all others → return (None, {}) so the caller falls through to
        #   _fetch_file_data which handles DataFilters via _file_filters instead.
        if query is None:
            if source_type == "MONGODB":
                resolved = resolve_data_filters(
                    data_filters_raw, source_type, self._runtime_filters,
                    is_target=is_target
                )
                return None, resolved if resolved else {}
            return None, {}
        # ── Resolve filter field/value pairs for placeholder substitution only ───
        # For TABLE/VIEW/COLLECTION we delegate entirely to resolve_data_filters
        # to avoid double-resolution (Fix #3)
        filter_values = {}
        for f in data_filters_raw:

            # SourceFilter applies only to source; TargetFilter only to target.
            # A filter missing the applicable key is silently skipped for that side.
            if is_target:
                field = f.get("TargetField", "")
            else:
                field = f.get("SourceField", "")

            if not field:
                continue  # filter not applicable to this side

            # Runtime value: -f by SourceField or TargetField, else DefaultValue
            value = filter_value(f, self._runtime_filters, is_target)

            if value in (None, ""):
                continue

            filter_values[field] = value
            # logger.info(
            #     f"DataFilter '{field}': resolved value = '{value}' "
            #     f"({'RuntimeFilter' if self._runtime_filters.get(rt_key) else 'DefaultValue'})"
            #     )
        # A placeholder in a QUERY / PIPELINE with no value would reach the
        # database as-is and fail there with an unclear error — stop here.
        if query and query_type in ("QUERY", "PIPELINE", "AGGREGATE", "JAVA_SCRIPT"):
            side_key = "TargetField" if is_target else "SourceField"
            missing = []
            for f in data_filters_raw:
                field = f.get(side_key, "")
                if field and field not in filter_values and any(
                        p.search(query) for p in _param_patterns(field)):
                    missing.append(field)
            if missing:
                side = "Target" if is_target else "Source"
                raise ValueError(
                    f"{side} query needs a value for {', '.join(missing)} — pass it at run time "
                    f"with -f \"{';'.join(m + '=<value>' for m in missing)}\" or set DefaultValue "
                    f"in the test case DataFilters.")
        if not filter_values:
            return query, {}
        # ── Check if query contains :placeholders ────────────────────────────────
        has_placeholders = bool(query) and any(
            _param_patterns(field)[1].search(query)
            for field in filter_values
        )

        # ═════════════════════════════════════════════════════════════════════════
        # ORACLE
        # ═════════════════════════════════════════════════════════════════════════
        if source_type == "ORACLE":
            # QUERY type — substitute :placeholders inside the SQL
            if query_type == "QUERY":
                if has_placeholders:
                    modified_query = query
                    for field, value in filter_values.items():
                        oracle_val = _to_oracle_value(str(value))
                        # whole name only: :FILE_ID does not touch :FILE_ID_NH
                        modified_query = _param_patterns(field)[1].sub(
                            lambda _m, v=oracle_val: v, modified_query)
                    modified_query = modified_query.strip().rstrip(";").strip()
                    if not silent:
                        logger.info("Oracle DataFilter — placeholder substitution applied.")
                    return modified_query, {}
                else:
                    if not silent:
                        logger.warning(
                            f"Oracle DataFilter — QueryType is 'QUERY' but no :placeholders "
                            f"found. DataFilters not applied. "
                            f"Add {[':' + k for k in filter_values.keys()]} to the query."
                        )
                    return query, {}
            # TABLE / VIEW — delegate to resolve_data_filters (Fix #3 — no double resolution)
            elif query_type in ("TABLE", "VIEW"):
                resolved = resolve_data_filters(
                    data_filters_raw, source_type, self._runtime_filters,
                    is_target=is_target
                )
                if not resolved:
                    return query, {}
                q = query.strip().rstrip(";").strip()
                if "select " in q.lower():
                    modified_query = f"SELECT * FROM ({q}) WHERE {resolved}"
                elif "." in q:
                    modified_query = f"SELECT * FROM {q} WHERE {resolved}"
                else:
                    modified_query = f"SELECT * FROM {db}.{q} WHERE {resolved}"
                logger.info("Oracle DataFilter — WHERE clause injection applied.")
                return modified_query, {}
            else:
                logger.warning(
                    f"Oracle DataFilter — unhandled QueryType '{query_type}'. "
                    f"DataFilters not applied."
                )
                return query, {}
        # ═════════════════════════════════════════════════════════════════════════
        # MONGODB
        # ═════════════════════════════════════════════════════════════════════════
        elif source_type == "MONGODB":
            pipeline_types = ("JAVA_SCRIPT", "PIPELINE", "AGGREGATE")
            # Pipeline types — substitute :placeholders inside the pipeline string
            if query_type == "PIPELINE":
                # Aggregation: fill $$Name / :Name placeholders with typed values
                # (dates stay dates). Filters with no placeholder in the pipeline
                # are appended as a final $match stage.
                try:
                    modified_query, used = substitute_pipeline_params(query, filter_values)
                except ValueError:
                    return query, {}  # invalid pipeline — run_mongo_query reports it
                side_key = "TargetField" if is_target else "SourceField"
                leftover = [f for f in data_filters_raw if f.get(side_key) not in used]
                resolved = resolve_data_filters(
                    leftover, source_type, self._runtime_filters, is_target=is_target
                ) if leftover else {}
                if not silent:
                    if used:
                        logger.info(f"MongoDB DataFilter — pipeline parameters filled: "
                                    f"{', '.join(sorted(used))}.")
                    if resolved:
                        logger.info("MongoDB DataFilter — filters with no placeholder "
                                    "appended as a final $match stage.")
                return modified_query, (resolved or {})
            if query_type in pipeline_types:
                if has_placeholders:
                    modified_query = query
                    for field, value in filter_values.items():
                        modified_query = _param_patterns(field)[1].sub(
                            lambda _m, v=str(value): v, modified_query)
                    logger.info("MongoDB DataFilter — placeholder substitution applied.")
                    return modified_query, {}
                else:
                    logger.warning(
                        f"MongoDB DataFilter — QueryType is '{query_type}' but no "
                        f":placeholders found. DataFilters not applied. "
                        f"Add {[':' + k for k in filter_values.keys()]} to the pipeline."
                    )
                    return query, {}
            # VIEW / COLLECTION / TABLE — delegate to resolve_data_filters (Fix #3)
            elif query_type in ("VIEW", "COLLECTION", "TABLE"):
                resolved = resolve_data_filters(
                    data_filters_raw, source_type, self._runtime_filters,
                    is_target=is_target
                )
                if not resolved:
                    return query, {}
                # logger.info(f"MongoDB DataFilter — collection $match filter resolved: {resolved}")
                return query, resolved
            else:
                logger.warning(
                    f"MongoDB DataFilter — unhandled QueryType '{query_type}'. "
                    f"DataFilters not applied."
                )
                return query, {}
        # ═════════════════════════════════════════════════════════════════════════
        # UNHANDLED SOURCE TYPE
        # ═════════════════════════════════════════════════════════════════════════
        else:
            logger.warning(
                f"DataFilter — unhandled SourceType '{source_type}'. "
                f"DataFilters not applied."
            )
            return query, {}

    def _build_query_log(self, label, query_display, source_type,
                         mongo_filter, db, query_type, collection=None):
        """
        Builds exact query string for logging.
        query_display — original query preserved as multiline for readability.
        query_filtered — substituted/filtered query used for execution.
        """
        if not source_type:
            return None
        source_type = source_type.upper()
        query_type = (query_type or "").upper()
        if source_type == "MONGODB":
            pipeline_types = ("JAVA_SCRIPT", "PIPELINE", "AGGREGATE")
            if query_type == "PIPELINE":
                where = f" db.{collection}.aggregate" if collection else ""
                extra = (f"\n  + $match {mongo_filter}" if mongo_filter else "")
                return f"{label} [{source_type} {query_type}]{where}:\n{query_display}{extra}"
            if query_type in pipeline_types:
                q = query_display
                return f"{label} [{source_type} {query_type}]:\n{q}"
            else:
                filter_str = str(mongo_filter) if mongo_filter else "{}"
                return f"{label}: db.{query_display}.find({filter_str})"
        elif source_type == "ORACLE":
            # Use filtered query if available (has substituted values)
            # Use display query for multiline readability
            sql = query_display or ""
            if "select " not in sql.lower():
                if "." in sql:
                    sql = f"SELECT * FROM {sql}"
                else:
                    sql = f"SELECT * FROM {db}.{sql}"
            return f"{label} [{source_type}]:{sql}"
        return None

    def _query_console_text(self, tc_number, label, source_type, query_type,
                            query_display, db, collection=None):
        """
        Short terminal line for a Source / Target query; the log file keeps
        the full query (see ConsoleFormatter in Log_Config).
        """
        source_type = (source_type or "").upper()
        query_type = (query_type or "").upper()
        if source_type == "MONGODB":
            if query_type in ("PIPELINE", "JAVA_SCRIPT", "AGGREGATE"):
                where = f"{db}.{collection}" if collection else (db or "MongoDB")
                text = f"{label} [MONGODB {query_type}] Mongo Pipeline is executed on {where}"
            else:
                text = f"{label} [MONGODB] Mongo Query is executed on {db}.{query_display}"
        elif source_type == "ORACLE":
            sql = query_display or ""
            obj = (query_object(sql) if "select " in sql.lower()
                   else (sql if "." in sql else f"{db}.{sql}"))
            text = f"{label} [ORACLE] Oracle Query is executed on {obj}"
        else:
            return None
        return console_text(f"TestCase {tc_number}: {text} — get details in test case")

    def _format_query_for_display(self, query):
        """
        Formats a SQL / MongoDB query for human-readable display.

        Comma rules
        -----------
        - depth 0 (CTE level)          : comma at START of next line
        - depth > 0, subquery context  : comma at END of current line
          (SELECT column lists inside CTEs / subqueries)
        - depth > 0, function context  : no break
          (NVL, UPPER, COALESCE, argument lists)
        """
        if query is None:
            return "N/A"

        sql = "\n".join(query) if isinstance(query, list) else str(query)

        # Step 0 — collapse multiple spaces/tabs to a single space
        sql = re.sub(r'[ \t]+', ' ', sql)

        # Step 1 — break before SQL keywords (WITH / UNION ALL added for CTEs)
        SQL_KEYWORDS = (
            r"\b(WITH\s+RECURSIVE|WITH|SELECT|DISTINCT|FROM|WHERE"
            r"|LEFT\s+OUTER\s+JOIN|LEFT\s+JOIN|INNER\s+JOIN|JOIN"
            r"|ON|AND|OR|ORDER\s+BY|GROUP\s+BY|HAVING"
            r"|UNION\s+ALL|UNION|MINUS|INTERSECT|WHEN|THEN|ELSE|END)\b"
        )
        sql = re.sub(SQL_KEYWORDS, r'\n  \1', sql, flags=re.IGNORECASE)

        # Step 2 — context-aware comma handling.
        # Opening ( preceded by a SQL keyword  → subquery/CTE context (True).
        # Opening ( preceded by any identifier → function call        (False).
        SUBQUERY_OPENERS = {
            "AS", "WITH", "IN", "EXISTS", "NOT", "UNION",
            "INTERSECT", "EXCEPT", "MINUS", "SELECT", "FROM",
            "WHERE", "JOIN", "AND", "OR", "ON", "HAVING", "DISTINCT"
        }

        result = []
        depth = 0
        context_stack = []  # True = subquery/CTE, False = function call
        i = 0

        while i < len(sql):
            ch = sql[i]

            if ch == "(":
                depth += 1
                joined = "".join(result)
                m = re.search(r'\b([A-Za-z_]\w*)\s*$', joined)
                last_word = m.group(1).upper() if m else ""
                context_stack.append(
                    (not last_word) or (last_word in SUBQUERY_OPENERS)
                )
                result.append(ch)

            elif ch == ")":
                depth -= 1
                if context_stack:
                    context_stack.pop()
                result.append(ch)

            elif ch == ",":
                in_subquery = context_stack and context_stack[-1]

                if depth == 0:
                    # CTE level — comma at START of next line
                    while result and result[-1] == " ":
                        result.pop()
                    if result and result[-1] == ")":
                        result.append("\n\n  ,")  # blank line between CTEs
                    else:
                        result.append("\n  ,")
                    i += 1
                    while i < len(sql) and sql[i] == " ":
                        i += 1
                    continue

                elif in_subquery:
                    # Inside CTE / subquery SELECT columns: END of current line
                    while result and result[-1] == " ":
                        result.pop()
                    result.append(",")
                    result.append("\n  ")
                    i += 1
                    while i < len(sql) and sql[i] == " ":
                        i += 1
                    continue

                else:
                    # Inside function call — no break
                    result.append(ch)

            else:
                result.append(ch)

            i += 1

        return "".join(result).strip()

    @staticmethod
    def _exec_query(side_type, query_type, raw_query, filtered_query):
        """
        The query text to execute for one side. Oracle and MongoDB pipelines
        run the DataFilter-substituted text; collections/views run the raw
        name (their filters travel separately as a find()/$match filter).
        """
        st = (side_type or "").upper()
        if st == "ORACLE" or (st == "MONGODB" and is_pipeline_query(query_type)):
            return filtered_query
        return raw_query

    @staticmethod
    def _single_side_query(side_type, query_type, raw_query, filtered_query):
        """
        Query text for Null / Duplicate / Format checks. An Oracle QUERY runs
        with its DataFilter :placeholders filled in; every other QueryType
        keeps the original behaviour (raw query, filters passed separately).
        """
        if ((side_type or "").upper() == "ORACLE"
                and (query_type or "").upper() == "QUERY" and filtered_query):
            return filtered_query
        return raw_query

    @staticmethod
    def _is_edi835(source) -> bool:
        """
        True only when Source is an EDI835 pipeline — identified by the
        presence of an API_CALL or SFTP_DOWNLOAD terminal step.
        """
        if not isinstance(source, list):
            return False
        return any(
            (step.get("Type") or "").upper() in ("API_CALL", "SFTP_DOWNLOAD")
            for step in source
        )

    @staticmethod
    def _is_dynamic_pipeline(source) -> bool:
        """
        True when Source is a generic dynamic-file-location pipeline —
        a list of steps containing a terminal Type='AUTO' fetch step,
        and NOT an EDI835 pipeline (no API_CALL/SFTP_DOWNLOAD step).
        Pattern: ORACLE/MONGODB lookup step(s) resolve a file path,
        then a Type='AUTO' step fetches+parses that file.
        """
        if not isinstance(source, list):
            return False
        has_auto_step = any(
            (step.get("Type") or "").upper() == "AUTO" for step in source
        )
        return has_auto_step and not Regression_inputs._is_edi835(source)

    @staticmethod
    def _build_projection(join_fields, compare_fields, is_source=True):
        """
        Builds a MongoDB projection from JoinFields + CompareFields.
        Only fetches fields needed for comparison, reducing network I/O.
        Returns None when fields are unspecified (fetches all fields).
        """
        field_key = "SourceField" if is_source else "TargetField"
        fields = set()
        for jf in (join_fields or []):
            f = jf.get(field_key)
            if f:
                fields.add(f)
        for cf in (compare_fields or []):
            f = cf.get(field_key)
            if f:
                fields.add(f)
        if not fields:
            return None
        return {f: 1 for f in fields}

    def _fetch_pipeline_rows(self, s_type, s_db, s_query, s_env, s_qt,
                             s_filter, s_collection, tc_number, log_doc, run_type):
        """Runs a MongoDB aggregation source and returns its rows (or None on error)."""
        try:
            rows, _ = get_data(
                s_type, s_db, s_query, s_env,
                None, None, None, "",
                s_qt, None, s_filter, {},
                source_collection=s_collection
            )
        except Exception as e:
            rows = None
            logger.issue(f"TestCase {tc_number}: pipeline fetch failed: {e}")
        if rows is None:
            append_log_entry(log_doc, "Error", run_type,
                             "Aggregation pipeline returned no result (fetch error).")
            return None
        rows = list(rows)
        self._log_data_sample(rows, "Source", tc_number)
        return rows

    def _log_data_sample(self, data, side, tc_number):
        """Logs the first Visible_Row_count records after data fetch for verification."""
        if not data or self._visible_row_count <= 0:
            return

        if not isinstance(data, (list, tuple)):
            data = list(data)

        if not data:
            return data

        sample = data[:self._visible_row_count]
        logger.info(
            f"[TC {tc_number}] {side} data sample "
            f"({len(sample)} of {len(data)} records):"
        )
        for i, record in enumerate(sample, 1):
            logger.info(f"  {i}: {record}")

        return data

    def _prepare_rules(self):
        """
        Optimization #1 & #2: pre-resolves DataFilters, queries, and MongoDB
        projections for all rules in the main thread before workers start.
        Workers read pre-computed values instead of recomputing per-worker.
        """
        for rule in self.rules:
            source = rule.get("Source", {})
            is_edi835 = self._is_edi835(source)
            is_dynamic_pipeline = self._is_dynamic_pipeline(source)
            is_pipeline = is_edi835 or is_dynamic_pipeline
            rule["_is_edi835"] = is_edi835
            rule["_is_dynamic_pipeline"] = is_dynamic_pipeline
            rule["_is_pipeline"] = is_pipeline

            join_fields = rule.get("JoinFields", [])
            compare_fields = rule.get("CompareFields", [])
            if is_pipeline:
                # Pipeline filters resolved at runtime (uses _runtime_filters)
                rule["_src_filter"] = {}
                rule["_src_proj"] = None
                rule["_tgt_proj"] = None
                continue
            source_type = source.get("Type") if source else None
            source_db = source.get("DB") if source else None
            source_q = source.get("Query") if source else None
            source_qt = source.get("QueryType") if source else None
            target = rule.get("Target")
            target_type = target.get("Type") if target else None
            target_db = target.get("DB") if target else None
            target_q = target.get("Query") if target else None
            target_qt = target.get("QueryType") if target else None
            source_q = normalize_query_text(source_q)
            target_q = normalize_query_text(target_q)
            data_filters_raw = rule.get("DataFilters", [])
            try:
                sq_filtered, src_filter = self._apply_data_filter(
                    source_q, source_db, source_type,
                    data_filters_raw, query_type=source_qt, is_target=False, silent=True
                )
            except Exception as e:
                # A missing run-time value is reported once, when the test case runs.
                (logger.debug if isinstance(e, ValueError) else logger.warning)(
                    f"TC{rule.get('TestCaseNumber')} source filter prep failed: {e}")
                sq_filtered, src_filter = source_q, {}
            try:
                tq_filtered, tgt_filter = self._apply_data_filter(
                    target_q, target_db, target_type,
                    data_filters_raw, query_type=target_qt, is_target=True, silent=True
                )
            except Exception as e:
                (logger.debug if isinstance(e, ValueError) else logger.warning)(
                    f"TC{rule.get('TestCaseNumber')} target filter prep failed: {e}")
                tq_filtered, tgt_filter = target_q, {}
            # Optimization #2: build MongoDB projections (None for Oracle/non-Mongo)
            src_proj = (
                self._build_projection(join_fields, compare_fields, is_source=True)
                if source_type and source_type.upper() == "MONGODB" else None
            )
            tgt_proj = (
                self._build_projection(join_fields, compare_fields, is_source=False)
                if target_type and target_type.upper() == "MONGODB" else None
            )
            rule["_sq_filtered"] = sq_filtered
            rule["_src_filter"] = src_filter
            rule["_tq_filtered"] = tq_filtered
            rule["_tgt_filter"] = tgt_filter
            rule["_src_proj"] = src_proj
            rule["_tgt_proj"] = tgt_proj

            # source_env = source.get("Environment", "") if source else ""
            # rule["_is_large_dataset"] = False

            # if (source_type and source_type.upper() == "MONGODB"
            # and source_db and source_q
            # and (source_qt or "").upper() in ("COLLECTION", "VIEW", "TABLE", "")):
            #     try:
            #         count = Collection(source_db, source_q, source_env).getDataCount(src_filter)
            #         rule["_is_large_dataset"] = count > 10_000
            #         if rule["_is_large_dataset"]:
            #             logger.info(
            #                 f"TC{rule.get('TestCaseNumber')}: large dataset "
            #                 f"flagged ({count:,} source records)."
            #             )
            #     except Exception as e:
            #         rule["_is_large_dataset"] = False
            #         logger.warning(f"TC{rule.get('TestCaseNumber')}: count check failed: {e}")
            # else:
            #     rule["_is_large_dataset"] = False

    def _run_single(self, rule, lock, counters, log_id_by_module):
        """
        Executes one test case. Called by run_parallel() from multithreading.py.
        """
        module_name = rule.get("Module")
        testCaseNumber = rule.get("TestCaseNumber")
        testDescription = rule.get("TestDescription")
        validationType = rule.get("ValidationType")

        logger.info(f"========== [ Running TestCase {testCaseNumber}: {testDescription} ] ==========")
        start_time = time.time()
        log_doc = create_log_doc(testCaseNumber, testDescription)

        log_doc["DataValidationLog_ID"] = log_id_by_module.get(module_name)
        log_doc["Module"] = module_name
        log_doc["Stage"] = rule.get("Stage", None)

        source = rule.get("Source", {})
        target = None  # initialise early — Python 3.12 requires every
        # local variable to be bound before first read
        is_edi835 = self._is_edi835(source)
        is_dynamic_pipeline = self._is_dynamic_pipeline(source)
        is_pipeline = is_edi835 or is_dynamic_pipeline

        if is_pipeline:
            sourceType = "PIPELINE"
            sourceEnvironment = None
            sourceDB = None
            sourceQuery = None
            sourceQueryType = None
            source_steps = source
            source_def = None  # pipeline manages its own fetching
        else:
            sourceType = source.get("Type") if source else None
            sourceEnvironment = source.get("Environment") if source else None
            sourceDB = source.get("DB") if source else None
            sourceQuery = source.get("Query") if source else None
            sourceQueryType = source.get("QueryType") if source else None
            source_steps = None
            # Pass full source dict when source is a file type
            source_def = (
                source
                if source and (sourceType or "").upper() in FILE_TYPES
                else None
            )

        target = rule.get("Target") if source else None
        targetEnvironment = target.get("Environment") if target else None
        targetDB = target.get("DB") if target else None
        targetType = target.get("Type") if target else None
        targetQuery = target.get("Query") if target else None
        targetQueryType = target.get("QueryType") if target else None
        # MongoDB aggregation: Query holds the pipeline, Collection the
        # collection it runs on (unused for VIEW / COLLECTION query types).
        sourceCollection = (source.get("Collection")
                            if isinstance(source, dict) else None)
        targetCollection = target.get("Collection") if target else None

        # Target — always a dict, may be file type
        target_def = (
            target
            if target and (targetType or "").upper() in FILE_TYPES
            else None
        )

        joinFields = rule.get("JoinFields")
        compareFields = rule.get("CompareFields")
        expectedResult = rule.get("ExpectedResult")
        viceversa = rule.get("Viceversa")
        source_data = None
        target_data = None

        test_case_status = False
        sourceEnvKey = f"{sourceEnvironment}" if sourceEnvironment else ""
        targetEnvKey = f"{targetEnvironment}" if targetEnvironment else ""

        start_time = time.time()

        # ── Normalise for execution (space join) BEFORE DataFilters ───
        # Query may be a string, a list of lines, or (MongoDB pipelines) a list
        # of stage objects — normalise to one string so DataFilter
        # :placeholders can be substituted and the query can be logged.
        sourceQuery = normalize_query_text(sourceQuery)
        targetQuery = normalize_query_text(targetQuery)

        # ── Resolve DataFilters ───────────────────────────────────────
        data_filters_raw = rule.get("DataFilters", [])
        sourceQuery_filtered = sourceQuery if not is_edi835 else None
        targetQuery_filtered = targetQuery if not is_edi835 else None
        source_mongo_filter = {}
        target_mongo_filter = {}

        if is_edi835:
            target_mongo_filter = {}
            source_mongo_filter = {}

            for f in (data_filters_raw or []):
                src_field = f.get("SourceField", "")
                if not src_field:
                    continue  # target-only filter — skip for source
                default = f.get("DefaultValue", "")
                value = self._runtime_filters.get(src_field, "") or default

                if src_field and value:
                    source_mongo_filter[src_field] = _to_mongo_value(str(value))
                    sourceQuery_filtered = sourceQuery

        else:  # ← ADD THIS

            try:
                sourceQuery_filtered, source_mongo_filter = self._apply_data_filter(
                    sourceQuery, sourceDB, sourceType, data_filters_raw,
                    query_type=sourceQueryType, is_target=False
                )

                targetQuery_filtered, target_mongo_filter = self._apply_data_filter(
                    targetQuery, targetDB, targetType, data_filters_raw,
                    query_type=targetQueryType, is_target=True
                )
            except ValueError as e:
                # A run-time value the query needs was not given: fail this
                # test case only, with the -f to pass.
                logger.issue(f"TestCase {testCaseNumber}: {e}")
                append_log_entry(log_doc, "Error", "DataFilter", str(e))
                log_doc["Result"] = "Fail"
                execution_time = time.time() - start_time
                logger.info(f"Execution time for TestCase {testCaseNumber}: {execution_time:.2f} seconds")
                log_doc["MismatchFields"] = log_doc.get("MismatchFields", [])
                log_doc["MissingFields"] = log_doc.get("MissingFields", [])
                with lock:
                    finalize_and_store_log(log_doc, self.test_logs)
                    counters["fail"] += 1
                    counters["times"].append({"TimeSecs": round(execution_time, 2)})
                return

        if is_edi835:
            for step in (source_steps or []):
                step_num = step.get("Step")
                step_type = step.get("Type", "")
                step_info = (f"Step {step_num} [{step_type}]: "
                             f"Query={step.get('Query', '')} "
                             f"OutputKey={step.get('OutputKey', '')} "
                             f"InputKey={step.get('InputKey', '')}"
                             ).strip()
            logger.info(f"TestCase {testCaseNumber}: Source {step_info}")
            append_log_entry(log_doc, "Info", "Query", f"Source {step_info}")

        # ── Keep display versions (multiline) for logging ─────────────
        sourceQuery_display = (pipeline_display(sourceQuery_filtered)
                               if is_pipeline_query(sourceQueryType)
                               else self._format_query_for_display(sourceQuery_filtered))
        re.sub(r' {2,}', '', sourceQuery_display)
        targetQuery_display = (pipeline_display(targetQuery_filtered)
                               if is_pipeline_query(targetQueryType)
                               else self._format_query_for_display(targetQuery_filtered))

        # ── Log exact queries — multiline for readability ─────────────
        src_log = self._build_query_log(
            "Source", sourceQuery_display,
            sourceType, source_mongo_filter, sourceDB, sourceQueryType,
            collection=sourceCollection
        )
        if src_log:
            # for line in src_log.split("\n"):
            if src_log.strip():
                logger.info(f"TestCase {testCaseNumber}: {src_log}",
                            extra=self._query_console_text(
                                testCaseNumber, "Source", sourceType, sourceQueryType,
                                sourceQuery_display, sourceDB, sourceCollection))
                # logger.info(f"{line}")
            append_log_entry(log_doc, "Info", "Query", src_log)

        if targetQuery:
            tgt_log = self._build_query_log(
                "Target", targetQuery_display,
                targetType, target_mongo_filter, targetDB, targetQueryType,
                collection=targetCollection
            )
            if tgt_log:
                # for line in tgt_log.split("\n"):
                if tgt_log.strip():
                    logger.info(f"TestCase {testCaseNumber}: {tgt_log}",
                                extra=self._query_console_text(
                                    testCaseNumber, "Target", targetType, targetQueryType,
                                    targetQuery_display, targetDB, targetCollection))
                    # logger.info(f"{line}")
                append_log_entry(log_doc, "Info", "Query", tgt_log)

        # ── Validation type branching ─────────────────────────────────
        if validationType == "SRC_TGT":
            # Inject DataFilters into FileConfig for PDF sources/targets.
            # PDF has no SQL query interface — filters are applied as Python
            # predicates inside _parse_pdf via FileConfig._data_filters.
            def _inject_pdf_filters(side_def, filters):
                if not isinstance(side_def, dict):
                    return side_def
                if (side_def.get("FileConfig") or {}).get("Format", "").upper() != "PDF":
                    return side_def
                side_def = dict(side_def)
                fc = dict(side_def.get("FileConfig") or {})
                fc["_data_filters"] = [f for f in (filters or []) if f]
                side_def["FileConfig"] = fc
                return side_def

            source_def = _inject_pdf_filters(source_def, data_filters_raw)
            target_def = _inject_pdf_filters(target_def, data_filters_raw)

            # Inject resolved file filters into non-PDF file source/target defs.
            # PDF carries its own _data_filters inside FileConfig (handled above);
            # all other file types receive _file_filters on the def itself so
            # _fetch_file_data can use them for path substitution and row filtering.
            def _is_non_pdf_file(side_def):
                if not isinstance(side_def, dict): return False
                fmt = (side_def.get("FileConfig") or {}).get("Format", "").upper()
                return fmt != "PDF"

            if source_def and _is_non_pdf_file(source_def):
                source_def = inject_file_filters(
                    source_def, data_filters_raw,
                    self._runtime_filters, is_target=False
                )
            if target_def and _is_non_pdf_file(target_def):
                target_def = inject_file_filters(
                    target_def, data_filters_raw,
                    self._runtime_filters, is_target=True
                )

            if is_dynamic_pipeline:
                # ── Dynamic file-location pipeline (TC4/TC5 pattern) ──────
                # Step 1 (ORACLE/MONGODB) resolves a file path, Step 2
                # (Type=AUTO) fetches+parses it. Distinct from EDI835.
                from Libraries.File_parser import fetch_dynamic_pipeline

                filter_values = {}
                for f in (data_filters_raw or []):
                    src_field = f.get("SourceField", "")
                    if not src_field:
                        continue
                    default = f.get("DefaultValue", "")
                    value = self._runtime_filters.get(src_field, "") or default
                    if value:
                        filter_values[src_field] = value

                try:
                    pipeline_result = fetch_dynamic_pipeline(source_steps, filter_values)
                    df = pipeline_result["dataframe"]
                    source_data = df.to_dict("records") if not df.empty else []
                    logger.info(
                        f"TestCase {testCaseNumber}: Dynamic pipeline fetched "
                        f"{pipeline_result['file_count']} file(s), "
                        f"{pipeline_result['row_count']} row(s)."
                    )
                except Exception as e:
                    logger.issue(f"TestCase {testCaseNumber}: Dynamic pipeline fetch failed: {e}")
                    append_log_entry(log_doc, "Error", "SRC->TGT", f"Dynamic pipeline fetch failed: {e}")
                    source_data = []

                tgt_q = self._exec_query(targetType, targetQueryType, targetQuery, targetQuery_filtered)
                _, target_data = get_data(
                    None, None, None, "",
                    targetType, targetDB, tgt_q, targetEnvKey,
                    None, targetQueryType,
                    {}, target_mongo_filter,
                    source_def=None,
                    target_def=target_def,
                    target_collection=targetCollection
                )
            else:
                src_q = self._exec_query(sourceType, sourceQueryType, sourceQuery, sourceQuery_filtered)
                tgt_q = self._exec_query(targetType, targetQueryType, targetQuery, targetQuery_filtered)
                source_data, target_data = get_data(
                    sourceType, sourceDB, src_q, sourceEnvKey,
                    targetType, targetDB, tgt_q, targetEnvKey,
                    sourceQueryType, targetQueryType,
                    source_mongo_filter,
                    target_mongo_filter,
                    source_def=source_def,
                    target_def=target_def,
                    source_collection=sourceCollection,
                    target_collection=targetCollection
                )
            src_empty = is_cursor_empty(source_data, "source_data")
            tgt_empty = is_cursor_empty(target_data, "target_data")
            if src_empty and tgt_empty:
                logger.warning("Source and Target data both are empty. Skipping comparison.")
                append_log_entry(log_doc, "Warning", "SRC->TGT",
                                 "Source and Target data both are empty. Skipping.")
                log_doc["Result"] = "Skip"
            elif src_empty and not tgt_empty:
                # Show the non-empty side's sample before logging the error
                # so the user can verify what data WAS pulled on TGT.
                if isinstance(target_data, Cursor):
                    target_data = list(target_data)
                self._log_data_sample(target_data, "Target", testCaseNumber)
                logger.issue("Source data empty and Target having data. Please check SRC.")
                append_log_entry(log_doc, "ISSUE", "SRC->TGT",
                                 "Source data empty and Target having data. Please check SRC.")
                test_case_status = True
            elif tgt_empty and not src_empty:
                # Show the non-empty side's sample before logging the error
                # so the user can verify what data WAS pulled on SRC.
                if isinstance(source_data, Cursor):
                    source_data = list(source_data)
                self._log_data_sample(source_data, "Source", testCaseNumber)
                logger.issue("Target data empty and Source having data. Please check TGT.")
                append_log_entry(log_doc, "ISSUE", "SRC->TGT",
                                 "Target data empty and Source having data. Please check TGT.")
                test_case_status = True
            else:
                if isinstance(source_data, Cursor):
                    source_data = list(source_data)
                if isinstance(target_data, Cursor):
                    target_data = list(target_data)

                self._log_data_sample(source_data, "Source", testCaseNumber)
                self._log_data_sample(target_data, "Target", testCaseNumber)

                if source_data and target_data:
                    append_log_entry(log_doc, "Info", "SRC->TGT",
                                     f"Total source {sourceType} records: {len(source_data)}, "
                                     f"Total target {targetType} records: {len(target_data)}"
                                     )
                    # CaseSensitiveJoin (FileConfig) — only meaningful when
                    # JoinFields="ALL" (full-row compare with no explicit
                    # JoinFields entries to carry their own "CaseSensitive" key)
                    case_sensitive_join = True
                    for _side in (source, target):
                        if isinstance(_side, dict):
                            _fc = _side.get("FileConfig") or {}
                            if "CaseSensitiveJoin" in _fc:
                                case_sensitive_join = bool(_fc["CaseSensitiveJoin"])
                                break

                    future = self._compare_executor.submit(
                        compare_data_in_process,
                        source_data, target_data, sourceType, targetType,
                        joinFields, compareFields, testCaseNumber, testDescription,
                        viceversa, case_sensitive_join
                    )
                    test_case_status, remote_log_doc, log_records = future.result()
                    # Replay subprocess logs into this worker's capture buffer
                    for rec in log_records:
                        logger.log(rec["levelno"], rec["msg"])
                    # Merge log_doc from subprocess back into this TC's log_doc
                    log_doc["Log"].extend(remote_log_doc.get("Log", []))
                    if remote_log_doc.get("Result") == "Fail":
                        log_doc["Result"] = "Fail"
                    for fld in remote_log_doc.get("MismatchFields", []):
                        log_doc.setdefault("MismatchFields", [])
                        if fld not in log_doc["MismatchFields"]:
                            log_doc["MismatchFields"].append(fld)
                    for fld in remote_log_doc.get("MissingFields", []):
                        log_doc.setdefault("MissingFields", [])
                        if fld not in log_doc["MissingFields"]:
                            log_doc["MissingFields"].append(fld)

                    # File vs Oracle table: every file column must exist in the table.
                    if _file_columns_missing_in_table(rule, source_data, target_data,
                                                      testCaseNumber, log_doc):
                        test_case_status = True
                        log_doc["Result"] = "Fail"

        elif validationType == "Record_Count":

            src_q = self._exec_query(sourceType, sourceQueryType, sourceQuery, sourceQuery_filtered)
            tgt_q = self._exec_query(targetType, targetQueryType, targetQuery, targetQuery_filtered)

            source_data, target_data = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                targetType, targetDB, tgt_q, targetEnvKey,
                sourceQueryType, targetQueryType,
                source_mongo_filter,
                target_mongo_filter,
                source_def=source_def,
                target_def=target_def,
                source_collection=sourceCollection,
                target_collection=targetCollection
            )

            if source_data is None and target_data is None:
                # msg = f"Error fetching data. Cannot evaluate test case."
                # logger.issue(msg)
                # append_log_entry(log_doc,"ISSUE","Record_Count", msg)
                test_case_status = True
            else:
                source_data = list(source_data) if isinstance(source_data, Cursor) else (
                    source_data if isinstance(source_data, list) else None)
                target_data = list(target_data) if isinstance(target_data, Cursor) else (
                    target_data if isinstance(target_data, list) else None)
                source_count = len(source_data) if source_data else 0
                target_count = len(target_data) if target_data else 0
                approvedValues = int(rule.get("ApprovedValues")[0])
                source_data = self._log_data_sample(source_data, "Source", testCaseNumber) or source_data
                target_data = self._log_data_sample(target_data, "Target", testCaseNumber) or target_data

                test_case_status = run_row_count_validation(
                    source_count, target_count, approvedValues,
                    source_data, target_data,
                    testCaseNumber, testDescription, log_doc
                )


        elif validationType == "InApprovedValue":

            src_q = self._exec_query(sourceType, sourceQueryType, sourceQuery, sourceQuery_filtered)

            source_data, _ = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                targetType, targetDB, targetQuery, targetEnvKey,
                sourceQueryType, targetQueryType,
                source_mongo_filter,
                {},
                source_def=source_def,
                source_collection=sourceCollection
            )

            approved_values = rule.get("ApprovedValues", [])
            self._log_data_sample(source_data, "Source", testCaseNumber) if source_data else 0
            test_case_status = inapproved_validation(
                source_data, approved_values, compareFields,
                testCaseNumber, testDescription, log_doc
            )

        elif validationType == "Null_Check":
            check_fields = rule.get("CheckFields", [])
            if (sourceType or "").upper() == "MONGODB" and is_pipeline_query(sourceQueryType):
                # Aggregation source: run the pipeline, then check the rows
                # in memory with the same logic used for files.
                test_case_status = _run_null_check_on_file(
                    None, check_fields,
                    testCaseNumber, testDescription, log_doc,
                    rows=self._fetch_pipeline_rows(
                        sourceType, sourceDB,
                        self._exec_query(sourceType, sourceQueryType,
                                         sourceQuery, sourceQuery_filtered),
                        sourceEnvKey, sourceQueryType, source_mongo_filter,
                        sourceCollection, testCaseNumber, log_doc, "Null_Check")
                )
            elif source_def:
                test_case_status = _run_null_check_on_file(
                    source_def, check_fields,
                    testCaseNumber, testDescription, log_doc
                )
            else:
                test_case_status = run_null_check(
                    sourceType, sourceDB,
                    self._single_side_query(sourceType, sourceQueryType,
                                            sourceQuery, sourceQuery_filtered),
                    sourceEnvKey, sourceQueryType,
                    check_fields, testCaseNumber, testDescription,
                    log_doc, source_mongo_filter
                )

        elif validationType == "Duplicate_Check":
            check_fields = rule.get("CheckFields", [])
            if (sourceType or "").upper() == "MONGODB" and is_pipeline_query(sourceQueryType):
                # Aggregation source: run the pipeline, then check the rows
                # in memory with the same logic used for files.
                test_case_status = _run_duplicate_check_on_file(
                    None, check_fields,
                    testCaseNumber, testDescription, log_doc,
                    rows=self._fetch_pipeline_rows(
                        sourceType, sourceDB,
                        self._exec_query(sourceType, sourceQueryType,
                                         sourceQuery, sourceQuery_filtered),
                        sourceEnvKey, sourceQueryType, source_mongo_filter,
                        sourceCollection, testCaseNumber, log_doc, "Duplicate_Check")
                )
            elif source_def:
                test_case_status = _run_duplicate_check_on_file(
                    source_def, check_fields,
                    testCaseNumber, testDescription, log_doc
                )
            else:
                test_case_status = run_duplicate_check(
                    sourceType, sourceDB,
                    self._single_side_query(sourceType, sourceQueryType,
                                            sourceQuery, sourceQuery_filtered),
                    sourceEnvKey, sourceQueryType,
                    check_fields, testCaseNumber, testDescription,
                    log_doc, source_mongo_filter
                )

        elif validationType == "Format_Check":
            check_fields = rule.get("CheckFields", [])
            if (sourceType or "").upper() == "MONGODB" and is_pipeline_query(sourceQueryType):
                # Aggregation source: run the pipeline, then check the rows
                # in memory with the same logic used for files.
                test_case_status = _run_format_check_on_file(
                    None, check_fields,
                    testCaseNumber, testDescription, log_doc,
                    rows=self._fetch_pipeline_rows(
                        sourceType, sourceDB,
                        self._exec_query(sourceType, sourceQueryType,
                                         sourceQuery, sourceQuery_filtered),
                        sourceEnvKey, sourceQueryType, source_mongo_filter,
                        sourceCollection, testCaseNumber, log_doc, "Format_Check")
                )
            elif source_def:
                test_case_status = _run_format_check_on_file(
                    source_def, check_fields,
                    testCaseNumber, testDescription, log_doc
                )
            else:
                test_case_status = run_format_check(
                    sourceType, sourceDB,
                    self._single_side_query(sourceType, sourceQueryType,
                                            sourceQuery, sourceQuery_filtered),
                    sourceEnvKey, sourceQueryType,
                    check_fields, testCaseNumber, testDescription,
                    log_doc, source_mongo_filter
                )

        elif validationType == "EDI835_Format":
            check_fields = rule.get("CheckFields", [])
            test_case_status = run_edi835_format_check(
                source_steps=source_steps,
                data_filters=source_mongo_filter,
                runtime_filters=self._runtime_filters,
                check_fields=check_fields,
                test_case_number=testCaseNumber,
                test_description=testDescription,
                log_doc=log_doc,
                visible_row_count=self._visible_row_count
            )

        elif validationType == "EDI835_Balance":
            check_fields = rule.get("CheckFields", [])
            test_case_status = run_edi835_balance_check(
                source_steps=source_steps,
                data_filters=source_mongo_filter,
                runtime_filters=self._runtime_filters,
                check_fields=check_fields,
                test_case_number=testCaseNumber,
                test_description=testDescription,
                log_doc=log_doc,
                visible_row_count=self._visible_row_count
            )

        elif validationType == "EDI835_Data":
            viceversa = rule.get("Viceversa", False)
            target_only_filters = [
                f for f in (data_filters_raw or [])
                if f.get("TargetField", "")
            ]

            _, edi835_target_mongo_filter = self._apply_data_filter(
                None, None, "MONGODB", target_only_filters,
                is_target=True
            ) if target_only_filters else (None, {})

            edi835_target_oracle_filter, _ = self._apply_data_filter(
                "", None, "ORACLE", target_only_filters,
                is_target=True
            ) if target_only_filters and targetType and targetType.upper() == "ORACLE" else ("", None)

            test_case_status = run_edi835_data_check(
                source_steps=source_steps,
                data_filters=source_mongo_filter,
                target_mongo_filter=edi835_target_mongo_filter,
                target_oracle_filter=edi835_target_oracle_filter,
                runtime_filters=self._runtime_filters,
                target=rule.get("Target"),
                targets=rule.get("Targets", []),
                test_case_number=testCaseNumber,
                test_description=testDescription,
                log_doc=log_doc,
                viceversa=viceversa,
                visible_row_count=self._visible_row_count
            )

        elif validationType == "DataView":
            # ── DataView ────────────────────────────────────────────────
            # Diagnostic type: fetch the source and display N rows so the
            # user can inspect the data without writing a comparison.
            #
            # Row count precedence:
            #   1. -v / --viewrows          (runtime CLI parameter)
            #   2. [DataView] rows          (environment.ini)
            #   3. [Visible_Row_count] count(environment.ini fallback)
            #   0 = show every row
            #
            # Transforms: when the source declares FileConfig.Columns, each
            # row is passed through _process_row — the same pipeline used by
            # CSV/Excel/PDF parsing — so Transform, Type, Regex, Precision
            # and NullValues all apply before display.
            #
            # Output format: -vf table (aligned grid) or -vf row (one per line)
            view_rows = self._view_rows
            view_format = self._view_format

            src_q = self._exec_query(sourceType, sourceQueryType,
                                     sourceQuery, sourceQuery_filtered)

            source_data, _ = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                None, None, None, "",
                sourceQueryType, None,
                source_mongo_filter, {},
                source_def=source_def,
                target_def=None,
                source_collection=sourceCollection
            )

            if isinstance(source_data, Cursor):
                source_data = list(source_data)

            if not source_data:
                logger.warning(
                    f"[TC {testCaseNumber}] DataView — source returned no rows."
                )
                append_log_entry(log_doc, "Warning", "DataView",
                                 "Source returned no rows.")
            else:
                total = len(source_data)
                shown = total if view_rows <= 0 else min(view_rows, total)
                rows = source_data[:shown]

                # ── Apply transforms when Columns are configured ──────────
                columns_cfg = (
                    (source_def or {}).get("FileConfig", {}).get("Columns", [])
                    if isinstance(source_def, dict) else []
                )
                if columns_cfg:
                    from Libraries.File_parser import _process_row
                    null_values = (
                            (source_def or {}).get("FileConfig", {}).get("NullValues", [])
                            or []
                    )
                    transformed, tx_errors = [], []
                    for i, r in enumerate(rows, 1):
                        try:
                            processed, row_errs = _process_row(
                                dict(r), columns_cfg, null_values
                            )
                            transformed.append(processed)
                            for e in row_errs:
                                tx_errors.append(f"row {i}: {e.get('Error', e)}")
                        except Exception as _te:
                            logger.warning(
                                f"[TC {testCaseNumber}] DataView — transform "
                                f"failed on row {i}: {_te}. Showing raw row."
                            )
                            transformed.append(r)
                    rows = transformed
                    logger.info(
                        f"[TC {testCaseNumber}] DataView — applied "
                        f"{len(columns_cfg)} column transform(s)."
                    )
                    for e in tx_errors[:int(missing)]:
                        logger.warning(f"  ↳ transform: {e}")

                logger.info(
                    f"[TC {testCaseNumber}] DataView — showing {shown} of "
                    f"{total} row(s) from {sourceType}:"
                )

                # ── Render ────────────────────────────────────────────────
                if view_format == "table":
                    try:
                        for line in _render_centered_table(rows):
                            logger.info(f"  {line}")
                    except Exception as _fe:
                        logger.warning(
                            f"[TC {testCaseNumber}] DataView — table render "
                            f"failed ({_fe}); falling back to row format."
                        )
                        for i, row in enumerate(rows, 1):
                            logger.info(f"  {i}: {row}")
                else:
                    for i, row in enumerate(rows, 1):
                        logger.info(f"  {i}: {row}")

                append_log_entry(
                    log_doc, "Info", "DataView",
                    f"Displayed {shown} of {total} row(s) from {sourceType}."
                )
                if total > shown:
                    logger.info(
                        f"  ... {total - shown} more row(s) not shown. "
                        f"Use -v <n> to show more, or -v 0 for all."
                    )

            # DataView is diagnostic only — it never fails a run
            log_doc["Result"] = "Pass"
            test_case_status = False

        else:
            msg = f"Validation Logic is not Implemented for {validationType}"
            logger.warning(msg)
            append_log_entry(log_doc, "Warning", "Validation", msg)
            log_doc["Result"] = "Fail"
            test_case_status = True

        # ── Per test case wrap-up ─────────────────────────────────────
        # result = log_doc.get("Result","")
        end_time = time.time()
        execution_time = end_time - start_time

        logger.info(f"Execution time for TestCase {testCaseNumber}: {execution_time:.2f} seconds")

        log_doc["MismatchFields"] = log_doc.get("MismatchFields", [])
        log_doc["MissingFields"] = log_doc.get("MissingFields", [])

        with lock:
            finalize_and_store_log(log_doc, self.test_logs)
            if log_doc.get("Result") == "Skip":
                counters["skipped"] += 1
            elif test_case_status:
                counters["fail"] += 1
                log_doc["Result"] = "Fail"
            else:
                counters["pass"] += 1
                log_doc["Result"] = "Pass"
            counters["times"].append({"TimeSecs": round(execution_time, 2)})

    def run(self, max_workers=config_max_workers):
        """
        Main validation execution loop.
        """
        try:
            # pass_count      = 0
            # fail_count      = 0
            # skipped_count   = 0
            # test_logs       = []
            # execution_times = []
            self.testcase_details()
            # Optimization #1/#2: pre-resolve filters & projections
            self._prepare_rules()

            try:
                self._visible_row_count = int(cr.visible_row_count("Visible_Row_count"))
            except Exception:
                logger.warning(f"Visible_Row_count missing in config file")

            # ProcessPoolExecutor for CPU-bound comparison (bypasses GIL)
            self._compare_executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 4)
            # Optimization #3: pre-fetch sequence IDs for all modules
            unique_modules = {r.get("Module") for r in self.rules if r.get("Module")}
            log_id_by_module = {m: get_next_sequence_id(self.env_label) for m in unique_modules}

            _wall_start = time.time()
            counters = run_parallel(self.rules, self._run_single, log_id_by_module=log_id_by_module,
                                    max_workers=max_workers)
            wall_time = round(time.time() - _wall_start, 2)
            self.test_logs.sort(key=lambda t: int(t.get("TestCaseID", 0)))
            pass_count = counters["pass"]
            fail_count = counters["fail"]
            skipped_count = counters["skipped"]
            execution_times = counters["times"]
            # ── Post-loop summary ─────────────────────────────────────────────
            logger.info(
                f"Total Number of Testcases Executed : {len(self.rules)},"
                f"Pass : {pass_count}, Fail : {fail_count}, Skippped : {skipped_count}"
            )
            # total_time = sum(t["TimeSecs"] for t in execution_times)
            total_time = wall_time
            logger.info(
                f"Total execution time: {total_time:.2f} seconds "
                f"({total_time / 60:.1f} minutes)"
            )
            consolidated_log_path = write_master_log_to_file(
                self.log_collection, self.env_label,
                self._module_name, self._stage, self._cycle, self.test_logs
            )

            path = os.path.join(scripts_path, "Config", "email_config.xml")
            config = read_email_config_from_ini()
            recipients = [r.strip() for r in config.get("email_to", "").split(",") if r]
            rows = [{
                "Environment": self.env_label,
                "DataValidationLog_ID": log.get("DataValidationLog_ID"),
                "Module": log.get("Module"),
                "Stage": self._stage or "",
                "Cycle": self._cycle or "",
                "TestCaseID": log.get("TestCaseID"),
                "TestCaseName": log.get("TestCaseName"),
                "StartTime": log.get("StartTime"),
                "EndTime": log.get("EndTime"),
                "Result": log.get("Result"),
                "MismatchFields": ", ".join(log.get("MismatchFields", [])),
                "MissingFields": ", ".join(log.get("MissingFields", []))
            } for log in self.test_logs]
            base_name = os.path.splitext(os.path.basename(consolidated_log_path))[0]
            send_email_report(
                recipients=recipients,
                subject=f"Validation Report - {base_name}",
                master_log_rows=rows,
                attachment_path=consolidated_log_path,
                sender_email=config["email_from"],
                smtp_server=config["email_host"],
                report_type="both"
            )

        except Exception as e:
            logger.exception(f"Exception occurred during regression validation run: {e}")
            raise


def _rows_or_file(source_def, rows=None) -> dict:
    """
    Data for the in-memory Null / Duplicate / Format checks: rows already
    fetched (e.g. from a MongoDB aggregation, nested fields flattened with
    dot names) or the file described by source_def. source_def=None with
    rows=None means the pipeline fetch failed — returns an empty frame so the
    check fails ("empty or fetch failed").
    """
    if rows is not None or source_def is None:
        rows = rows or []
        df = pd.json_normalize(rows) if rows else pd.DataFrame()
        if "_id" in df.columns:
            df = df.drop(columns=["_id"])
        return {"dataframe": df, "schema_errors": [], "parse_errors": []}
    from Libraries.File_parser import fetch_and_parse
    return fetch_and_parse(source_def)


def _run_format_check_on_file(source_def, check_fields,
                              test_case_number, test_description, log_doc, rows=None):
    import re as _re
    from Libraries.File_parser import fetch_and_parse
    from Libraries.Regression_Log_Helper import append_log_entry
    try:
        result = _rows_or_file(source_def, rows)
        df = result["dataframe"]
        status = False
        if result["schema_errors"]:
            for err in result["schema_errors"]:
                logger.issue(f"TestCase {test_case_number}: Format_Check schema error — {err}")
                append_log_entry(log_doc, "Error", "Format_Check", f"Schema error: {err}")
            status = True
        if result["parse_errors"]:
            status = True
        if df.empty:
            logger.issue(f"TestCase {test_case_number}: Format_Check — file empty or parse failed.")
            log_doc["Result"] = "Fail"
            return True
        for field_def in (check_fields or []):
            field = field_def.get("Field", "") or field_def.get("SourceField", "")
            pattern = field_def.get("Regex", "") or field_def.get("Pattern", "")
            format_type = str(field_def.get("FormatType") or "").upper()
            # Aggregation (PIPELINE) rows: also check DATE_FORMAT / LENGTH /
            # NUMERIC_RANGE, as the database Format_Check does. Files keep
            # their regex-only behaviour.
            value_check = (rows is not None and not pattern
                           and format_type in ("DATE_FORMAT", "LENGTH", "NUMERIC_RANGE"))
            if not field or not (pattern or value_check):
                continue
            if field not in df.columns:
                logger.issue(f"TestCase {test_case_number}: Format_Check — field '{field}' not found.")
                append_log_entry(log_doc, "Error", "Format_Check", f"Field '{field}' not found in file.")
                status = True
                continue
            failing = df[field].dropna()
            if value_check:
                ok = _format_value_check(format_type, field_def.get("Value", ""))
                pattern = f"{format_type} {field_def.get('Value', '')}".strip()
                failing = failing[~failing.apply(ok)]
            else:
                failing = failing[~failing.astype(str).str.match(pattern, na=False)]
            if not failing.empty:
                logger.issue(
                    f"TestCase {test_case_number}: Format_Check — field '{field}': {len(failing)} value(s) fail pattern '{pattern}'. Sample: {failing.head(5).tolist()}")
                append_log_entry(log_doc, "Error", "Format_Check",
                                 f"Field '{field}': {len(failing)} fail '{pattern}'. Sample: {failing.head(5).tolist()}")
                log_doc.setdefault("MismatchFields", []).append(field)
                status = True
            else:
                logger.info(f"TestCase {test_case_number}: Format_Check — field '{field}': all match '{pattern}' ✓")
                append_log_entry(log_doc, "Info", "Format_Check", f"Field '{field}': all match '{pattern}'")
        log_doc["Result"] = "Fail" if status else "Pass"
        return status
    except Exception as e:
        logger.exception(f"TestCase {test_case_number}: _run_format_check_on_file: {e}")
        append_log_entry(log_doc, "Error", "Format_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
        return True


def _format_value_check(format_type, value):
    """Row-level test for a non-regex Format_Check on aggregation rows."""
    import re as _re
    from datetime import date as _date
    from Libraries.data_fetcher import FORMAT_DATE_REGEX_MAP
    if format_type == "DATE_FORMAT":
        regex = FORMAT_DATE_REGEX_MAP.get(str(value or "").upper(), "")

        def ok(v):
            if isinstance(v, _date):  # a real date / datetime is valid
                return True
            return bool(regex) and bool(_re.match(regex, str(v)))

        return ok
    if format_type == "LENGTH":
        try:
            length = int(value)
        except (TypeError, ValueError):
            return lambda v: True
        return lambda v: len(str(v).strip()) == length
    try:
        low, high = (float(p.strip()) for p in str(value).split("-"))
    except Exception:
        return lambda v: True

    def in_range(v):
        try:
            return low <= float(v) <= high
        except (TypeError, ValueError):
            return False

    return in_range


def _run_null_check_on_file(source_def, check_fields,
                            test_case_number, test_description, log_doc, rows=None):
    from Libraries.File_parser import fetch_and_parse
    from Libraries.Regression_Log_Helper import append_log_entry
    try:
        result = _rows_or_file(source_def, rows)
        df = result["dataframe"]
        status = False
        total_rows = len(df)
        if df.empty:
            logger.issue(f"TestCase {test_case_number}: Null_Check — file empty or parse failed.")
            log_doc["Result"] = "Fail"
            return True
        # Build a case/underscore-insensitive lookup once so DisplayField
        # and Field names match DataFrame columns regardless of casing
        # differences between the test case config and the source file.
        col_lookup = {str(c).lower().replace(" ", "_"): c for c in df.columns}

        for field_def in (check_fields or []):
            field = field_def.get("Field", "") or field_def.get("SourceField", "")
            # Aggregation (PIPELINE) rows fail on nulls like the database
            # Null_Check does, unless the field says Required: false.
            # Files keep their default (not required).
            required = field_def.get("Required", rows is not None)
            display_field = field_def.get("DisplayField", "")
            if not field:
                continue

            actual_field = field if field in df.columns else col_lookup.get(field.lower().replace(" ", "_"))
            if not actual_field:
                if required:
                    logger.issue(f"TestCase {test_case_number}: Null_Check — required field '{field}' not found.")
                    append_log_entry(log_doc, "Error", "Null_Check", f"Required field '{field}' not found.")
                    status = True
                continue
            field = actual_field

            actual_display = (
                display_field if display_field in df.columns
                else col_lookup.get(display_field.lower().replace(" ", "_"), "")
            ) if display_field else ""

            null_mask = df[field].isna() | df[field].astype(str).str.strip().str.lower().isin(
                ["", "null", "none", "na", "n/a", "nan"]
            )
            null_count = int(null_mask.sum())

            if null_count > 0 and required:
                logger.issue(
                    f"TestCase {test_case_number}: Null_Check — field '{field}': {null_count}/{total_rows} null(s) — FAIL")
                append_log_entry(log_doc, "Error", "Null_Check",
                                 f"Field '{field}': {null_count}/{total_rows} nulls found.")
                log_doc.setdefault("MismatchFields", []).append(field)
                if actual_display:
                    # Cap displayed rows at the configured No_Of_Records_Display limit
                    for _, row in df[null_mask].head(int(missing)).iterrows():
                        display_val = row.get(actual_display, "")
                        logger.issue(f"  ↳ {actual_display} = {display_val!r} has null '{field}'")
                        append_log_entry(log_doc, "Error", "Null_Check",
                                         f"{actual_display} = {display_val!r} has null '{field}'.")
                    if null_count > int(missing):
                        logger.issue(f"  ↳ ... {null_count - int(missing)} more null '{field}' row(s) not shown.")
                status = True
            elif null_count > 0:
                logger.warning(
                    f"TestCase {test_case_number}: Null_Check — field '{field}': {null_count}/{total_rows} null(s) — WARNING (not required)")
                append_log_entry(log_doc, "Warning", "Null_Check",
                                 f"Field '{field}': {null_count}/{total_rows} nulls — not required.")
                if actual_display:
                    # Cap displayed rows at the configured No_Of_Records_Display limit
                    for _, row in df[null_mask].head(int(missing)).iterrows():
                        display_val = row.get(actual_display, "")
                        logger.warning(f"  ↳ {actual_display} = {display_val!r} has null '{field}'")
                    if null_count > int(missing):
                        logger.issue(f"  ↳ ... {null_count - int(missing)} more null '{field}' row(s) not shown.")
            else:
                logger.info(f"TestCase {test_case_number}: Null_Check — field '{field}': 0 null(s) ✓")
                append_log_entry(log_doc, "Info", "Null_Check", f"Field '{field}': No nulls found.")
        log_doc["Result"] = "Fail" if status else "Pass"
        return status
    except Exception as e:
        logger.exception(f"TestCase {test_case_number}: _run_null_check_on_file: {e}")
        append_log_entry(log_doc, "Error", "Null_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
        return True


# File formats whose columns are the file's own fields (EDI / HL7 / NCPDP / PDF
# parsers add derived columns a table is not expected to hold).
_TABULAR_FILE_FORMATS = {"CSV", "PIPE", "TSV", "FIXED_WIDTH", "EXCEL", "JSON", "XML"}
_FILE_BOOKKEEPING = {"SourceFile", "_chunk_seq", "_row_seq"}


def _file_columns_missing_in_table(rule, source_data, target_data, test_case_number, log_doc) -> bool:
    """
    SRC_TGT between a file (CSV, PIPE, TSV, FIXED_WIDTH, EXCEL, JSON, XML) and an
    Oracle table: every column of the file must exist in the table, so the
    file is loaded field for field. A file column with no table column of the
    same name (cZPassPaymentIds ~ C_ZPASS_PAYMENT_IDS) fails the test case and
    is named in the log. A file column the table has but the test case does
    not compare is reported as a warning. FileConfig "IgnoreColumns": [...]
    leaves columns out; "CheckAllColumns": false turns the check off.
    Returns True when columns are missing.
    """
    source, target = rule.get("Source"), rule.get("Target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False
    def is_file(side):
        fmt = str((side.get("FileConfig") or {}).get("Format") or "").upper()
        return str(side.get("Type") or "").upper() in FILE_TYPES and fmt in _TABULAR_FILE_FORMATS
    if is_file(source) and str(target.get("Type") or "").upper() == "ORACLE":
        file_side, db_side, rows, file_key, db_key = source, target, source_data, "SourceField", "TargetField"
    elif is_file(target) and str(source.get("Type") or "").upper() == "ORACLE":
        file_side, db_side, rows, file_key, db_key = target, source, target_data, "TargetField", "SourceField"
    else:
        return False
    fc = file_side.get("FileConfig") or {}
    if fc.get("CheckAllColumns") is False or not rows:
        return False
    join, compare = rule.get("JoinFields"), rule.get("CompareFields") or []
    if join == "ALL" or join == ["ALL"]:
        return False
    pairs = [f for f in (join or []) + compare if isinstance(f, dict)]
    compared = {str(f.get(file_key)) for f in pairs}
    ignore = {str(c) for c in (fc.get("IgnoreColumns") or [])} | _FILE_BOOKKEEPING
    sample = rows[:200] if isinstance(rows, list) else []
    file_cols = list(dict.fromkeys(k for r in sample if isinstance(r, dict) for k in r))
    unpaired = [c for c in file_cols if str(c) not in compared and str(c) not in ignore]
    if not unpaired:
        return False

    # The table's columns (all of them, not only the ones the query selects).
    query = str(db_side.get("Query") or "")
    table = (query_object(query) if "select " in query.lower() else query).strip()
    if table and "." not in table and db_side.get("DB"):
        table = f"{db_side['DB']}.{table}"
    db_cols = []
    try:
        from Libraries.OracleDB import OracleConnection
        owner, _, name = table.upper().rpartition(".")
        rows_ = OracleConnection(str(db_side.get("Environment") or "")).fetchAllDict(
            f"SELECT column_name FROM all_tab_columns WHERE owner = '{owner}' "
            f"AND table_name = '{name}' ORDER BY column_id")
        db_cols = [r.get("COLUMN_NAME") or r.get("column_name") for r in rows_ or []]
    except Exception as exc:
        logger.warning(f"TestCase {test_case_number}: could not read the columns of {table} ({exc}).")
    if not db_cols:
        logger.warning(f"TestCase {test_case_number}: column check skipped — no columns read for {table}.")
        return False

    try:
        from Libraries.File_Table_Mapper import _tier
    except Exception:
        _tier = None
    squash = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    def table_column_for(col):
        exact = [c for c in db_cols if squash(c) == squash(col)]
        if exact:
            return exact[0]
        # DW names that carry a one-letter type prefix: Master.cName ~ C_MASTER_C_NAME
        prefixed = [c for c in db_cols
                    if re.match(r"^[A-Za-z]_", str(c)) and squash(str(c)[2:]) == squash(col)]
        if prefixed:
            return prefixed[0]
        if _tier is not None:
            close = [c for c in db_cols if _tier(c, col) is not None]
            if close:
                return close[0]
        return None

    missing, not_compared = [], []
    for col in unpaired:
        match = table_column_for(col)
        filled = sum(1 for r in rows if isinstance(r, dict) and str(r.get(col) or "").strip() not in ("", "nan", "None"))
        if str(col).startswith("Unnamed:") and not filled:
            continue  # blank trailing Excel/CSV column, not a field
        (not_compared if match else missing).append((col, match, filled))
    if not_compared:
        msg = (f"{len(not_compared)} file column(s) exist in {table} but are not in CompareFields, "
               f"so their data is not checked: "
               + ", ".join(f"{col} ({match})" for col, match, _ in not_compared))
        logger.warning(f"TestCase {test_case_number}: {msg}")
        append_log_entry(log_doc, "Warning", "SRC->TGT", msg)
    if not missing:
        return False
    summary = (f"{len(missing)} file column(s) have no column in {table} — the file's fields are not "
               f"all loaded: {', '.join(c for c, _, _ in missing)}")
    logger.issue(f"TestCase {test_case_number}: {summary}")
    append_log_entry(log_doc, "ISSUE", "SRC->TGT", summary)
    for col, _, filled in missing:
        msg = (f"Missing in table {table}: file column '{col}' "
               f"({filled} of {len(rows)} row(s) have a value)")
        logger.issue(f"TestCase {test_case_number}: {msg}")
        append_log_entry(log_doc, "ISSUE", "SRC->TGT", msg)
    log_doc.setdefault("MissingColumns", [])
    log_doc["MissingColumns"] += [c for c, _, _ in missing if c not in log_doc["MissingColumns"]]
    return True


def _is_all_fields_check(check_fields) -> bool:
    """CheckFields [{"SourceField": "ALL"}] — whole rows, every field."""
    for f in check_fields or []:
        value = f.get("Field") or f.get("SourceField")
        values = value if isinstance(value, list) else [value]
        if any(str(v).strip().upper() == "ALL" for v in values if v is not None):
            return True
    return False


def _run_full_row_duplicate_check(df, source_def, check_fields, test_case_number, log_doc) -> bool:
    """
    Duplicate_Check with SourceField "ALL": rows that are identical in every
    field, within each file. Position columns an EDI parser adds (SEG_POS,
    OCC …) and file bookkeeping columns are left out, as are any listed in the
    CheckFields entry's "ExcludeFields". Empty and blank values count as equal.
    Each duplicated row is reported once, with how many times it appears, the
    file and the row numbers (data rows, header not counted; segment
    positions for EDI files). Returns True when duplicates are found.
    """
    from Libraries.Regression_Log_Helper import append_log_entry
    from Libraries.File_Table_Mapper import row_position_columns
    file_config = (source_def or {}).get("FileConfig") or {}
    excluded = set(row_position_columns(file_config, df.columns))
    for f in check_fields or []:
        extra = f.get("ExcludeFields") or []
        excluded |= {str(x) for x in (extra if isinstance(extra, list) else [extra])}
    cols = [c for c in df.columns if c not in excluded]
    label = "Duplicate_Check (all fields)"
    if not cols:
        msg = "no fields left to compare after leaving out position columns"
        logger.issue(f"TestCase {test_case_number}: {label} — {msg}.")
        append_log_entry(log_doc, "Error", "Duplicate_Check", msg)
        return True

    def _text(v):
        if v is None or (isinstance(v, float) and v != v):
            return ""
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        return str(v).strip()

    work = df[cols].apply(lambda s: s.map(_text))
    files = df["SourceFile"] if "SourceFile" in df.columns else pd.Series([""] * len(df), index=df.index)
    work["__file"] = files.astype(str).values
    row_no = work.groupby("__file", sort=False).cumcount() + 1           # data row within its file
    is_edi = str(file_config.get("Format") or "").upper().startswith("EDI") and "SEG_POS" in df.columns
    where = df["SEG_POS"] if is_edi else row_no
    where_label = "segment" if is_edi else "row"
    dup_mask = work.duplicated(subset=["__file"] + cols, keep=False)
    msg_fields = f"{len(cols)} field(s)" + (f", leaving out {', '.join(sorted(excluded & set(map(str, df.columns))))}"
                                           if excluded & set(map(str, df.columns)) else "")
    if not dup_mask.any():
        logger.info(f"TestCase {test_case_number}: {label} — no row repeated in all {msg_fields} ✓")
        append_log_entry(log_doc, "Info", "Duplicate_Check", f"No row repeated in all {msg_fields}.")
        return False

    dups = work[dup_mask].copy()
    dups["__where"] = where[dup_mask].values
    groups = list(dups.groupby(["__file"] + cols, sort=False, dropna=False))
    # EDI rows with segments under them (a 271 EB, an 834 member, an 837
    # claim …): copies are only duplicates when everything under them is the
    # same too.
    from Libraries.File_Table_Mapper import has_child_rows, child_signatures
    if source_def is not None and has_child_rows(file_config):
        try:
            sig = pd.Series(child_signatures(source_def, df.loc[dups.index]), index=dups.index)
            refined = []
            for key, grp in groups:
                for _, part in grp.groupby(sig.loc[grp.index], sort=False):
                    if len(part) > 1:
                        refined.append((key, part))
            dropped = len(groups) - len({k for k, _ in refined})
            if dropped:
                logger.info(f"TestCase {test_case_number}: {label} — {dropped} repeated row(s) not reported: "
                            f"the segments under the copies differ.")
            groups = refined
        except Exception as exc:
            logger.warning(f"TestCase {test_case_number}: {label} — segments under the rows not "
                           f"compared ({exc}); reporting the rows alone.")
    if not groups:
        logger.info(f"TestCase {test_case_number}: {label} — no row repeated in all {msg_fields} "
                    f"together with the segments under it ✓")
        append_log_entry(log_doc, "Info", "Duplicate_Check", f"No row repeated in all {msg_fields}.")
        return False
    extra_copies = sum(len(g) for _, g in groups) - len(groups)
    summary = (f"{len(groups)} row(s) appear more than once ({extra_copies} extra cop"
               f"{'y' if extra_copies == 1 else 'ies'}), compared on all {msg_fields}")
    logger.issue(f"TestCase {test_case_number}: {label} — {summary}.")
    append_log_entry(log_doc, "Error", "Duplicate_Check", summary + ".")
    shown = min(len(groups), int(missing))
    if shown < len(groups):
        logger.issue(f"TestCase {test_case_number}: showing the first {shown} of {len(groups)}.")
    for key, grp in groups[:shown]:
        file_name = key[0]
        values = {c: v for c, v in zip(cols, key[1:]) if v != ""}
        positions = ", ".join(str(p) for p in grp["__where"].tolist()[:20]) + (" …" if len(grp) > 20 else "")
        msg = (f"Repeated {len(grp)} times" + (f" in {file_name}" if file_name else "")
               + f" at {where_label}s {positions}: {values}")
        logger.issue(f"TestCase {test_case_number}: {msg}")
        append_log_entry(log_doc, "Error", "Duplicate_Check", msg)
    log_doc.setdefault("MismatchFields", []).append("ALL")
    return True


def _run_duplicate_check_on_file(source_def, check_fields,
                                 test_case_number, test_description, log_doc, rows=None):
    from Libraries.File_parser import fetch_and_parse
    from Libraries.Regression_Log_Helper import append_log_entry
    try:
        if _is_all_fields_check(check_fields):
            # Whole-row duplicates are checked within each file: read with the
            # file name on every row.
            if rows is None and source_def is not None:
                result = fetch_and_parse(source_def, add_source_file_column=True)
            else:
                result = _rows_or_file(source_def, rows)
            df = result["dataframe"]
            if df.empty:
                logger.issue(f"TestCase {test_case_number}: Duplicate_Check — file empty or parse failed.")
                log_doc["Result"] = "Fail"
                return True
            status = _run_full_row_duplicate_check(df, source_def, check_fields, test_case_number, log_doc)
            log_doc["Result"] = "Fail" if status else "Pass"
            return status
        result = _rows_or_file(source_def, rows)
        df = result["dataframe"]
        status = False
        if df.empty:
            logger.issue(f"TestCase {test_case_number}: Duplicate_Check — file empty or parse failed.")
            log_doc["Result"] = "Fail"
            return True
        key_fields = [
            f.get("Field", "") or f.get("SourceField", "")
            for f in (check_fields or [])
            if f.get("Field", "") or f.get("SourceField", "")
        ]
        if not key_fields:
            logger.warning(f"TestCase {test_case_number}: Duplicate_Check — no CheckFields specified.")
            log_doc["Result"] = "Pass"
            return False
        missing_fields = [f for f in key_fields if f not in df.columns]
        if missing_fields:
            logger.issue(f"TestCase {test_case_number}: Duplicate_Check — fields not found: {missing_fields}")
            append_log_entry(log_doc, "Error", "Duplicate_Check", f"Fields not found: {missing_fields}")
            log_doc["Result"] = "Fail"
            return True
        duplicates = df[df.duplicated(subset=key_fields, keep=False)]
        if not duplicates.empty:
            dup_count = len(duplicates)
            logger.issue(
                f"TestCase {test_case_number}: Duplicate_Check — {dup_count} duplicate row(s) on {key_fields}.")
            append_log_entry(log_doc, "Error", "Duplicate_Check", f"{dup_count} duplicate row(s) on {key_fields}.")
            for _, row in duplicates.head(int(missing)).iterrows():
                key_vals = {f: row.get(f) for f in key_fields}
                logger.issue(f"TestCase {test_case_number}: Duplicate — {key_vals}")
            log_doc.setdefault("MismatchFields", []).extend(key_fields)
            status = True
        else:
            logger.info(f"TestCase {test_case_number}: Duplicate_Check — no duplicates on {key_fields} ✓")
            append_log_entry(log_doc, "Info", "Duplicate_Check", f"No duplicates on {key_fields}.")
        log_doc["Result"] = "Fail" if status else "Pass"
        return status
    except Exception as e:
        logger.exception(f"TestCase {test_case_number}: _run_duplicate_check_on_file: {e}")
        append_log_entry(log_doc, "Error", "Duplicate_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
        return True


def parse_multi_value(value):
    if "," in value:
        return {"$in": [v.strip() for v in value.split(",")]}
    return value.strip()


def _render_centered_table(rows, max_colwidth: int = 40) -> list:
    """
    Renders a list of row dicts as an aligned text table with every cell —
    header and value alike — centred within its column.

    pandas' to_string() right-aligns values regardless of the `justify`
    setting (which only affects headers), so the table is built manually.

    Missing keys render as an empty cell rather than NaN, so ragged rows
    (common with PDF anchor extraction) stay readable.

    Returns a list of lines: header, separator, then one line per row.
    """
    if not rows:
        return []

    # Column order: first appearance across all rows, preserving insertion order
    columns = []
    for r in rows:
        for k in r.keys():
            if k not in columns:
                columns.append(k)

    def _cell(value) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        s = str(value)
        return s if len(s) <= max_colwidth else s[:max_colwidth - 1] + "…"

    table = [[_cell(r.get(c)) for c in columns] for r in rows]

    widths = [
        max(len(str(col)), *(len(row[i]) for row in table)) if table
        else len(str(col))
        for i, col in enumerate(columns)
    ]

    lines = ["  ".join(str(c).center(w) for c, w in zip(columns, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in table:
        lines.append("  ".join(v.center(w) for v, w in zip(row, widths)).rstrip())
    return lines


def run_validation(module, stage, cycle, testcase_num,
                   environment=test_data_environment,
                   runtime_filters=None, max_workers=config_max_workers,
                   view_rows=None, view_format="table"):
    Logger_("", module=module)
    logger.info("========== Starting Validation ==========")
    logger.info(f"Module         : {module}")
    logger.info(f"Stage          : {stage}")
    logger.info(f"Cycle          : {cycle}")
    logger.info(f"Environment    : {environment}")
    logger.info(f"Runtime Filters: {runtime_filters or 'None — using DefaultValues'}")
    if view_rows is not None:
        logger.info(f"DataView Rows  : {view_rows} (runtime -v)")
    Regression_inputs(
        module, stage, cycle, testcase_num, environment, runtime_filters,
        view_rows=view_rows, view_format=view_format
    ).run(max_workers=max_workers)
    logger.info("========== Completed Validation ==========")


if __name__ == '__main__':
    try:
        args = argparse_()
        Logger_(" ", module=args.module)
        runtime_filters = parse_runtime_filters(args.filters)
        max_workers = int(args.threads) if args.runtype == 'p' else 1
        run_validation(
            args.module,
            args.stage,
            args.cycle,
            args.testcasenumber,
            test_data_environment,
            runtime_filters,
            max_workers=max_workers,
            view_rows=args.viewrows,
            view_format=args.viewformat
        )
    except Exception as esp:
        logger.exception(f"Unexpected exception in __main__: {esp}")