import pdb

from Libraries.MongoDB import Collection
from Libraries.OracleDB import OracleConnection
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()

def get_data(sourceType,sourceDB,sourceQuery,sourceEnvKey,targetType,targetDB,targetQuery, targetEnvKey,sourceQueryType,targetQueryType):
    """
    Fetches data from source and target based on their type and connection info.

    :param sourceType: Type of source (e.g., "MONGODB", "ORACLE")
    :param sourceDB: Source database or collection
    :param sourceQuery: Query for source
    :param sourceEnvKey: Environment key for source
    :param targetType: Type of target (e.g., "MONGODB", "ORACLE")
    :param targetDB: Target database or collection
    :param targetQuery: Query for target
    :param targetEnvKey: Environment key for target
    :return: Tuple of (source_data, target_data)
    """
    source_data, target_data = None, None
    sourceEnvKey = sourceEnvKey.upper()
    targetEnvKey = targetEnvKey.upper()
    try:
        if sourceType and sourceType.upper() == "MONGODB":
            # logger.info("Fetching source data from MongoDB...")
            if sourceQueryType.upper() == "VIEW" or sourceQueryType.upper() == "Collection":
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = source_view.getData({})
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                execute_js_from_query(sourceQuery,125)
            # logger.info(f"Source data records fetched: {len(source_data)}")

        elif sourceType and sourceType.upper() == "ORACLE":
            # logger.info("Fetching source data from Oracle...")
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery).lower():
                if "." in sourceQuery:
                    sourceQuery = "Select * from " + sourceQuery
                else:
                    sourceQuery = "Select * from " + sourceDB + "." + sourceQuery

            source_data = source_connection.fetchAllDict(sourceQuery)
            # logger.info(f"Source data records fetched: {len(source_data)}")
    except Exception as e:
        logger.error(f"Error fetching source data ({sourceType}): {e}")
    try:
        if targetType and targetType.upper() == "MONGODB":
            # logger.info("Fetching target data from MongoDB...")
            target_view = Collection(targetDB, targetQuery, targetEnvKey)
            target_data = target_view.getData({})
            # logger.info(f"Target data records fetched: {len(target_data)}")

        elif targetType and targetType.upper() == "ORACLE":
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery).lower():
                if "." in targetQuery:
                    targetQuery = "Select count(*) from " + targetQuery
                else:
                    targetQuery = "Select count(*) from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchAllDict(targetQuery)
    except Exception as e:
        logger.error(f"Error fetching target data ({targetType}): {e}")

    return source_data,target_data


def get_count(sourceType,sourceDB,sourceQuery,sourceEnvKey,targetType,targetDB,targetQuery, targetEnvKey,sourceQueryType,targetQueryType):
    """
    Fetches data from source and target based on their type and connection info.

    :param sourceType: Type of source (e.g., "MONGODB", "ORACLE")
    :param sourceDB: Source database or collection
    :param sourceQuery: Query for source
    :param sourceEnvKey: Environment key for source
    :param targetType: Type of target (e.g., "MONGODB", "ORACLE")
    :param targetDB: Target database or collection
    :param targetQuery: Query for target
    :param targetEnvKey: Environment key for target
    :return: Tuple of (source_data, target_data)
    """
    source_data, target_data = None, None
    sourceEnvKey = sourceEnvKey.upper()
    targetEnvKey = targetEnvKey.upper()
    try:

        if sourceType and sourceType.upper() == "MONGODB":
            if sourceQueryType.upper() == "VIEW" or sourceQueryType.upper() == "Collection":
                source_view = Collection(sourceDB, sourceQuery, sourceEnvKey)
                source_data = source_view.getDataCount({})
            elif sourceQueryType.upper() == "JAVA_SCRIPT":
                # sourceQuery='print("Hello from Mongosh");'
                execute_js_from_query(sourceQuery, 101)
            # logger.info("Fetching source data from MongoDB...")

            # logger.info(f"Source data records fetched: {len(source_data)}")

        elif sourceType and sourceType.upper() == "ORACLE":
            # logger.info("Fetching source data from Oracle...")
            source_connection = OracleConnection(sourceEnvKey)
            if "select " not in (sourceQuery).lower():
                if "." in sourceQuery:
                    sourceQuery = "Select count(*) from " + sourceQuery
                else:
                    sourceQuery = "Select count(*) from " + sourceDB + "." + sourceQuery

            source_data = source_connection.fetchOne(sourceQuery)
            # logger.info(f"Source data records fetched: {len(source_data)}")
    except Exception as e:
        logger.error(f"Error fetching source data ({sourceType}): {e}")
    try:
        if targetType and targetType.upper() == "MONGODB":
            # logger.info("Fetching target data from MongoDB...")
            target_view = Collection(targetDB, targetQuery, targetEnvKey)
            target_data = target_view.getDataCount({})
            # logger.info(f"Target data records fetched: {len(target_data)}")

        elif targetType and targetType.upper() == "ORACLE":
            # logger.info("Fetching target data from Oracle...")
            target_connection = OracleConnection(targetEnvKey)
            if "select " not in (targetQuery).lower():
                if "." in targetQuery:
                    targetQuery = "Select count(*) from " + targetQuery
                else:
                    targetQuery = "Select count(*) from " + targetDB + "." + targetQuery
            target_data = target_connection.fetchOne(targetQuery)
    except Exception as e:
        logger.error(f"Error fetching target data ({targetType}): {e}")

    return source_data,target_data

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
        logger.error(f"Error fetching target data : {e}")


def execute_js_from_query(js_code_string, payment_event_id):
    js_path = None  # Define early to avoid reference error

    try:

        # Step 1: Write JS code to a temporary file
        # pdb.set_trace()
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