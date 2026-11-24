
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
from Libraries.Comparison_vice_versa_with_log_info import (compare_data,compare_data_in_process,is_cursor_empty,run_row_count_validation,inapproved_validation,run_null_check,
                                                           run_duplicate_check, run_format_check )
from Libraries.Edi835_validator import ( run_edi835_format_check, run_edi835_balance_check, run_edi835_data_check )
from Libraries.parameter_reading import ( argparse_,  parse_multi_value, resolve_parameters, parse_runtime_filters,  resolve_data_filters,_to_oracle_value,_to_mongo_value )
from Libraries.Regression_Log_Helper import *
from Libraries.multithreading import run_parallel
from Libraries.data_fetcher import get_next_sequence_id, get_data, get_count
from Libraries.parameter_reading import *
from Libraries.send_email_report import *
import logging
from Libraries.Log_Config import Logger_
from Libraries.Read_config import config_read
cr=config_read()
test_data_environment= cr.test_data_environment("REGRESSION_DATA")
config_max_workers = int(cr.max_threads("Max_Threads"))
logger = logging.getLogger(__name__)
 
class Regression_inputs:
    def __init__(self,module_name,stage,cycle,order,environment, runtime_filters=None):
        self._module_name=module_name
        self._stage = stage
        self._cycle = cycle
        self._order = order
        self.env_label=environment
        self._runtime_filters = runtime_filters or {}
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
                       query_type=None, is_target=False, silent = False):
        """
        Applies DataFilters to a query based on QueryType.
        TABLE / VIEW / COLLECTION  → WHERE clause injection (Oracle) or $match filter (MongoDB)
        QUERY / PIPELINE / AGGREGATE / JAVA_SCRIPT → :placeholder substitution in query string
        Special case: query=None with MONGODB → $match filter only (used by EDI835 target path)
         """
        if not data_filters_raw or not source_type:
            return query, {}
        source_type = source_type.upper()
        query_type  = (query_type or "").upper()
     # ── Fix #6 — EDI835 / target-only path: query=None, return $match filter only ──
        if query is None and source_type == "MONGODB":
            resolved = resolve_data_filters(
                data_filters_raw, source_type, self._runtime_filters,
                is_target=is_target
             )
            if resolved:
                # logger.info(f"MongoDB DataFilter — query=None, $match filter resolved: {resolved}")
                pass
            return None, resolved if resolved else {}
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
                continue   # filter not applicable to this side
 
            # Runtime lookup uses the filter's own field name
            default = f.get("DefaultValue", "")
            value   = self._runtime_filters.get(field, "") or default
 
            if value in (None, ""):
                continue
 
            filter_values[field] = value
            # logger.info(
            #     f"DataFilter '{field}': resolved value = '{value}' "
            #     f"({'RuntimeFilter' if self._runtime_filters.get(rt_key) else 'DefaultValue'})"
            #     )
        if not filter_values:
            return query, {}
    # ── Check if query contains :placeholders ────────────────────────────────
        has_placeholders = any(
            f":{field}" in query
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
                        oracle_val     = _to_oracle_value(str(value))
                        modified_query = modified_query.replace(f":{field}", oracle_val)
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
            if query_type in pipeline_types:
                if has_placeholders:
                    modified_query = query
                    for field, value in filter_values.items():
                        modified_query = modified_query.replace(f":{field}", str(value))
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
 
 
    def _build_query_log(self, label, query_display,  source_type,
                         mongo_filter, db, query_type):
        """
        Builds exact query string for logging.
        query_display — original query preserved as multiline for readability.
        query_filtered — substituted/filtered query used for execution.
        """
        if not source_type:
            return None
        source_type = source_type.upper()
        query_type  = (query_type or "").upper()
        if source_type == "MONGODB":
            pipeline_types = ("JAVA_SCRIPT", "PIPELINE", "AGGREGATE")
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
 
        result        = []
        depth         = 0
        context_stack = []   # True = subquery/CTE, False = function call
        i             = 0
 
        while i < len(sql):
            ch = sql[i]
 
            if ch == "(":
                depth += 1
                joined    = "".join(result)
                m         = re.search(r'\b([A-Za-z_]\w*)\s*$', joined)
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
                        result.append("\n\n  ,")   # blank line between CTEs
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

    def _log_data_sample(self, data, side, tc_number):
        """Logs the first Visible_Row_count records after data fetch for verification."""
        if not data or self._visible_row_count <= 0:
            return

        if not isinstance(data, (list,tuple)):
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
            source    = rule.get("Source", {})
            is_edi835 = isinstance(source, list)
            rule["_is_edi835"] = is_edi835
 

            join_fields    = rule.get("JoinFields",    [])
            compare_fields = rule.get("CompareFields", [])
            if is_edi835:
                # EDI835 filter resolved at runtime (uses _runtime_filters)
                rule["_src_filter"] = {}
                rule["_src_proj"]   = None
                rule["_tgt_proj"]   = None
                continue
            source_type = source.get("Type")      if source else None
            source_db   = source.get("DB")        if source else None
            source_q    = source.get("Query")     if source else None
            source_qt   = source.get("QueryType") if source else None
            target      = rule.get("Target")
            target_type = target.get("Type")      if target else None
            target_db   = target.get("DB")        if target else None
            target_q    = target.get("Query")     if target else None
            target_qt   = target.get("QueryType") if target else None
            if isinstance(source_q, list): source_q = " ".join(source_q)
            if isinstance(target_q, list): target_q = " ".join(target_q)
            data_filters_raw = rule.get("DataFilters", [])
            try:
                sq_filtered, src_filter = self._apply_data_filter(
                    source_q, source_db, source_type,
                    data_filters_raw, query_type=source_qt, is_target=False, silent = True
                )
            except Exception as e:
                logger.warning(f"TC{rule.get('TestCaseNumber')} source filter prep failed: {e}")
                sq_filtered, src_filter = source_q, {}
            try:
                tq_filtered, tgt_filter = self._apply_data_filter(
                    target_q, target_db, target_type,
                    data_filters_raw, query_type=target_qt, is_target=True, silent = True
                )
            except Exception as e:
                logger.warning(f"TC{rule.get('TestCaseNumber')} target filter prep failed: {e}")
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
            rule["_src_filter"]  = src_filter
            rule["_tq_filtered"] = tq_filtered
            rule["_tgt_filter"]  = tgt_filter
            rule["_src_proj"]    = src_proj
            rule["_tgt_proj"]    = tgt_proj
 
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
        module_name     = rule.get("Module")
        testCaseNumber  = rule.get("TestCaseNumber")
        testDescription = rule.get("TestDescription")
        validationType  = rule.get("ValidationType")
 
        logger.info(f"========== [ Running TestCase {testCaseNumber}: {testDescription} ] ==========")
        start_time = time.time()
        log_doc    = create_log_doc(testCaseNumber, testDescription)
 
       
        log_doc["DataValidationLog_ID"] = log_id_by_module.get(module_name)
        log_doc["Module"]               = module_name
        log_doc["Stage"] = rule.get("Stage",None)
 
        source          = rule.get("Source", {})
        is_edi835 = isinstance(source, list)
 
        if is_edi835:
            sourceType        = "EDI835"
            sourceEnvironment = None
            sourceDB          = None
            sourceQuery       = None
            sourceQueryType   = None
            source_steps      = source   # list of step dicts
        else:
            sourceType        = source.get("Type")        if source else None
            sourceEnvironment = source.get("Environment") if source else None
            sourceDB          = source.get("DB")          if source else None
            sourceQuery       = source.get("Query")       if source else None
            sourceQueryType   = source.get("QueryType")   if source else None
            source_steps      = None
 
        target          = rule.get("Target")          if source else None
        targetEnvironment = target.get("Environment") if target else None
        targetDB        = target.get("DB")            if target else None
        targetType      = target.get("Type")          if target else None
        targetQuery     = target.get("Query")         if target else None
        targetQueryType = target.get("QueryType")     if target else None
 
        joinFields    = rule.get("JoinFields")
        compareFields = rule.get("CompareFields")
        expectedResult = rule.get("ExpectedResult")
        viceversa     = rule.get("Viceversa")
        source_data   = None
        target_data   = None
 
        test_case_status = False
        sourceEnvKey = f"{sourceEnvironment}" if sourceEnvironment else ""
        targetEnvKey = f"{targetEnvironment}" if targetEnvironment else ""
 
        start_time = time.time()
 
        
        # ── Normalise for execution (space join) BEFORE DataFilters ───
        if isinstance(sourceQuery, list):
            sourceQuery = " ".join(sourceQuery)
        if isinstance(targetQuery, list):
            targetQuery = " ".join(targetQuery)
 
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
                    continue   # target-only filter — skip for source
                default   = f.get("DefaultValue", "")
                value     = self._runtime_filters.get(src_field, "") or default
 
                if src_field and value:
                    source_mongo_filter[src_field] = _to_mongo_value(str(value))
                    sourceQuery_filtered = sourceQuery
 
        else:                                              # ← ADD THIS
 
            sourceQuery_filtered, source_mongo_filter = self._apply_data_filter(
            sourceQuery, sourceDB, sourceType, data_filters_raw,
            query_type=sourceQueryType, is_target=False
            )
 
            targetQuery_filtered, target_mongo_filter = self._apply_data_filter(
            targetQuery, targetDB, targetType, data_filters_raw,
            query_type=targetQueryType, is_target=True
            )
 
        if is_edi835:
            for step in (source_steps or []):
                step_num  = step.get("Step")
                step_type = step.get("Type", "")
                step_info = (f"Step {step_num} [{step_type}]: "
                                f"Query={step.get('Query','')} "
                                f"OutputKey={step.get('OutputKey','')} "
                                f"InputKey={step.get('InputKey','')}"
                            ).strip()
            logger.info(f"TestCase {testCaseNumber}: Source {step_info}")
            append_log_entry(log_doc, "Info", "Query", f"Source {step_info}")
 
        # ── Keep display versions (multiline) for logging ─────────────
        sourceQuery_display = self._format_query_for_display(sourceQuery_filtered)
        re.sub(r' {2,}','',sourceQuery_display) 
        targetQuery_display = self._format_query_for_display(targetQuery_filtered)
 
 
        # ── Log exact queries — multiline for readability ─────────────
        src_log = self._build_query_log(
            "Source", sourceQuery_display,
            sourceType, source_mongo_filter, sourceDB, sourceQueryType
        )
        if src_log:
            # for line in src_log.split("\n"):
            if src_log.strip():
                logger.info(f"TestCase {testCaseNumber}: {src_log}")
                #logger.info(f"{line}")
            append_log_entry(log_doc, "Info", "Query", src_log)
 
        if targetQuery:
            tgt_log = self._build_query_log(
                "Target", targetQuery_display,
                targetType, target_mongo_filter, targetDB, targetQueryType
            )
            if tgt_log:
                #for line in tgt_log.split("\n"):
                if tgt_log.strip():
                    logger.info(f"TestCase {testCaseNumber}: {tgt_log}")
                    #logger.info(f"{line}")
                append_log_entry(log_doc, "Info", "Query", tgt_log)
 
        # ── Validation type branching ─────────────────────────────────
        if validationType == "SRC_TGT":
            src_q = sourceQuery_filtered if sourceType and sourceType.upper() == "ORACLE" else sourceQuery
            tgt_q = targetQuery_filtered if targetType and targetType.upper() == "ORACLE" else targetQuery
            source_data, target_data = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                targetType, targetDB, tgt_q, targetEnvKey,
                sourceQueryType, targetQueryType,
                source_mongo_filter,
                target_mongo_filter
            )
            if is_cursor_empty(source_data, "source_data") and is_cursor_empty(target_data, "target_data"):
                logger.warning("Source and Target data both are empty. Skipping comparison.")
                append_log_entry(log_doc, "Warning", "SRC->TGT",
                                 "Source and Target data both are empty. Skipping.")
                log_doc["Result"] = "Skip"
            elif is_cursor_empty(source_data, "source_data") and not is_cursor_empty(target_data, "target_data"):
                logger.issue("Source data empty and Target having data. Please check SRC.")
                append_log_entry(log_doc, "ISSUE", "SRC->TGT",
                                 "Source data empty and Target having data. Please check SRC.")
                test_case_status = True
            elif is_cursor_empty(target_data, "target_data") and not is_cursor_empty(source_data, "source_data"):
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
                    future = self._compare_executor.submit(
                        compare_data_in_process,
                        source_data, target_data, sourceType, targetType,
                        joinFields, compareFields, testCaseNumber, testDescription,
                        viceversa
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
 
        elif validationType == "Record_Count":
 
            src_q = sourceQuery_filtered if sourceType and sourceType.upper() == "ORACLE" else sourceQuery
            tgt_q = targetQuery_filtered if targetType and targetType.upper() == "ORACLE" else targetQuery
 
            source_data, target_data = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                targetType, targetDB, tgt_q, targetEnvKey,
                sourceQueryType, targetQueryType,
                source_mongo_filter,
                target_mongo_filter
            )
 
            if source_data is None and target_data is None:
                # msg = f"Error fetching data. Cannot evaluate test case."
                # logger.issue(msg)
                # append_log_entry(log_doc,"ISSUE","Record_Count", msg)
                test_case_status = True
            else:
                source_data = list(source_data) if isinstance(source_data, Cursor) else (source_data if isinstance(source_data, list) else None)
                target_data = list(target_data) if isinstance(target_data, Cursor) else (target_data if isinstance(target_data, list) else None)
                source_count   = len(source_data) if source_data else 0
                target_count   = len(target_data) if target_data else 0
                approvedValues = int(rule.get("ApprovedValues")[0])
                source_data = self._log_data_sample(source_data, "Source", testCaseNumber) or source_data
                target_data = self._log_data_sample(target_data, "Target", testCaseNumber) or target_data

                test_case_status = run_row_count_validation(
                    source_count, target_count, approvedValues,
                    source_data, target_data,
                    testCaseNumber, testDescription, log_doc
                )
 
 
        elif validationType == "InApprovedValue":
 
            src_q = sourceQuery_filtered if sourceType and sourceType.upper() == "ORACLE" else sourceQuery
 
            source_data, _ = get_data(
                sourceType, sourceDB, src_q, sourceEnvKey,
                targetType, targetDB, targetQuery, targetEnvKey,
                sourceQueryType, targetQueryType,
                source_mongo_filter,
                {}
            )
 
            approved_values  = rule.get("ApprovedValues", [])
            self._log_data_sample(source_data, "Source", testCaseNumber) if source_data else 0
            test_case_status = inapproved_validation(
                source_data, approved_values, compareFields,
                testCaseNumber, testDescription, log_doc
            )
 
        elif validationType == "Null_Check":
            check_fields     = rule.get("CheckFields", [])
            test_case_status = run_null_check(
                sourceType, sourceDB, sourceQuery,
                sourceEnvKey, sourceQueryType,
                check_fields, testCaseNumber, testDescription,
                log_doc, source_mongo_filter
            )
 
        elif validationType == "Duplicate_Check":
            check_fields     = rule.get("CheckFields", [])
            test_case_status = run_duplicate_check(
                sourceType, sourceDB, sourceQuery,
                sourceEnvKey, sourceQueryType,
                check_fields, testCaseNumber, testDescription,
                log_doc, source_mongo_filter
            )
 
        elif validationType == "Format_Check":
            check_fields     = rule.get("CheckFields", [])
            test_case_status = run_format_check(
                sourceType, sourceDB, sourceQuery,
                sourceEnvKey, sourceQueryType,
                check_fields, testCaseNumber, testDescription,
                log_doc, source_mongo_filter
            )
 
        elif validationType == "EDI835_Format":
            check_fields = rule.get("CheckFields", [])
            test_case_status = run_edi835_format_check(
                source_steps     = source_steps,
                data_filters     = source_mongo_filter,
                runtime_filters  = self._runtime_filters,
                check_fields     = check_fields,
                test_case_number = testCaseNumber,
                test_description = testDescription,
                log_doc          = log_doc,
                visible_row_count=self._visible_row_count
                )
 
        elif validationType == "EDI835_Balance":
            check_fields = rule.get("CheckFields", [])
            test_case_status = run_edi835_balance_check(
                source_steps     = source_steps,
                data_filters     = source_mongo_filter,
                runtime_filters  = self._runtime_filters,
                check_fields     = check_fields,
                test_case_number = testCaseNumber,
                test_description = testDescription,
                log_doc          = log_doc,
                visible_row_count= self._visible_row_count
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
                source_steps         = source_steps,
                data_filters         = source_mongo_filter,
                target_mongo_filter  = edi835_target_mongo_filter,
                target_oracle_filter = edi835_target_oracle_filter,
                runtime_filters      = self._runtime_filters,
                target               = rule.get("Target"),
                targets              = rule.get("Targets", []),
                test_case_number     = testCaseNumber,
                test_description     = testDescription,
                log_doc              = log_doc,
                viceversa            = viceversa,
                visible_row_count = self._visible_row_count
            )
 
 
        else:
            msg = f"Validation Logic is not Implemented for {validationType}"
            logger.warning(msg)
            append_log_entry(log_doc, "Warning", "Validation", msg)
            log_doc["Result"] = "Fail"
            test_case_status  = True
 
        # ── Per test case wrap-up ─────────────────────────────────────
        #result = log_doc.get("Result","")
        end_time       = time.time()
        execution_time = end_time - start_time
 
        logger.info(f"Execution time for TestCase {testCaseNumber}: {execution_time:.2f} seconds")
 
        log_doc["MismatchFields"] = log_doc.get("MismatchFields", [])
        log_doc["MissingFields"]  = log_doc.get("MissingFields", [])
 
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
            counters["times"].append({"TimeSecs": round(execution_time,2)})
			
    def run(self, max_workers=config_max_workers):
        """
        Main validation execution loop.
        """
        try:
            #pass_count      = 0
            #fail_count      = 0
            #skipped_count   = 0
            #test_logs       = []
            #execution_times = []
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
            unique_modules   = {r.get("Module") for r in self.rules if r.get("Module")}
            log_id_by_module = {m: get_next_sequence_id(self.env_label) for m in unique_modules}
 
            _wall_start = time.time()
            counters        = run_parallel(self.rules, self._run_single, log_id_by_module=log_id_by_module, max_workers= max_workers)
            wall_time = round(time.time() - _wall_start,2)
            self.test_logs.sort(key=lambda t:int(t.get("TestCaseID",0)))
            pass_count      = counters["pass"]
            fail_count      = counters["fail"]
            skipped_count   = counters["skipped"]
            execution_times = counters["times"]
            # ── Post-loop summary ─────────────────────────────────────────────
            logger.info(
                f"Total Number of Testcases Executed : {len(self.rules)},"
                f"Pass : {pass_count}, Fail : {fail_count}, Skippped : {skipped_count}"
            )
            #total_time = sum(t["TimeSecs"] for t in execution_times)
            total_time = wall_time
            logger.info(
                f"Total execution time: {total_time:.2f} seconds "
                f"({total_time/60:.1f} minutes)"
            )
            consolidated_log_path = write_master_log_to_file(
                self.log_collection, self.env_label,
                self._module_name, self._stage, self._cycle, self.test_logs
            )
 
 
            path       = os.path.join(scripts_path, "Config", "email_config.xml")
            config     = read_email_config_from_ini()
            recipients = [r.strip() for r in config.get("email_to", "").split(",") if r]
            rows = [{
                "Environment"         : self.env_label,
                "DataValidationLog_ID": log.get("DataValidationLog_ID"),
                "Module"              : log.get("Module"),
                "Stage"               : self._stage or "",
                "Cycle"               : self._cycle or "",
                "TestCaseID"          : log.get("TestCaseID"),
                "TestCaseName"        : log.get("TestCaseName"),
                "StartTime"           : log.get("StartTime"),
                "EndTime"             : log.get("EndTime"),
                "Result"              : log.get("Result"),
                "MismatchFields"      : ", ".join(log.get("MismatchFields", [])),
                "MissingFields"       : ", ".join(log.get("MissingFields", []))
            } for log in self.test_logs]
            base_name = os.path.splitext(os.path.basename(consolidated_log_path))[0]
            send_email_report(
                recipients      = recipients,
                subject         = f"Validation Report - {base_name}",
                master_log_rows = rows,
                attachment_path = consolidated_log_path,
                sender_email    = config["email_from"],
                smtp_server     = config["email_host"],
                report_type     = "both"
            )
 
        except Exception as e:
            logger.exception(f"Exception occurred during regression validation run: {e}")
            raise

def parse_multi_value(value):
    if "," in value:
        return {"$in": [v.strip() for v in value.split(",")]}
    return value.strip()

def run_validation(module, stage, cycle, testcase_num,
                   environment=test_data_environment,
                   runtime_filters=None, max_workers=config_max_workers):
    Logger_("", module=module)
    logger.info("========== Starting Validation ==========")
    logger.info(f"Module         : {module}")
    logger.info(f"Stage          : {stage}")
    logger.info(f"Cycle          : {cycle}")
    logger.info(f"Environment    : {environment}")
    logger.info(f"Runtime Filters: {runtime_filters or 'None — using DefaultValues'}")
    Regression_inputs(
        module, stage, cycle, testcase_num, environment, runtime_filters
    ).run(max_workers = max_workers)
    logger.info("========== Completed Validation ==========")

if __name__ == '__main__':
    try:
        args            = argparse_()
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
            max_workers = max_workers
        )
    except Exception as esp:
        logger.exception(f"Unexpected exception in __main__: {esp}")			