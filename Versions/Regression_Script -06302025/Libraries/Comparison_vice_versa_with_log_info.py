import pdb

import pandas as pd
from datetime import datetime
import logging
from pymongo.cursor import Cursor
from Libraries.Regression_Log_Helper import append_log_entry
from collections import defaultdict
from .Read_config import config_read , Timezone
cr= config_read()

logger = logging.getLogger()

mismatch=cr.mismatch_count("No_Of_Records_Display")
missing=cr.missing_count("No_Of_Records_Display")

def is_cursor_empty(data,name=""):
    try:
        # logger.debug(f"Checking if data '{name}' is empty.")

        if isinstance(data, Cursor):
            # Safe way to peek into MongoDB cursor
            first_item = next(data.clone(), None)
            if first_item is None:
                logger.info(f"MongoDB cursor '{name}' is empty.")
                return True
            else:
                # logger.info(f"MongoDB cursor '{name}' is NOT empty.")
                return False

        elif isinstance(data, list):
            if not data:
                logger.info(f"Oracle data '{name}' is an empty list.")
                return True
            else:
                # logger.info(f"Oracle data '{name}' has {len(data)} records.")
                return False

        else:
            logger.warning(f"Unknown data type for '{name}': {type(data)}. Assuming not empty.")
            return False
    except Exception as e:
        logger.error(f"Error while checking if cursor '{name}' is empty: {e}")
        return True  # Treat as empty on failure


def compare_data(source_data, target_data, sourceType, targetType,join_fields, compare_fields,testCaseNumber,testDescription,viceversa=False,log_doc=None):
    # logger.info("Starting data comparison between source and target.")

    def try_parse_date(val):
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str):
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(val, fmt).date()
                    # return datetime.strptime(val, fmt).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
        return val

    def sort_dataframe(df, keys):
        missing_keys = [k for k in keys if k not in df.columns]
        if missing_keys:
            # logger.error(f"[sort_dataframe] Missing columns: {missing_keys}")
            # logger.debug(f"[sort_dataframe] DataFrame columns: {df.columns.tolist()}")
            return df  # or raise Exception if you want to stop execution

        return df.sort_values(by=keys).reset_index(drop=True)

    def run_comparison(src_df, tgt_df,sourceType, targetType, join_fields, field_map, direction="SRC->TGT"):

        logger.info(f"[{direction}]  - Total source {sourceType} records: {len(src_df)}")
        logger.info(f"[{direction}]  - Total target {targetType} records: {len(tgt_df)}")

        src_keys = [j["SourceField"] for j in join_fields]
        tgt_keys = [j["TargetField"] for j in join_fields]

        # src_keys, tgt_keys = [], []
        # for join in join_fields:
        #     for k, v in join.items():
        #         if k.startswith("SRC"):
        #             src_keys.append(v)
        #         elif k.startswith("TGT"):
        #             tgt_keys.append(v)

        tgt_df_renamed = tgt_df.rename(columns={tgt: src for src, tgt in zip(src_keys, tgt_keys)})
        src_df = sort_dataframe(src_df, src_keys)
        tgt_df_renamed = sort_dataframe(tgt_df_renamed, src_keys)

        merged = pd.merge(src_df, tgt_df_renamed, on=src_keys, how='outer', suffixes=('_src', '_tgt'), indicator=True)
        differences_found = False
        difference_count = 0
        missing_count=0
        mismatch_log_counts = defaultdict(int)
        for _, row in merged.iterrows():
            join_key_vals = {k: row.get(k) for k in src_keys}


            if row['_merge'] == 'left_only':
                if direction == "TGT->SRC":
                    if int(missing_count) < int(missing):
                        msg = f"[{direction}] Missing in Source: {join_key_vals}"
                        logger.error(msg)
                        if log_doc:
                            append_log_entry(log_doc, "Error", direction, msg)
                            if "MissingFields" not in log_doc:
                                log_doc["MissingFields"] = []
                            for k in join_key_vals:
                                if k not in log_doc["MissingFields"]:
                                    log_doc["MissingFields"].append(k)
                    missing_count += 1
                    differences_found = True

            # elif row['_merge'] == 'right_only':
                if direction == "SRC->TGT":
                    if int(missing_count) < int(missing):
                        msg = f"[{direction}] Missing in Target: {join_key_vals}"
                        logger.error(msg)
                        if log_doc:
                            append_log_entry(log_doc, "Error", direction, msg)
                            if "MissingFields" not in log_doc:
                                log_doc["MissingFields"] = []
                            for k in join_key_vals:
                                if k not in log_doc["MissingFields"]:
                                    log_doc["MissingFields"].append(k)
                    missing_count+=1
                    differences_found = True
            else:
                for f in field_map:
                    s_field = f["SourceField"]
                    t_field = f["TargetField"]
                    s_val = try_parse_date(row.get(f"{s_field}_src"))
                    t_val = try_parse_date(row.get(f"{s_field}_tgt"))
                    # s_field, t_field = None, None
                    # for k, v in f.items():
                    #     if k.startswith("SRC"):
                    #         s_field = v
                    #     elif k.startswith("TGT"):
                    #         t_field = v

                    # s_val = try_parse_date(row.get(s_field))
                    # t_val = try_parse_date(row.get(t_field))

                    if pd.isna(s_val) and pd.isna(t_val):
                        continue
                    if s_val != t_val:
                        difference_count+=1
                        if int(mismatch_log_counts[s_field]) < int(mismatch):
                            msg = f"Mismatch at {join_key_vals}: {s_field}={s_val} != {t_field}={t_val}"
                            logger.error(f"[{direction}] {msg}")
                            if log_doc:
                                append_log_entry(log_doc, "Error", direction, msg)
                                if "MismatchFields" not in log_doc:
                                    log_doc["MismatchFields"] = []
                                if s_field not in log_doc["MismatchFields"]:
                                    log_doc["MismatchFields"].append(s_field)
                            mismatch_log_counts[s_field] += 1
                        differences_found = True

        # total_tgt_missing = merged['_merge'].value_counts().get('left_only', 0)
        # total_src_missing= merged['_merge'].value_counts().get('right_only', 0)
        # logger.info("Summary:")
        # logger.info(f"[{direction}]  - Total source records: {len(src_df)}")
        # logger.info(f"[{direction}]  - Total target records: {len(tgt_df)}")
        # if direction =="TGT->SRC":
        #     logger.error(f" [{direction}]  - Source Missing Records: {total_src_missing}")
        #     logger.error(f"[{direction}]  - Target Missing Records: {total_tgt_missing}")
        # logger.info("Data match successful!" if difference_count == 0 else "Differences detected.")
        # logger.error(f"[{direction}]  - Total Number Of Difference Records: {difference_count}")
        append_log_entry(log_doc, "Info", direction, f"Total Mismatches: {difference_count}")
        append_log_entry(log_doc, "Info", direction, f"Total Missing Records: {missing_count}")
        return differences_found

        # logger.info(f"[{direction}] Comparison completed.")

    # Main forward comparison
    src_df = pd.DataFrame(source_data)
    tgt_df = pd.DataFrame(target_data)
    # logger.info("Source and Target dataframes created.")
    result_forward =run_comparison(src_df, tgt_df,sourceType, targetType, join_fields, compare_fields, direction="SRC->TGT")
    result_reverse = False

    # Vice-versa comparison if enabled
    if viceversa:
        reversed_joins = [
            {"SourceField": j["TargetField"], "TargetField": j["SourceField"]}
            for j in join_fields
        ]
        reversed_fields = [
            {"SourceField": f["TargetField"], "TargetField": f["SourceField"]}
            for f in compare_fields
        ]
        # logger.info("Vice-versa comparison enabled. Reversing field maps for TGT->SRC check.")
        # reversed_joins = [
        #     {k.replace("SRC", "TGT") if k.startswith("SRC") else k.replace("TGT", "SRC"): v for k, v in join.items()}
        #     for join in join_fields
        # ]
        # reversed_fields = [
        #     {k.replace("SRC", "TGT") if k.startswith("SRC") else k.replace("TGT", "SRC"): v for k, v in field.items()}
        #     for field in compare_fields
        # ]
        # logger.info("=========================================================================")
        logger.info("Vice-versa comparison TGT->SRC Started")
        # logger.info("=========================================================================")
        result_reverse =run_comparison(tgt_df, src_df, targetType,sourceType,reversed_joins, reversed_fields, direction="TGT->SRC")
    logger.info("Data comparison process completed.")
    final_result = result_forward or result_reverse
    if not final_result:
        logger.info(f"[TestCase {testCaseNumber}] Test Result: PASS")
    else:
        logger.error(f"[TestCase {testCaseNumber}] Test Result: FAIL")


    return final_result


def run_row_count_validation(source_data, target_data,approvedValues, testCaseNumber, testDescription,log_doc=None):
    try:
        # Convert to list if cursor
        if isinstance(source_data, Cursor):
            source_data = list(source_data)
        if isinstance(target_data, Cursor):
            target_data = list(target_data)

        # source_count = len(source_data) if source_data else 0
        # target_count = len(target_data) if target_data else 0
        source_count = source_data
        target_count = target_data

        logger.info(f"[TC {testCaseNumber}] {testDescription}")

        if not target_data and source_count == approvedValues :
            logger.info(f"Source Count: {source_count}| Approved Count: {approvedValues}")
            logger.info("Records Count Validation is completed.")
            logger.info(f"[TC {testCaseNumber}] PASS: Row counts match.")
            if log_doc:
                append_log_entry(log_doc, "Info", "RowCount",
                                 f"Source Count: {source_count}, Approved Count: {approvedValues}")
            return False
        elif not target_data and source_count != approvedValues :
            logger.info(f"Source Count: {source_count}| Approved Count: {approvedValues}")
            logger.info("Records Count Validation is completed.")
            logger.error(f"[TC {testCaseNumber}] Fail: Row count Not matched.")
            if log_doc:
                append_log_entry(log_doc, "Error", "RowCount",
                                 f"Source Count: {source_count},  Approved Count: {approvedValues}")
            return True
        elif source_count == target_count:
            logger.info(f"Source Count: {source_count}| Approved Count: {target_count}")
            logger.info("Records Count Validation is completed.")
            logger.info(f"[TC {testCaseNumber}] PASS: Row counts match.")
            if log_doc:
                append_log_entry(log_doc, "Info", "RowCount",
                                 f"Source Count: {source_count}, Target Count: {target_count}")
            return False
        else:
            logger.warning(f"[TC {testCaseNumber}] Not Able to perform any action")
            return True
    except Exception as e:
        logger.exception(f"Error in row count validation for TC {testCaseNumber}: {e}")
        return False

