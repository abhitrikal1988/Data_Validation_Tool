import json
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
 
def get_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
             targetType, targetDB, targetQuery, targetEnvKey,
             sourceQueryType, targetQueryType,
             source_filter=None, target_filter=None,
             src_projection= None, tgt_projection=None):
    """
    Fetches data from source and target.
    Query can be a string or list of strings — list joined with space.
    """
    source_data, target_data = None, None
    sourceEnvKey  = sourceEnvKey.upper()
    targetEnvKey  = targetEnvKey.upper()
    source_filter = source_filter or {}
    target_filter = target_filter or {}
 
    # ── Normalise query — list → string joined with space
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
    if isinstance(targetQuery, list):
        targetQuery = " ".join(targetQuery)
 
    try:
        if sourceType and sourceType.upper() == "MONGODB":
            if sourceQueryType.upper() == "VIEW" or sourceQueryType.upper() == "Collection":
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = (source_view.getDataView(source_filter, src_projection, batch_size = 0) if src_projection else source_view.getData(source_filter, batch_size = 0))
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                execute_js_from_query(sourceQuery, 125)
 
        elif sourceType and sourceType.upper() == "ORACLE":
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery).lower():
                if "." in sourceQuery:
                    sourceQuery = "Select * from " + sourceQuery
                else:
                    sourceQuery = "Select * from " + sourceDB + "." + sourceQuery
            source_data = source_connection.fetchAllDict(sourceQuery)
 
    except Exception as e:
        logger.issue(f"Error fetching source data ({sourceType}): {e}")
 
    try:
        if targetType and targetType.upper() == "MONGODB":
            target_view = Collection(targetDB, targetQuery, targetEnvKey)
            target_data = (target_view.getDataView(target_filter,tgt_projection, batch_size = 0) if tgt_projection else target_view.getData(target_filter, batch_size = 0))
 
        elif targetType and targetType.upper() == "ORACLE":
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery).lower():
                if "." in targetQuery:
                    targetQuery = "Select count(*) from " + targetQuery
                else:
                    targetQuery = "Select count(*) from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchAllDict(targetQuery)
 
    except Exception as e:
        logger.issue(f"Error fetching target data ({targetType}): {e}")
 
    return source_data, target_data
 
 
def get_count(sourceType, sourceDB, sourceQuery, sourceEnvKey,
              targetType, targetDB, targetQuery, targetEnvKey,
              sourceQueryType, targetQueryType,
              source_filter=None, target_filter=None):
    """
    Fetches counts from source and target.
    Query can be a string or list of strings — list joined with space.
    """
    source_data, target_data = None, None
    sourceEnvKey  = sourceEnvKey.upper()
    targetEnvKey  = targetEnvKey.upper()
    source_filter = source_filter or {}
    target_filter = target_filter or {}
 
    # ── Normalise query — list → string joined with space
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
    if isinstance(targetQuery, list):
        targetQuery = " ".join(targetQuery)
 
    try:
        if sourceType and sourceType.upper() == "MONGODB":
            if sourceQueryType.upper() == "VIEW" or sourceQueryType.upper() == "Collection":
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = source_view.getDataCount(source_filter)
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                execute_js_from_query(sourceQuery, 101)
 
        elif sourceType and sourceType.upper() == "ORACLE":
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery).lower():
                if "." in sourceQuery:
                    sourceQuery = "Select count(*) from " + sourceQuery
                else:
                    sourceQuery = "Select count(*) from " + sourceDB + "." + sourceQuery
            source_data = source_connection.fetchOne(sourceQuery)
 
    except Exception as e:
        logger.issue(f"Error fetching source data ({sourceType}): {e}")
 
    try:
        if targetType and targetType.upper() == "MONGODB":
            target_view = Collection(targetDB, targetQuery, targetEnvKey)
            target_data = target_view.getDataCount(target_filter)
 
        elif targetType and targetType.upper() == "ORACLE":
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery).lower():
                if "." in targetQuery:
                    targetQuery = "Select count(*) from " + targetQuery
                else:
                    targetQuery = "Select count(*) from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchOne(targetQuery)
 
    except Exception as e:
        logger.issue(f"Error fetching target data ({targetType}): {e}")
 
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
 
 
def get_null_data(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                  sourceQueryType, check_fields, data_filters=None):
    """
    Fetches null records per field.
 
    Oracle  : single query with all fields in one OR condition — one round trip.
              Result stored under each field key for individual reporting.
    MongoDB : per-field loop with .limit(missing) cap — keeps large collections fast.
 
    Returns dict: { field_name: cursor_or_list_or_None }
    """
    sourceEnvKey = sourceEnvKey.upper()
    data_filters = data_filters or {}
    results      = {}
 
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
 
    # ── Oracle — single query for all fields ──────────────────────────────────
    if sourceType and sourceType.upper() == "ORACLE":
        try:
            source_connection = OracleConnection(sourceEnvKey)
 
            if "select " in sourceQuery.lower():
                table_ref = f"({sourceQuery})"
            elif "." in sourceQuery:
                table_ref = sourceQuery
            else:
                table_ref = f"{sourceDB}.{sourceQuery}"
 
            # Collect all result fields and null conditions across all check_fields
            all_result_fields = []
            null_conditions   = []
 
            for field_def in check_fields:
                field         = field_def.get("SourceField", "")
                result_fields = field_def.get("ResultFields", [])
 
                if not field:
                    continue
 
                if isinstance(result_fields, str):
                    result_fields = [result_fields]
                if field not in result_fields:
                    result_fields = [field] + result_fields
 
                for rf in result_fields:
                    if rf not in all_result_fields:
                        all_result_fields.append(rf)
 
                null_conditions.append(
                    f"({field} IS NULL OR TRIM(TO_CHAR({field})) = '')"
                )
 
            if not null_conditions:
                return results
 
            null_where   = " OR ".join(null_conditions)
            where_clause = (
                f"{data_filters} AND ({null_where})"
                if data_filters else null_where
            )
 
            select_cols = ", ".join(all_result_fields)
            final_query = (
                f"SELECT {select_cols} FROM {table_ref} "
                f"WHERE {where_clause} AND ROWNUM <= {int(missing)}"
            )
 
            logger.info(f"Source [ORACLE]: {final_query}")
            oracle_data = source_connection.fetchAllDict(final_query)
 
            # Store same result list under each field key for individual reporting
            for field_def in check_fields:
                field = field_def.get("SourceField", "")
                if field:
                    results[field] = oracle_data
 
        except Exception as e:
            logger.issue(f"Error fetching null data (ORACLE): {e}")
            for field_def in check_fields:
                field = field_def.get("SourceField", "")
                if field:
                    results[field] = None
 
    # ── MongoDB — per-field loop with .limit() cap ────────────────────────────
    elif sourceType and sourceType.upper() == "MONGODB":
        for field_def in check_fields:
            field         = field_def.get("SourceField", "")
            result_fields = field_def.get("ResultFields", [])
 
            if not field:
                continue
 
            if isinstance(result_fields, str):
                result_fields = [result_fields]
            if field not in result_fields:
                result_fields = [field] + result_fields
 
            try:
                if sourceQueryType.upper() in ("VIEW", "COLLECTION"):
 
                    source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
 
                    # None catches both explicit null and missing fields
                    null_conditions = [{field: None}, {field: ""}]
 
                    mongo_filter = (
                        {"$and": [data_filters, {"$or": null_conditions}]}
                        if data_filters
                        else {"$or": null_conditions}
                    )
 
                    projection = {"_id": 0}
                    for rf in result_fields:
                        projection[rf.split(".")[0]] = 1
 
                    logger.info(
                        f"Source [MONGODB]: db.{sourceQuery}.find(\n"
                        f"{json.dumps(mongo_filter, indent=2, default=str)}\n)"
                        f".limit({int(missing)})"
                    )
 
                    results[field] = source_view.getDataView(
                        mongo_filter, projection
                    ).limit(int(missing))
 
            except Exception as e:
                logger.issue(f"Error fetching null data for '{field}' (MONGODB): {e}")
                results[field] = None
 
    return results
 
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
    results      = {}
    data_filters = data_filters or {}
    max_records  = int(missing)
 
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
 
    for field_def in check_fields:
        src_field     = field_def.get("SourceField", "")
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
 
                select_cols   = ", ".join(result_fields)
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
                    f"{sample_query.strip()}"
                )
 
                sample_data = source_connection.fetchAllDict(sample_query)
 
                if not sample_data:
                    results[field_label] = {
                        "sample"     : [],
                        "total_count": 0,
                        "total_recs" : 0,
                        "capped"     : False
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
                    f"{count_query.strip()}"
                )
 
                count_result = source_connection.fetchAllDict(count_query)
                total_count  = (
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
                total_recs  = (
                    recs_result[0].get("REC_COUNT", 0)
                    if recs_result else 0
                )
 
                results[field_label] = {
                    "sample"     : sample_data,
                    "total_count": total_count,
                    "total_recs" : total_recs,
                    "capped"     : total_count >= COUNT_CAP
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
                        top_key           = rf.split(".")[0]
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
                                "_id"  : group_id,
                                "count": {"$sum": 1},
                                "docs" : {"$push": push_doc}
                            }
                        },
                        {"$match": {"count": {"$gt": 1}}},
                        {"$sort" : {"count": -1}},
                        {"$limit": max_records}
                    ]
 
                    logger.info(
                        f"Source [MONGODB PIPELINE] Duplicate_Check sample "
                        f"for '{field_label}':\n"
                        f"db.{sourceQuery}.aggregate(\n"
                        f"{json.dumps(sample_pipeline, indent=2, default=str)}\n)"
                    )
 
                    sample_data = list(source_col.getPipeData(sample_pipeline))
 
                    if not sample_data:
                        results[field_label] = {
                            "sample"     : [],
                            "total_count": 0,
                            "total_recs" : 0,
                            "capped"     : False
                        }
                        continue
 
                    # ── Count pipeline — exact dup value + record counts ───────
                    count_pipeline = [
                        {"$match": {"$and": match_conditions}},
                        {
                            "$group": {
                                "_id"  : group_id,
                                "count": {"$sum": 1}
                            }
                        },
                        {"$match": {"count": {"$gt": 1}}},
                        {
                            "$group": {
                                "_id"     : None,
                                "dup_vals": {"$sum": 1},
                                "dup_recs": {"$sum": "$count"}
                            }
                        }
                    ]
 
                    logger.info(
                        f"Source [MONGODB PIPELINE] Duplicate_Check count "
                        f"for '{field_label}':\n"
                        f"db.{sourceQuery}.aggregate(\n"
                        f"{json.dumps(count_pipeline, indent=2, default=str)}\n)"
                    )
 
                    count_result = list(source_col.getPipeData(count_pipeline))
                    total_count  = (
                        count_result[0].get("dup_vals", 0)
                        if count_result else 0
                    )
                    total_recs   = (
                        count_result[0].get("dup_recs", 0)
                        if count_result else 0
                    )
 
                    results[field_label] = {
                        "sample"     : sample_data,
                        "total_count": total_count,
                        "total_recs" : total_recs,
                        "capped"     : total_count >= COUNT_CAP
                    }
 
        except Exception as e:
            logger.issue(
                f"Error fetching duplicate data for '{field_label}' ({sourceType}): {e}"
            )
            results[field_label] = None
 
    return results

FORMAT_DATE_REGEX_MAP = {
    "UTC"        : r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
    "EST"        : r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
    "YYYY-MM-DD" : r"^\d{4}-\d{2}-\d{2}$",
    "MM/DD/YYYY" : r"^\d{2}/\d{2}/\d{4}$",
    "DD/MM/YYYY" : r"^\d{2}/\d{2}/\d{4}$",
    "YYYY/MM/DD" : r"^\d{4}/\d{2}/\d{2}$",
    "MM-DD-YYYY" : r"^\d{2}-\d{2}-\d{4}$",
    "YYYYMMDD"   : r"^\d{8}$",
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
            parts     = (value or "").split("-")
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
            parts     = (value or "").split("-")
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
    seen   = set()
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
    results      = {}
    data_filters = data_filters or {}
    max_records  = int(missing)
 
    # ── Normalise query — list → string joined with space
    if isinstance(sourceQuery, list):
        sourceQuery = " ".join(sourceQuery)
 
    for field_def in check_fields:
        field         = field_def.get("SourceField", "")
        format_type   = field_def.get("FormatType", "")
        pattern       = field_def.get("Pattern", "")
        value         = field_def.get("Value", "")
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
 
                select_cols  = ", ".join(result_fields)
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
                    f"{sample_query.strip()}"
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
                    f"{count_query.strip()}"
                )
 
                count_result = source_connection.fetchAllDict(count_query)
                raw_count    = (
                    count_result[0].get("FAIL_COUNT", len(sample_data))
                    if count_result else len(sample_data)
                )
                capped = raw_count >= COUNT_CAP
 
                results[field] = {
                    "sample"      : sample_data,
                    "total_count" : raw_count,
                    "capped"      : capped
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
                        f"{json.dumps(mongo_filter, indent=2, default=str)}\n)"
                    )
 
                    source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
 
                    sample_data = list(
                        source_view.getDataView(mongo_filter, projection).limit(max_records)
                    )
 
                    if not sample_data:
                        results[field] = {"sample": [], "total_count": 0, "capped": False}
                        continue
 
                    total_count = source_view.getDataCount(mongo_filter)
                    capped      = total_count >= COUNT_CAP
 
                    results[field] = {
                        "sample"      : sample_data,
                        "total_count" : total_count,
                        "capped"      : capped
                    }
 
        except Exception as e:
            logger.issue(
                f"Error fetching format check data for '{field}' ({sourceType}): {e}"
            )
            results[field] = None
 
    return results