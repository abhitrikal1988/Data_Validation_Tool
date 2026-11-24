import os
import pdb
import time
import sys

import pandas as pd
from pymongo.cursor import Cursor
scripts_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# combine the project dir with the 'config' dir
project_path = os.path.abspath(os.path.join(scripts_path, '..'))

sys.path.append(scripts_path)
sys.path.append(project_path)

from Libraries.MongoDB import Collection
from Libraries.Comparison_vice_versa_with_log_info import compare_data,is_cursor_empty,run_row_count_validation
from Libraries.Regression_Log_Helper import *
from Libraries.data_fetcher import get_next_sequence_id
from Libraries.parameter_reading import *
from Libraries.send_email_report import *
from Libraries.email_config_parser import *
import logging
from Libraries.Log_Config import Logger_
from Libraries.Read_config import config_read
cr=config_read()
test_data_environment= cr.test_data_environment("REGRESSION_DATA")

logger = logging.getLogger(__name__)


class Regression_inputs:

    def __init__(self,module_name,stage,cycle,order,environment):
        self._module_name=module_name
        self._stage = stage
        self._cycle = cycle
        self._order = order
        self.env_label=environment
        # logger.info("Initializing Regression_inputs")
        environment="MONGODB_"+environment
        db_name = cr.db_name("REGRESSION_DATA")
        rg_collection_data = cr.regression_collection_name("REGRESSION_DATA")
        log_collection = cr.log_collection_name("REGRESSION_DATA")
        self.ValidationList = Collection(db_name, rg_collection_data, environment)
        self.log_collection = Collection(db_name, log_collection, environment)
        # self.ValidationList = Collection("Quality", "DataRegressionSet",environment)
        # self.log_collection = Collection("Quality", "DataValidationLog", environment)
        self.ErrorCount = 0
        self.rules = []
        self.test_logs = []
        # self.log_id = get_next_sequence_id(environment)


    def testcase_details(self):
        try:
            raw_query={}
            if self._module_name:
                raw_query['Module'] = parse_multi_value(self._module_name)
            if self._stage :
                raw_query['Stage'] = parse_multi_value(self._stage)
            if self._cycle:
                raw_query['Cycle'] = parse_multi_value(self._cycle)
            if self._order!="0":
                if isinstance(self._order, str) and "," in self._order:
                    raw_query['TestCaseNumber'] = {"$in": [int(v.strip()) for v in self._order.split(",")]}
                else:
                    raw_query['TestCaseNumber'] = int(self._order)

            sort = [("TestCaseNumber", 1)]
            # logger.info(f"Fetching regression test cases with filter: {raw_query} and sort: {sort}")
            data = self.ValidationList.getDataSort(raw_query, sort)
            if isinstance(data, Cursor):
                first_item = next(data.clone(), None)
                if first_item is None:
                    logger.warning("No validation rules returned from MongoDB with the given filter. Skipping execution.")
                else:
                    for e in data:
                        self.rules.append(e)
        except Exception as e:
            logger.exception("Exception in getting validation rules")
            raise

    def run(self):
        """

        :return:
        """
        from Libraries.data_fetcher import get_data,get_count

        try:
            pass_count=0
            fail_count=0
            test_logs = []
            self.testcase_details()
            # pdb.set_trace()
            environment = "MONGODB_" + self.env_label
            log_id_by_module={}
            for rule in self.rules:
                module_name=rule.get("Module")
                testCaseNumber = rule.get("TestCaseNumber")
                testDescription = rule.get("TestDescription")
                validationType = rule.get("ValidationType")
                # logger.info("=" * 50)
                logger.info(f"========== [ Running TestCase {testCaseNumber}: {testDescription} ] ==========")
                # logger.info("=" * 50)

                start_time = time.time()
                # prepare test infop log
                log_doc = create_log_doc(testCaseNumber, testDescription)
                if module_name not in log_id_by_module:
                    log_id_by_module[module_name] = get_next_sequence_id(environment)
                log_doc["DataValidationLog_ID"]=log_id_by_module[module_name]
                log_doc["Module"]=module_name
                source = rule.get("Source",{})
                sourceType = source.get("Type") if source else None
                sourceEnvironment = source.get("Environment") if source else None
                sourceDB = source.get("DB") if source else None
                sourceQuery = source.get("Query") if source else None
                sourceQueryType = source.get("QueryType") if source else None
                target=rule.get("Target") if source else None
                targetEnvironment = target.get("Environment") if target else None
                targetDB = target.get("DB") if target else None
                targetType = target.get("Type") if target else None
                targetQuery = target.get("Query") if target else None
                targetQueryType = target.get("QueryType") if target else None
                joinFields = rule.get("JoinFields")
                compareFields = rule.get("CompareFields")
                expectedResult = rule.get("ExpectedResult")
                viceversa = rule.get("Viceversa")


                test_case_status=False
                sourceEnvKey = f"{sourceType}_{sourceEnvironment}" if sourceType and sourceEnvironment else ""
                targetEnvKey = f"{targetType}_{targetEnvironment}" if targetType and targetEnvironment else ""

                if validationType == "SRC_TGT":
                    source_data, target_data = get_data(sourceType, sourceDB, sourceQuery, sourceEnvKey, targetType,
                                                        targetDB, targetQuery, targetEnvKey,sourceQueryType,targetQueryType)

                    if not is_cursor_empty(source_data, "source_data") and not is_cursor_empty(target_data, "target_data"):
                        if isinstance(source_data, Cursor):
                            source_data = list(source_data)
                        if isinstance(target_data, Cursor):
                            target_data = list(target_data)
                        if source_data and target_data:
                            append_log_entry(log_doc,"Info","SRC->TGT",
                                f"Total source {sourceType} records: {len(source_data)}, Total target {targetType} records: {len(target_data)}"
                            )
                            test_case_status=compare_data(source_data, target_data,sourceType, targetType,joinFields, compareFields,testCaseNumber,testDescription,viceversa=viceversa,log_doc=log_doc)
                    else:
                        logger.warning("Source or Target data is empty. Skipping comparison.")
                        append_log_entry(log_doc, "Warning", "SRC->TGT", "Source or Target data is empty. Skipping.")
                        log_doc["Result"] = "Fail"

                elif validationType =="Record_Count":
                    approvedValues = int(rule.get("ApprovedValues")[0])
                    source_data, target_data = get_count(sourceType, sourceDB, sourceQuery, sourceEnvKey, targetType,
                                                        targetDB, targetQuery, targetEnvKey,sourceQueryType,targetQueryType)
                    test_case_status=run_row_count_validation(source_data, target_data, approvedValues,testCaseNumber, testDescription,log_doc)

                elif validationType =="InApprovedValue":
                    source_data, target_data = get_data(sourceType, sourceDB, sourceQuery, sourceEnvKey, targetType,
                                                        targetDB, targetQuery, targetEnvKey,sourceQueryType,targetQueryType)
                    approved_values = rule.get("ApprovedValues",[])
                    if isinstance(source_data, Cursor):
                        src_data=pd.DataFrame(source_data)
                        if src_data.shape[1] != 1:
                            raise ValueError("DataFrame must have exactly one column")

                            # Extract values from the only column
                        col_name = src_data.columns[0]
                        values = src_data[col_name].dropna()
                        converted_approved = pd.Series(approved_values).astype(values.dtype)

                        # Find values not in approved list
                        invalid_values = converted_approved[~converted_approved.isin(values)]

                        if invalid_values.empty:
                            logger.info("All values are approved.")
                            append_log_entry(log_doc, "Info", "InApprovedValue",
                                             f"All values of '{approved_values}' are within Source Data.")
                            test_case_status= False
                        else:
                            logger.error(f"Values not in Source Data '{invalid_values.tolist()}'")
                            append_log_entry(log_doc, "Error", "InApprovedValue",
                                             f"Values not in Source Data '{invalid_values.tolist()}'")
                            test_case_status=True
                    else:
                        logger.warning(f"Unknown data type for Assuming not empty.")
                        return True
                else:
                    msg = f"Validation Logic is not Implemented for {validationType}"
                    logger.warning(msg)
                    append_log_entry(log_doc, "Warning", "Validation", msg)
                    log_doc["Result"] = "Fail"
                    test_case_status = True

                if not test_case_status:
                    pass_count+=1
                else:
                    fail_count+=1
                end_time = time.time()
                execution_time = end_time - start_time
                logger.info(f" Execution time for TestCase {testCaseNumber}: {execution_time:.2f} seconds")

                log_doc["MismatchFields"] = log_doc.get("MismatchFields", [])
                log_doc["MissingFields"] = log_doc.get("MissingFields", [])
                # log_doc["DataValidationLog_ID"] = log_id
                finalize_and_store_log(log_doc, self.test_logs)

            logger.info(f" Total Number of Testcases Executed:{len(self.rules)},Pass :{pass_count}, Fail : {fail_count}")
            consolidated_log_path=write_master_log_to_file(self.log_collection,self.env_label,self._module_name, self._stage, self._cycle, self.test_logs)
            # Prepare rows for email report
            path= os.path.join(scripts_path,"Config","email_config.xml")
            # config = read_email_config(path)
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
                report_type="html"
            )
        except Exception as e:
            logger.exception(f"Exception occurred during regression validation run: {e}")
            raise

def parse_multi_value(value):
    if "," in value:
        return {"$in": [v.strip() for v in value.split(",")]}
    return value.strip()

def run_validation(module, stage, cycle, testcase_num, environment=test_data_environment):
    Logger_("")
    logger.info("========== Starting Validation (web/cli) ==========")
    logger.info(f"Module :  {module} ")
    logger.info(f"Stage :  {stage} ")
    logger.info(f"Cycle :  {cycle}")
    logger.info(f"TestCaseNumber : {testcase_num}")
    logger.info(f"Environment  : {environment}")
    Regression_inputs(module, stage, cycle, testcase_num, environment).run()
    logger.info("========== Completed Validation ==========")

if __name__ == '__main__':
    try:
        Logger_("")
        # logger.info("========== Starting Validation ==========")
        args = argparse_()
        run_validation(args.module,args.stage,args.cycle,args.testcasenumber,test_data_environment)
        # logger.info("========== Completed Validation ==========")
    except Exception as esp:
        logger.exception(f"Unexpected exception in __main__: {esp}")
