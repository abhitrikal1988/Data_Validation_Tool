import json
import logging
import requests
 
from .MongoDB  import Collection
from .OracleDB import OracleConnection
from .Read_config       import config_read
 
cr     = config_read()
logger = logging.getLogger()
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Config setup required:
#
# 1. Add this method to Libraries/Read_config.py inside class config_read:
#
#       def edi835_api_url(self, environment: str) -> str:
#           return self.get_conf_data("EDI835_API", environment)
#
# 2. Add this section to Config/environment.ini:
#
#       [EDI835_API]
#       MONGODB_HTA_PROD = https://test1.com/ArtifactsApi/DocumentApi/downloadDocument
#       MONGODB_HTA_QA   = https://qa.test1.com/ArtifactsApi/DocumentApi/downloadDocument
#
# ─────────────────────────────────────────────────────────────────────────────
# Supported Source step Types:
#
#   MONGODB       — query collection with DataFilters, extract OutputKey value
#   ORACLE        — query table/view with DataFilters, extract OutputKey value
#   API_CALL      — call API using InputKey from context, get binary 835 file
#                   (application/octet-stream) decoded as UTF-8 text.
#                   Always the terminal step — no OutputKey needed.
#   SFTP_DOWNLOAD — (future) download file from SFTP path in context
#
# Example Source array in test case document:
#
#   "Source": [
#       {
#           "Step"       : 1,
#           "Type"       : "MONGODB",
#           "DB"         : "Quality",
#           "Query"      : "Attachment",
#           "QueryType"  : "Collection",
#           "Environment": "MONGODB_HTA_PROD",
#           "OutputKey"  : "Attachment_ID"
#       },
#       {
#           "Step"     : 2,
#           "Type"     : "API_CALL",
#           "APIKey"   : "EDI835_API",
#           "ParamKey" : "attachmentID",
#           "InputKey" : "Attachment_ID"
#       }
#   ]
# ─────────────────────────────────────────────────────────────────────────────
 
 
def _execute_step_mongodb(step: dict, context: dict,
                           data_filters: dict) -> dict:
    """
    Executes a MONGODB retrieval step.
    Queries collection with DataFilters, extracts OutputKey from first doc.
 
    Args:
        step         : Step definition from Source array
        context      : Shared pipeline context dict
        data_filters : Resolved MongoDB filter dict from DataFilters
 
    Returns:
        Updated context dict
    """
    db          = step.get("DB", "")
    query       = step.get("Query", "")
    environment = step.get("Environment", "").upper()
    output_key  = step.get("OutputKey", "")
 
    if not query or not output_key:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [MONGODB]: "
            f"Requires 'Query' and 'OutputKey'."
        )
 
    mongo_filter = data_filters if isinstance(data_filters, dict) else {}
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [MONGODB]: "
        f"db.{query}.find("
        f"{json.dumps(mongo_filter, default=str)}).limit(1)"
    )
 
    collection = Collection(db, query, environment)
    document   = collection.find_one(mongo_filter)
 
    if not document:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [MONGODB]: "
            f"No document found in '{query}' with filter {mongo_filter}."
        )
 
    value = document.get(output_key)
    if value is None:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [MONGODB]: "
            f"Field '{output_key}' not found in document."
        )
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [MONGODB]: "
        f"{output_key} = {value}"
    )
 
    context[output_key] = value
    return context
 
 
def _execute_step_oracle(step: dict, context: dict,
                          data_filters: str) -> dict:
    """
    Executes an ORACLE retrieval step.
    Substitutes context values into :placeholders in query,
    applies DataFilters, extracts OutputKey from first result row.
 
    Args:
        step         : Step definition from Source array
        context      : Shared pipeline context dict
        data_filters : Resolved Oracle WHERE string from DataFilters
 
    Returns:
        Updated context dict
    """
    db          = step.get("DB", "")
    query       = step.get("Query", "")
    environment = step.get("Environment", "").upper()
    output_key  = step.get("OutputKey", "")
 
    if not query or not output_key:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [ORACLE]: "
            f"Requires 'Query' and 'OutputKey'."
        )
 
    # Substitute context values into :placeholder in query
    resolved_query = query
    for key, val in context.items():
        resolved_query = resolved_query.replace(f":{key}", str(val))
 
    if "select " not in resolved_query.lower():
        if "." in resolved_query:
            resolved_query = f"SELECT * FROM {resolved_query}"
        else:
            resolved_query = f"SELECT * FROM {db}.{resolved_query}"
 
    if data_filters:
        resolved_query = (
            f"SELECT * FROM ({resolved_query}) WHERE {data_filters}"
        )
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [ORACLE]: {resolved_query}"
    )
 
    connection = OracleConnection(environment)
    result     = connection.fetchAllDict(resolved_query)
 
    if not result:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [ORACLE]: "
            f"No records returned."
        )
 
    value = result[0].get(output_key)
    if value is None:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [ORACLE]: "
            f"Field '{output_key}' not found in result row."
        )
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [ORACLE]: "
        f"{output_key} = {value}"
    )
 
    context[output_key] = value
    return context
 
 
def _execute_step_api_call(step: dict, context: dict,
                            environment: str) -> str:
    """
    Executes an API_CALL retrieval step — always the terminal step.
    Looks up API base URL from config using environment key,
    appends InputKey value from context as query parameter,
    calls the API and decodes binary response as UTF-8 text.
 
    Response expected:
        Content-Type        : application/octet-stream
        Content-Disposition : attachment; filename=...
 
    Args:
        step        : Step definition from Source array
        context     : Shared pipeline context dict
        environment : Environment key for config API URL lookup
 
    Returns:
        Raw 835 file content as string
    """
    api_key   = step.get("APIKey", "")
    param_key = step.get("ParamKey", "")
    input_key = step.get("InputKey", "")
 
    if not api_key or not param_key or not input_key:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [API_CALL]: "
            f"Requires 'APIKey', 'ParamKey', 'InputKey'."
        )
 
    input_value = context.get(input_key)
    if input_value is None:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [API_CALL]: "
            f"InputKey '{input_key}' not found in context. "
            f"Check previous step OutputKey."
        )
 
    # APIKey in step = the key name within [EDI835_API] config section
    # Fall back to environment if APIKey not specified
    lookup_key = api_key if api_key else environment
    try:
        base_url = cr.get_conf_data("EDI835_API", lookup_key)
    except KeyError:
        try:
            base_url = cr.get_conf_data("EDI835_API", environment)
        except KeyError:
            base_url = ""
            
    if not base_url:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [API_CALL]: "
            f"No API URL configured for environment '{environment}'. "
            f"Add [EDI835_API] section to Config/environment.ini."
        )
 
    url = f"{base_url}?{param_key}={input_value}"
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [API_CALL]: GET {url}"
    )
 
    response = requests.get(url, timeout=120)
 
    if response.status_code != 200:
        raise ValueError(
            f"EDI835 Step {step.get('Step')} [API_CALL]: "
            f"HTTP {response.status_code} — {response.text[:200]}"
        )
 
    # Extract filename from content-disposition for logging
    disposition = response.headers.get("content-disposition", "")
    filename    = ""
    if "filename=" in disposition:
        filename = (
            disposition.split("filename=")[1]
            .split(";")[0]
            .strip()
            .strip('"')
        )
 
    content = response.content.decode("utf-8", errors="replace")
 
    logger.info(
        f"EDI835 Step {step.get('Step')} [API_CALL]: "
        f"Downloaded '{filename}' — "
        f"{len(response.content):,} bytes."
    )
 
    return content
 
 
def _execute_step_sftp_download(step: dict, context: dict,
                                 environment: str) -> str:
    """
    SFTP_DOWNLOAD step — stub for future implementation.
 
    To implement:
        1. Add [SFTP_HTA_PROD] section to Config/environment.ini
           with host, port, username, password fields
        2. Add sftp_config(environment) method to Read_config
        3. pip install paramiko
        4. Implement paramiko SFTP download here
    """
    raise NotImplementedError(
        f"EDI835 Step {step.get('Step')} [SFTP_DOWNLOAD]: "
        f"Not yet implemented. See docstring for implementation guide."
    )
 
 
def fetch_835_file(source_steps: list, data_filters,
                   runtime_filters: dict = None) -> str:
    """
    Executes the Source retrieval pipeline defined in the test case.
    Steps executed in Step number order.
    OutputKey from each step is stored in context and available
    as InputKey to subsequent steps.
 
    Args:
        source_steps    : List of step dicts from test case Source array
        data_filters    : Resolved DataFilters — dict (MongoDB) or str (Oracle)
        runtime_filters : Runtime -f CLI overrides
 
    Returns:
        Raw 835 file content as string
 
    Raises:
        ValueError if any step fails or no file was retrieved
    """

    logger.info(f"EDI835 fetch_835_file received data_filters: {data_filters}")

    if not source_steps:
        raise ValueError(
            "EDI835: Source steps are empty — nothing to execute."
        )
 
    steps   = sorted(source_steps, key=lambda s: s.get("Step", 0))
    context = {}
    raw_835 = None
 
    # Determine environment from first step that declares one
    environment = ""
    for step in steps:
        env = step.get("Environment", "")
        if env:
            environment = env.upper()
            break
 
    for step in steps:
        step_num  = step.get("Step")
        step_type = (step.get("Type") or "").upper()
 
        try:
            if step_type == "MONGODB":
                mongo_filter = (
                    data_filters
                    if isinstance(data_filters, dict)
                    else {}
                )
                context = _execute_step_mongodb(step, context, mongo_filter)
 
            elif step_type == "ORACLE":
                oracle_filter = (
                    data_filters
                    if isinstance(data_filters, str)
                    else ""
                )
                context = _execute_step_oracle(step, context, oracle_filter)
 
            elif step_type == "API_CALL":

                api_env = (step.get("Environment") or environment or "").upper()
                raw_835 = _execute_step_api_call(step, context, api_env)
 
            elif step_type == "SFTP_DOWNLOAD":
                raw_835 = _execute_step_sftp_download(
                    step, context, environment
                )
 
            else:
                raise ValueError(
                    f"EDI835 Step {step_num}: "
                    f"Unknown Type '{step_type}'. "
                    f"Supported: MONGODB, ORACLE, API_CALL, SFTP_DOWNLOAD."
                )
 
        except NotImplementedError:
            raise
        except Exception as e:
            logger.issue(
                f"EDI835 Step {step_num} [{step_type}] failed: {e}"
            )
            raise
 
    if raw_835 is None:
        raise ValueError(
            "EDI835: Pipeline completed but no file was retrieved. "
            "Ensure one step has Type='API_CALL' or 'SFTP_DOWNLOAD'."
        )
 
    return raw_835