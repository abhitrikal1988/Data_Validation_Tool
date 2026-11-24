import os
import pdb
from datetime import datetime
import json
import logging
import getpass
from .send_email_report import make_dir, log_dirc
from collections import defaultdict

logger = logging.getLogger()

def create_log_doc(test_case_id, test_case_name):
    now = datetime.utcnow()
    return {
        "TestCaseID": int(test_case_id),
        "TestCaseName": test_case_name,
        "Result": "Pass",
        "StartTime": now,
        "EndTime": None,
        "Log": []
    }

def append_log_entry(log_doc, log_type, run_type, detail):
    log_doc["Log"].append({
        "Log_ID": len(log_doc["Log"]) + 1,
        "LogTime": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "Type": log_type.upper(),
        "RunType": run_type,
        "Detail": detail
    })
    if log_type.upper() == "ERROR":
        log_doc["Result"] = "Fail"
    return log_doc

def finalize_and_store_log(log_doc, test_logs):
    log_doc["EndTime"] = datetime.utcnow()
    test_logs.append(log_doc)


def write_master_log_to_file(log_collection, environment, module, stage, cycle, test_logs,
                             file_dir=make_dir(log_dirc), filename=None):
    try:
        os.makedirs(file_dir, exist_ok=True)
        now = datetime.utcnow()
        user_name = getpass.getuser()
        module_log_ids={}
        # Group test cases by module
        module_testcases_map = defaultdict(list)
        for test_case in test_logs:
            try:
                module_name = test_case.get("Module", module)
                data_validation_id=test_case.get("DataValidationLog_ID")
                cleaned_case=dict(test_case)
                cleaned_case.pop("Module", None)
                cleaned_case.pop("DataValidationLog_ID",None)
                module_testcases_map[module_name].append(cleaned_case
                    # **test_case,
                    # "DataValidationLog_ID":data_validation_id,
                    # "Module":module_name
                )
                module_log_ids[module_name]=data_validation_id
                # module_testcases_map[module_name].append(test_case)
                # module_log_id[module_name]=data_validation_id
            except Exception as e:
                logger.error(f"Error processing test case {test_case}: {e}")

        # Construct final master log with separate sections per module
        master_log = []
        for mod, testcases in module_testcases_map.items():
            # log_id=testcases[0].get("DataValidationLog_ID") if testcases else None
            log_id=module_log_ids.get(mod)
            # master_log[mod] = {
            master_log.append({
                "DataValidationLog_ID": log_id,
                "Module": mod,
                "Stage": stage,
                "Cycle": cycle,
                "TestCases": testcases,
                "Audit": {
                    "InsertedName": user_name,
                    "InsertedDate": now,
                }
            })

        # Create filename if not passed
        if filename is None:
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            filename = f"{environment}_{module}_Log_{timestamp}.json"
        dir_path = make_dir(os.path.join(file_dir, "DB_Log"))
        filepath = os.path.join(dir_path, filename)

        # Write to JSON file
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(master_log, f, indent=4, default=str)
        logger.info(f"Master combined log written to: {filename}")

        # Insert each module’s log into the database
        for log_data in master_log:
            try:
                log_collection.InsertData(log_data)
            except Exception as db_err:
                logger.error(f"Failed to insert log for module {log_data.get('Mdoule')}: {db_err}")

        return filepath

    except Exception as e:
        logger.error(f"Unexpected error in write_master_log_to_file: {e}")
        return None



def write_master_log_to_file2(log_collection,environment,module, stage, cycle, test_logs, log_id=None, file_dir=make_dir(log_dirc),filename=None):
    os.makedirs(file_dir, exist_ok=True)
    now = datetime.utcnow()
    user_name = getpass.getuser()
    if filename is None:
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"{environment}_{module}_Log_{timestamp}.json"
    dir_path = make_dir(os.path.join(file_dir, "DB_Log"))
    filepath = os.path.join(dir_path, filename)
    master_log = {
        "DataValidationLog_ID": log_id,
        "Module": module,
        "Stage": stage,
        "Cycle": cycle,
        "TestCases": test_logs,
        "Audit": {

            "InsertedName": user_name,
            "InsertedDate": now,

        }
    }
    # timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    # filename = f"{module}_consolidated_log_{timestamp}.json"
    # filepath = os.path.join(file_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(master_log, f, indent=4, default=str)
    log_collection.InsertData(master_log)
    logger.info(f"Master log written to: {filename}")
    return filepath
