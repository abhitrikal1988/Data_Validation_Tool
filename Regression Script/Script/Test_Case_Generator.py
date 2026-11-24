#!/usr/bin/env python3
"""
Test_Case_Generator.py
======================
QA Automation Framework — AI Agent
Location: Libraries/

User-facing entry point for AI test case generation.

    1. Takes a source type and a target type (with file format when either
       side is a file)
    2. Runs Data_Profiler.py against both
    3. Builds the Ollama prompt (BRD Section 4.1 system prompt)
    4. Calls Ollama at the endpoint configured as OLLAMA_API
    5. Extracts the JSON array of test cases from the response
    6. Loads each complete, valid test case into DataRegressionSet (insert
       new / update changed / leave unchanged — Libraries/TestCase_Loader.py);
       incomplete or invalid ones are not loaded
    7. Saves the test case file into the folder configured as TestCase_Dir,
       with the load result of every test case under "_LoadStatus"

Configuration
-------------
Read from an INI file, searched in this order:
    --config <path>
    ./agent_config.ini
    ./config.ini
    ./environment.ini

    [AI_AGENT]
    OLLAMA_API   = http://localhost:11434
    OLLAMA_MODEL = phi3:mini
    TestCase_Dir = ./generated_testcases

Environment variables OLLAMA_API, OLLAMA_MODEL and TESTCASE_DIR override
the file. Command line flags override everything.

Mandatory input
---------------
Only the source definition is mandatory. The target is optional:
  - source + target → SRC_TGT (which also compares the record counts) plus
    source checks; no Record_Count test case
  - source only     → Record_Count, Null_Check, Duplicate_Check,
    Format_Check, InApprovedValue
  - source + several tables (-t repeated, or DW.CONTRACT*) → each table is
    matched to the level of the collection it holds (the document itself or
    one of its arrays, e.g. DefaultPay[]) and gets its own SRC_TGT; array
    tables are compared through a pipeline that $unwinds the array
    (Libraries/Array_Profiler.py)
Record_Count's expected count and InApprovedValue's approved values are asked
for after generation (or given with --expected-count / --approved-values).
--module and --stage are optional labels; when omitted they are simply left
out of the prompt and of the generated file name.

Connection test
---------------
Passing TEST_CONNECTION (or "Test Connection") as the source type checks the
Ollama endpoint and exits without profiling or generating:

    python Test_Case_Generator.py --source-type TEST_CONNECTION

The whole feature lives in one block marked TEST CONNECTION MODE and can be
commented out without touching anything else.

Reference: AI_Agent_Technical_BRD.md
    Section 3   — Data Profiler
    Section 4.1 — System prompt specification
    Section 4.2 — Ollama client, retry and JSON extraction
    Section 9   — Error handling

Examples
--------
Oracle source, fixed-width file target:

    python Test_Case_Generator.py \
        --source-type ORACLE \
        --source-env ORACLE_HTA_PROD --source-table FIN.PAYMENTS \
        --target-type LOCAL_PATH --target-format FIXED_WIDTH \
        --target-path "Z:\\Extracts\\Payments\\May_2026\\" \
        --target-pattern "FIN_PAYMENTS_EXTRACT_*.txt" \
        --target-columns layout.json

Interactive:

    python Test_Case_Generator.py --interactive

Build the prompt without calling Ollama:

    python Test_Case_Generator.py ... --prompt-only
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import copy
import hashlib
import configparser
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Framework logging compatibility
# ---------------------------------------------------------------------------
# The validation framework uses custom log levels (ISSUE, SUMMARY, DETAIL)
# registered by its CLI entry point. Library code such as Oracle_Connection
# calls logger.issue(...) directly. When those libraries are driven from this
# wrapper the entry point never runs, so the methods are missing and the call
# fails with: 'Logger' object has no attribute 'issue'.
#
# Register them here before any framework module is used. Values follow the
# standard scale: DEBUG 10, INFO 20, WARNING 30, ERROR 40.

CUSTOM_LOG_LEVELS = {
    "DETAIL":  15,
    "SUMMARY": 25,
    "ISSUE":   35,
}


def ensure_framework_logging() -> None:
    """Adds the framework's custom levels and logger methods if absent."""
    for name, level in CUSTOM_LOG_LEVELS.items():
        logging.addLevelName(level, name)
        method = name.lower()

        if hasattr(logging.Logger, method):
            continue

        def make_method(lvl):
            def log_method(self, message, *args, **kwargs):
                if self.isEnabledFor(lvl):
                    self._log(lvl, message, args, **kwargs)
            return log_method

        setattr(logging.Logger, method, make_method(level))
        setattr(logging, method, make_method(level).__get__(
            logging.getLogger(), logging.Logger))


ensure_framework_logging()


# Imported after the shim so framework modules load with the levels present
scripts_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
project_path = os.path.abspath(os.path.join(scripts_path, '..'))
sys.path.append(scripts_path)
sys.path.append(project_path)

from Libraries.Data_Profiler import DataProfiler  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Configuration — reads from the same environment.ini as the rest of the
# framework via Read_config.config_read.  Add [AI_AGENT] to that file:
#
#   [AI_AGENT]
#   OLLAMA_API   = http://localhost:11434
#   OLLAMA_MODEL = phi3:mini
#   TestCase_Dir = ./generated_testcases
#
# Environment variables and --config / CLI flags override in that order.
# ---------------------------------------------------------------------------

CONFIG_SECTION = "AI_AGENT"

CONFIG_DEFAULTS = {
    "OLLAMA_API":   "http://localhost:11434",
    "OLLAMA_MODEL": "phi3:mini",
    "TestCase_Dir": "./generated_testcases",
}

ENV_OVERRIDES = {
    "OLLAMA_API":   "OLLAMA_API",
    "OLLAMA_MODEL": "OLLAMA_MODEL",
    "TestCase_Dir": "TESTCASE_DIR",
}

from Libraries.Read_config import config_read as _config_read  # noqa: E402


def load_config(explicit_path: str = None) -> dict:
    """
    Loads AI agent configuration, using the framework's shared environment.ini.

    Priority (lowest → highest):
      1. CONFIG_DEFAULTS (built-in fallbacks)
      2. [AI_AGENT] section in Config/environment.ini  — via config_read()
      3. Environment variables OLLAMA_API, OLLAMA_MODEL, TESTCASE_DIR
      4. --config <path>          explicit INI override (test / CI use)
      5. --ollama-api / --model / --testcase-dir  CLI flags (handled in main)

    Keys returned: OLLAMA_API, OLLAMA_MODEL, TestCase_Dir, _config_file.
    """
    config = dict(CONFIG_DEFAULTS)

    if explicit_path:
        # ── Explicit path (--config flag or CI override) ──────────────────
        if not os.path.exists(explicit_path):
            raise SystemExit(f"ERROR: config file not found: {explicit_path}")
        parser = configparser.ConfigParser()
        parser.optionxform = str          # preserve TestCase_Dir casing
        parser.read(explicit_path, encoding="utf-8")
        section = (parser[CONFIG_SECTION]
                   if parser.has_section(CONFIG_SECTION)
                   else parser["DEFAULT"])
        for key in CONFIG_DEFAULTS:
            for option in section:
                if option.lower() == key.lower() and section[option].strip():
                    config[key] = section[option].strip()
        config["_config_file"] = explicit_path
    else:
        # ── Framework environment.ini via config_read() ───────────────────
        # Reads the same Config/environment.ini as OracleDB, MongoDB, etc.
        # Falls back to CONFIG_DEFAULTS silently if [AI_AGENT] is absent.
        cr = _config_read()
        for method, key in (
            ("ollama_api",   "OLLAMA_API"),
            ("ollama_model", "OLLAMA_MODEL"),
            ("testcase_dir", "TestCase_Dir"),
        ):
            try:
                config[key] = getattr(cr, method)()
            except Exception:
                pass   # section or key absent — keep default

        config["_config_file"] = os.path.normpath(
            os.path.join(cr.path, "Config", "environment.ini")
        )

    # ── Environment variable overrides ────────────────────────────────────
    for key, env_name in ENV_OVERRIDES.items():
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]

    config["OLLAMA_API"] = config["OLLAMA_API"].rstrip("/")
    return config



# ---------------------------------------------------------------------------
# Accepted types — UI BRD Section 2.2, Technical BRD Section 4.1
# ---------------------------------------------------------------------------

DB_TYPES = ["ORACLE", "MONGODB"]

FILE_RETRIEVAL_TYPES = [
    "LOCAL_PATH", "UNC_PATH", "SFTP", "FTP", "S3", "AZURE_BLOB",
    "ORACLE_CLOB", "MONGODB_GRIDFS", "API_CALL", "AUTO",
]

FILE_FORMATS = [
    "CSV", "PIPE", "TSV", "FIXED_WIDTH", "EXCEL", "JSON", "XML",
    "HL7", "NCPDP_D0", "NCPDP_SCRIPT", "EDI835", "EDI271", "EDI834", "EDI837", "PDF",
]

# File formats whose parser returns one of several tables (FileConfig.Table).
MULTI_TABLE_FORMATS = {"EDI271", "EDI834", "EDI837"}
DEFAULT_FILE_TABLE = {"EDI271": "MEMBER", "EDI834": "MEMBER", "EDI837": "CLAIM"}

# ---------------------------------------------------------------------------
# Per-validation-type test case shape (from the field specification table)
# ---------------------------------------------------------------------------
# "required" — must be present, filled in from the profile when the model
#              leaves it out
# "allowed"  — may be present; kept when supplied
# Anything not listed for a type is removed, so a Null_Check never carries
# JoinFields and a SRC_TGT never carries CheckFields.
#
# Source.* and the common fields (Module, Stage, TestCaseNumber,
# TestDescription, ValidationType, DataFilters) are mandatory for every type
# and are handled separately.
# ---------------------------------------------------------------------------

TESTCASE_SHAPE = {
    "SRC_TGT": {
        "required": ["Target", "JoinFields", "CompareFields", "Viceversa"],
        "allowed":  ["ExpectedResult", "ApprovedValues"],
    },
    "RECORD_COUNT": {
        # Source only: ApprovedValues[0] is the expected source record count.
        "required": ["ApprovedValues"],
        "allowed":  ["ExpectedResult"],
    },
    "INAPPROVEDVALUE": {
        "required": ["CompareFields", "ApprovedValues"],
        "allowed":  ["Target", "CheckFields", "ExpectedResult"],
    },
    "NULL_CHECK": {
        "required": ["CheckFields"],
        "allowed":  ["Target", "ExpectedResult"],
    },
    "DUPLICATE_CHECK": {
        "required": ["CheckFields", "ExpectedResult"],
        "allowed":  ["Target"],
    },
    "FORMAT_CHECK": {
        "required": ["CheckFields", "ExpectedResult"],
        "allowed":  ["Target"],
    },
}

# Present on every test case regardless of type.
COMMON_FIELDS = [
    "TestCaseNumber", "Module", "Stage", "TestDescription",
    "ValidationType", "Source", "DataFilters",
    # The table marks Viceversa NA outside SRC_TGT, but it is kept on every
    # test case as an explicit false so the flag is never simply absent.
    "Viceversa",
]

# Validation types that legitimately compare two sides. Everything else is a
# single-sided check on the source, and must not carry a Target.
TWO_SIDED_TYPES = {"SRC_TGT"}

# EDI835 types are only meaningful when one side actually is an 835 file.
EDI835_TYPES = {"EDI835_FORMAT", "EDI835_BALANCE", "EDI835_DATA"}

ALL_TYPES = DB_TYPES + FILE_RETRIEVAL_TYPES

DEFAULT_DELIMITERS = {"CSV": ",", "TSV": "\t", "PIPE": "|"}

# Ollama context window (prompt + response combined) and response cap.
# The default num_ctx in Ollama is small; a wide profile leaves too little
# room for the answer and the JSON arrives truncated. Override with --num-ctx.
# Structured-output schema handed to Ollama. Constrained decoding forces the
# reply to BE an array of test cases, so the model cannot return a JSON Schema
# describing them, wrap them in an object, or drift into prose. Requires
# Ollama 0.5+; older builds ignore it and fall back to plain JSON mode.
# Minimum test cases to demand from the model. A small model will happily
# emit one object and close the array — constrained decoding cannot end the
# array before minItems is satisfied, so this is what actually drives volume.
DEFAULT_MIN_TESTCASES = 8


def build_testcase_schema(min_items: int = DEFAULT_MIN_TESTCASES) -> dict:
    """
    Structured-output schema for the reply. `minItems` is the lever that stops
    the model finishing after a single test case: decoding is not allowed to
    close the array until that many objects exist.
    """
    exact = max(1, int(min_items))
    return {
        "type": "array",
        # Both bounds set to the same value: decoding cannot close the array
        # early AND cannot run past it, so every run returns the same number
        # of test cases even when the model itself is not reproducible.
        "minItems": exact,
        "maxItems": exact,
        "items": {
            "type": "object",
            "properties": {
                "TestCaseNumber":  {"type": ["integer", "null"]},
                "Module":          {"type": "string"},
                "Stage":           {"type": "string"},
                "ValidationType":  {"type": "string"},
                "TestDescription": {"type": "string"},
                "Viceversa":       {"type": "boolean"},
            },
            "required": ["ValidationType", "TestDescription"],
        },
    }

# Sampling is the main reason two runs with identical inputs return a
# different number of test cases. temperature 0 makes decoding greedy and a
# fixed seed pins any remaining tie-breaks, so the same prompt gives the same
# reply. Raise --temperature deliberately if you want varied suggestions.
DEFAULT_SEED        = 42
DEFAULT_TEMPERATURE = 0.0

# Raised so a full set of test cases fits in one reply. num_ctx covers the
# prompt AND the response together, so the profile prompt and the generated
# array both have to fit inside it — hence the headroom over num_predict.
# If the model or the host cannot hold this much, lower it with --num-ctx.
# 32768 was more than the prompt needs (a typical profile prompt is under
# 4k tokens) and an oversized KV cache makes llama.cpp batch differently
# between runs, which costs reproducibility for no benefit. Raise it only if
# a prompt actually approaches the limit.
DEFAULT_NUM_CTX     = 8192
DEFAULT_NUM_PREDICT = 16384


# ===========================================================================
# TEST CONNECTION MODE — START
# ---------------------------------------------------------------------------
# Diagnostic short circuit. When the source type is TEST_CONNECTION the script
# pings the configured Ollama endpoint, reports what it found and exits. No
# profiling, no generation, no file written.
#
# To remove the feature: comment out this block and the two call sites marked
# "TEST CONNECTION MODE" in interactive_side() and main(). Nothing else refers
# to it.
# ===========================================================================

TEST_CONNECTION_TYPE = "TEST_CONNECTION"


def is_test_connection(value: str) -> bool:
    """True for TEST_CONNECTION written with spaces, dashes or any casing."""
    if not value:
        return False
    return value.strip().replace(" ", "_").replace("-", "_").upper() \
        == TEST_CONNECTION_TYPE


def run_test_connection(ollama_api: str, ollama_model: str,
                        timeout: int = 10) -> int:
    """
    Checks the Ollama endpoint and prints the result.

    Returns 0 when reachable, 5 (AI_UNAVAILABLE) when not — the same code the
    normal run uses for an unreachable endpoint.
    """
    print("Connection test — Ollama")
    print(f"  Endpoint: {ollama_api}")
    print(f"  Model:    {ollama_model}")

    client = OllamaClient(ollama_api, ollama_model, timeout=timeout)
    print(f"  Transport: {'ollama package' if client._client else 'REST'}")

    started = time.time()
    if not client.health_check():
        print(f"\nFAILED: AI_UNAVAILABLE — no Ollama at {ollama_api}.",
              file=sys.stderr)
        print("Check the service is running and OLLAMA_API is correct.",
              file=sys.stderr)
        return 5

    elapsed = time.time() - started
    print(f"\nReachable — responded in {elapsed:.2f}s")

    # -- Model inventory, best effort: a reachable endpoint is still a pass --
    try:
        if client._client:
            payload = client._client.list()
        else:
            payload = client._get("/api/tags")

        models = []
        for entry in (payload or {}).get("models", []) or []:
            name = entry.get("model") or entry.get("name") if isinstance(
                entry, dict) else getattr(entry, "model", None)
            if name:
                models.append(str(name))

        if models:
            print(f"Models available ({len(models)}): " + ", ".join(sorted(models)))
            base = ollama_model.split(":")[0]
            if any(m == ollama_model or m.split(":")[0] == base for m in models):
                print(f"Configured model '{ollama_model}' is present.")
            else:
                print(f"WARNING: configured model '{ollama_model}' was not "
                      f"found — pull it with: ollama pull {ollama_model}")
        else:
            print("No models are installed on this endpoint.")
    except Exception as exc:
        print(f"WARNING: could not list models: {exc}")

    return 0

# ===========================================================================
# TEST CONNECTION MODE — END
# ===========================================================================


# ---------------------------------------------------------------------------
# Section 4.1 — System prompt, full specification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are a QA test case generator for a healthcare data
validation framework. You analyze data profiles (schema
and statistics only — no patient data) and generate
complete test case JSON documents.

VALIDATION TYPES:
- SRC_TGT: Compare source data against target data field by field
- Null_Check: Check for null values in specified fields
- Duplicate_Check: Check for duplicate records on key fields
- Format_Check: Validate field values against regex patterns
- Record_Count: Check the source row count against an expected count
  (source only — SRC_TGT already compares source and target counts)
- InApprovedValue: Validate field values against approved list
- EDI835_Format: Validate X12 835 file structure
- EDI835_Balance: Validate 835 internal amount balancing
- EDI835_Data: Cross-validate 835 amounts against DB

SOURCE/TARGET TYPES: ORACLE, MONGODB, SFTP, LOCAL_PATH,
UNC_PATH, S3, AZURE_BLOB, CSV, PIPE, TSV, FIXED_WIDTH,
EXCEL, JSON, XML, HL7, NCPDP_D0, NCPDP_SCRIPT, AUTO

COLUMN DEFINITION:
{ Name, SourceName (or position "0","1","2" for no-header),
  Type (STRING/DECIMAL/INTEGER/DATE/DATETIME/BOOLEAN),
  Position (fixed-width only), Width (fixed-width only),
  Required (true/false), Transform }

TRANSFORMS:
String:  TRIM, UPPERCASE, LOWERCASE, TRIM_LEADING_ZEROS,
         PAD_LEADING_ZEROS:N
Decimal: STRIP_CURRENCY, STRIP_COMMAS, PARENTHESES_NEGATIVE,
         EUROPEAN_DECIMAL, IMPLIED_DECIMAL:N, SCIENTIFIC_NOTATION
Date:    YYYYMMDD, MM/DD/YYYY, DD-MON-YYYY, AUTO_DATE
Boolean: BOOLEAN_YN, BOOLEAN_10, BOOLEAN_TF, BOOLEAN_AUTO

MAPPING RULES:
- Exact name match → confidence 1.0
- Same root word (PAID_AMT ~ PaidAmt) → confidence 0.9
- Same pattern + type → confidence 0.8
- Same value range → confidence 0.7
- No match → do not include in JoinFields

GENERATION RULES:
- one Null_Check on the key fields (IDs, codes) that have no nulls
- distinct_count == row_count → candidate for Duplicate_Check key
- pattern [0-9]{10} → generate Format_Check regex ^\\d{10}$
- no Target given → generate one Record_Count on the source
- distinct_count <= 5 → consider InApprovedValue
- column constant (min==max) → use as DataFilter
- has_currency_symbol → add STRIP_CURRENCY transform
- date pattern detected → add appropriate date transform

OUTPUT RULES:
- Return ONLY a valid JSON array
- No prose, no markdown, no explanation
- No trailing commas
- All strings properly quoted
- NEVER write comments. JSON has no comments: no // and no /* */
- Do not annotate or explain any value — emit only the value
- Keep TestDescription under 12 words
- Each test case must have:
  TestCaseNumber (always null),
  Module, Stage, ValidationType, TestDescription,
  Viceversa (exact spelling, true for SRC_TGT),
  Source, Target, DataFilters
- The key is spelled exactly "Viceversa" — not ViceVersa,
  not Vice_Versa, and never a variant spelling
- SRC_TGT test cases must also have:
  JoinFields    — the field(s) that identify a row. A join often
                  needs SEVERAL fields; list every one:
                  [{"SourceField": "ElectionStartDate",
                    "TargetField": "ElectionStartDate"},
                   {"SourceField": "Cardholder_ID",
                    "TargetField": "Cardholder_ID"}]
  CompareFields — same shape, the fields whose VALUES are checked.
                  Never repeat a JoinFields entry here: join keys
                  match the rows, compare fields verify them. For a
                  period, the START date is a join key and the END
                  date is a compare field
- CheckFields entries are {"SourceField": "X",
  "ResultFields": ["X"]} — ResultFields holds exactly its own
  SourceField, one to one, never a list of other columns
- DataFilters is an ARRAY. Each entry names ONE side only:
  [{"SourceField": "PaymentEvent_ID",
    "DefaultValue": "", "OperatorType": "="},
   {"TargetField": "payment_event_id",
    "DefaultValue": "", "OperatorType": "="}]
  Use an empty array [] when no filter applies — never {}
- Target rules — a test case carries "Target" ONLY when its
  ValidationType is SRC_TGT or an EDI835_* type. Record_Count,
  Null_Check, Duplicate_Check, Format_Check and InApprovedValue
  examine the source alone: omit "Target" entirely for them,
  do not emit it as null or {}
- When a Target is given, never generate Record_Count: SRC_TGT
  already compares the source and target record counts
- When NO Target is given, never generate SRC_TGT
- When "Target" is required, embed the Target definition given
  below exactly as the Source definition is embedded
- When a Target is given, the FIRST test case must be SRC_TGT
  with JoinFields and CompareFields filled in. Emit it before
  any single-sided check so it is never the one left out
- Only generate EDI835_Format, EDI835_Balance or EDI835_Data
  when the EDI835 line in the constraints below says they are
  allowed. Otherwise never use those types.
- Module and Stage are optional labels. Use the values given
  above when present; when they are not given, set both to ""
  — never invent them
"""

USER_PROMPT_TEMPLATE = """Generate test cases for the following data profile.
{context}
Source type: {source_type}{source_format}
Target type: {target_type}{target_format}

The source definition to embed in each generated test case:
{source_def}

The target definition to embed in each generated test case:
{target_def}

DATA PROFILE:
{profile}

Apply the GENERATION RULES to the profile above and return the
JSON array of test cases."""

RETRY_SUFFIX = "\nIMPORTANT: Return ONLY the JSON array. Nothing else."


# ---------------------------------------------------------------------------
# Section 4.2 — Ollama client
# ---------------------------------------------------------------------------

class OllamaClient:
    """
    Talks to the Ollama endpoint configured as OLLAMA_API.

    Uses the `ollama` Python package when available, otherwise falls back
    to the REST API over urllib so the script runs with no extra install.
    """

    def __init__(self, host: str, model: str, timeout: int = 120,
                 num_ctx: int = DEFAULT_NUM_CTX,
                 num_predict: int = DEFAULT_NUM_PREDICT,
                 json_format: bool = True,
                 seed: int = DEFAULT_SEED,
                 min_testcases: int = DEFAULT_MIN_TESTCASES):
        self.host    = host.rstrip("/")
        self.model   = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.json_format = json_format
        self._schema_unsupported = False
        self.seed = seed
        self.min_testcases = min_testcases
        self.last_done_reason = None
        self.last_stats = {}
        self._client = None

        try:
            from ollama import Client
            self._client = Client(host=self.host)
        except ImportError:
            logger.debug("ollama package not installed — using REST fallback")

    # -- transport ----------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> dict:
        import urllib.request
        req = urllib.request.Request(f"{self.host}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- api ----------------------------------------------------------------

    def health_check(self) -> bool:
        """Check if Ollama is running and reachable at OLLAMA_API."""
        try:
            if self._client:
                self._client.list()
            else:
                self._get("/api/tags")
            return True
        except Exception as exc:
            logger.debug(f"Ollama health check failed: {exc}")
            return False

    def generate(self, system: str, user: str,
                 temperature: float = DEFAULT_TEMPERATURE) -> str:
        """
        Calls Ollama with retry on timeout.
        Low temperature for consistent JSON output.
        """
        # num_ctx is the total window: prompt + response share it. Ollama's
        # default is small, so a wide data profile crowds out the answer and
        # the JSON comes back cut off mid-token. Set it explicitly.
        options = {
            "temperature": temperature,
            "seed":        self.seed,
            "top_k":       1,
            "top_p":       1.0,
            "num_ctx":     self.num_ctx,
            "num_predict": self.num_predict,
            "stop": ["```"],
        }
        # Ollama's JSON mode constrains decoding to valid JSON, which stops
        # prose, markdown fences and comments at the source. Disable with
        # --no-json-format if a model behaves badly under it.
        # Prefer the schema (forces the array shape); fall back to plain
        # "json" if the server rejects it, and to nothing if disabled.
        if not self.json_format:
            fmt = None
        elif self._schema_unsupported:
            fmt = "json"
        else:
            fmt = build_testcase_schema(self.min_testcases)
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]

        last_error = None
        for attempt in range(3):
            try:
                if self._client:
                    kwargs = {"model": self.model, "messages": messages,
                              "options": options}
                    if fmt:
                        kwargs["format"] = fmt
                    response = self._client.chat(**kwargs)
                else:
                    payload = {
                        "model":    self.model,
                        "messages": messages,
                        "options":  options,
                        "stream":   False,
                    }
                    if fmt:
                        payload["format"] = fmt
                    response = self._post("/api/chat", payload)
                def _field(name):
                    if isinstance(response, dict):
                        return response.get(name)
                    return getattr(response, name, None)

                self.last_done_reason = _field("done_reason")
                self.last_stats = {
                    "done_reason":  self.last_done_reason,
                    "done":         _field("done"),
                    "eval_count":   _field("eval_count"),
                    "prompt_eval_count": _field("prompt_eval_count"),
                    "num_ctx":      self.num_ctx,
                    "num_predict":  self.num_predict,
                }
                logger.debug(f"Ollama stats: {self.last_stats}")
                if self.last_done_reason == "length":
                    logger.warning(
                        f"Ollama stopped at the token limit "
                        f"(num_ctx={self.num_ctx}, num_predict={self.num_predict}). "
                        f"The reply is cut off — raise --num-ctx, or narrow the "
                        f"source/target so the profile prompt is smaller."
                    )
                return response["message"]["content"]
            except Exception as exc:
                last_error = exc
                # Older Ollama builds reject a schema object in "format".
                # Drop to plain JSON mode and try again rather than failing.
                if fmt and not isinstance(fmt, str) and not self._schema_unsupported:
                    self._schema_unsupported = True
                    fmt = "json"
                    logger.warning(
                        "This Ollama build rejected the response schema — "
                        "falling back to plain JSON mode (needs Ollama 0.5+)."
                    )
                    continue
                if attempt == 2:
                    raise
                logger.warning(f"Ollama call failed (attempt {attempt + 1}) — "
                               f"retrying: {exc}")
                time.sleep(2)

        raise RuntimeError(f"Ollama call failed: {last_error}")

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def _looks_like_testcase(obj) -> bool:
        """
        A real test case has a ValidationType. Schema fragments, property
        definitions and the model's own metadata do not — without this check
        those get 'salvaged' and written out as malformed test cases.
        """
        if not isinstance(obj, dict):
            return False
        if not obj.get("ValidationType"):
            return False
        # A JSON Schema property block can carry ValidationType as a key name;
        # reject anything carrying schema vocabulary.
        schema_markers = {"$schema", "properties", "items", "required",
                          "definitions", "additionalProperties"}
        if schema_markers & set(obj.keys()):
            return False
        return True

    @staticmethod
    def _is_schema_document(obj) -> bool:
        """True when the model described the format instead of filling it."""
        if not isinstance(obj, dict):
            return False
        return bool({"$schema", "properties", "definitions"} & set(obj.keys()))

    @staticmethod
    def _strip_json_noise(text: str) -> str:
        """
        Removes things models emit that JSON forbids: // and /* */ comments,
        markdown fences, and trailing commas. String-aware, so a URL such as
        "https://x" and a comma inside a value are left alone.
        """
        out, i, length = [], 0, len(text)
        in_string = escaped = False

        while i < length:
            ch = text[i]

            if in_string:
                out.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue

            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue

            # line comment — skip to end of line
            if ch == "/" and i + 1 < length and text[i + 1] == "/":
                while i < length and text[i] != "\n":
                    i += 1
                continue

            # block comment — skip to terminator
            if ch == "/" and i + 1 < length and text[i + 1] == "*":
                end = text.find("*/", i + 2)
                i = length if end == -1 else end + 2
                continue

            out.append(ch)
            i += 1

        cleaned = "".join(out)
        cleaned = cleaned.replace("```json", " ").replace("```", " ")
        # trailing commas before a closing bracket/brace
        cleaned = re.sub(r",(\s*[\]}])", r"\1", cleaned)
        return cleaned

    @staticmethod
    def extract_json_array(text: str) -> list:
        """
        Safely extracts a JSON array from model output.
        Handles prose before/after the JSON, and salvages whole objects from a
        reply that was cut off by the token limit (the array never closes, so a
        plain json.loads of the whole thing fails and everything is lost).
        """
        if not text:
            return []

        text = OllamaClient._strip_json_noise(text)

        # Some models wrap the array in an object: {"testcases": [ ... ]}.
        # Take the first list value rather than failing.
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    if OllamaClient._is_schema_document(obj):
                        logger.error(
                            "The model returned a JSON Schema describing test "
                            "cases instead of the test cases themselves. "
                            "Nothing usable in this reply."
                        )
                        return []
                    for value in obj.values():
                        if isinstance(value, list) and any(
                                OllamaClient._looks_like_testcase(v)
                                for v in value):
                            return [v for v in value
                                    if OllamaClient._looks_like_testcase(v)]
            except json.JSONDecodeError:
                pass

        try:
            start = text.index("[")
        except ValueError:
            logger.error(f"No JSON array found in: {text[:200]}")
            return []

        # Fast path — well-formed array.
        try:
            end = text.rindex("]") + 1
            if end > start:
                parsed = json.loads(text[start:end])
                if isinstance(parsed, list):
                    real = [c for c in parsed
                            if OllamaClient._looks_like_testcase(c)]
                    if real or not parsed:
                        return real
                    logger.error(
                        f"Parsed {len(parsed)} object(s) but none look like "
                        f"test cases (no ValidationType) — the model returned "
                        f"the wrong kind of document."
                    )
                    return []
        except (ValueError, json.JSONDecodeError):
            pass

        # Salvage path — walk the array and keep every complete object.
        # Brace counting is string-aware so braces inside values don't confuse it.
        salvaged, depth, obj_start = [], 0, None
        in_string, escaped = False, False

        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start is not None:
                    chunk = text[obj_start:i + 1]  # already noise-stripped
                    try:
                        candidate = json.loads(chunk)
                        if OllamaClient._looks_like_testcase(candidate):
                            salvaged.append(candidate)
                    except json.JSONDecodeError:
                        pass
                    obj_start = None

        if salvaged:
            logger.warning(
                f"Reply was not valid JSON (likely cut off at the token limit) "
                f"— salvaged {len(salvaged)} complete test case(s); any "
                f"partially written one was dropped."
            )
            return salvaged

        logger.error(f"Failed to parse JSON from: {text[:200]}")
        return []


# ---------------------------------------------------------------------------
# Definition builders
# ---------------------------------------------------------------------------

def load_columns(path: str) -> list:
    """Loads a column layout JSON file (required for FIXED_WIDTH)."""
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: column layout file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        columns = json.load(fh)
    if not isinstance(columns, list):
        raise SystemExit("ERROR: column layout file must contain a JSON array")
    return columns


def load_file_config(value: str) -> dict:
    """--source-file-config: JSON text or a JSON file, merged into FileConfig
    (RecordTag for XML, RecordPath for JSON, RecordSets for HL7 / NCPDP,
    Sheet for Excel, CodeLists for EDI271 ...)."""
    text = value
    if os.path.exists(value):
        with open(value, "r", encoding="utf-8") as fh:
            text = fh.read()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"ERROR: --file-config is not valid JSON ({exc}): {value[:80]}")
    if not isinstance(data, dict):
        raise SystemExit("ERROR: --file-config must be a JSON object, e.g. '{\"RecordTag\": \"Claim\"}'")
    return data


def build_definition(side: str, args) -> dict:
    """Builds a Source or Target definition dict. `side` is 'source' or 'target'."""
    def arg(name, default=None):
        return getattr(args, f"{side}_{name}", default)

    type_name = (arg("type") or "").upper()

    if type_name == "ORACLE":
        return {
            "Type":        "ORACLE",
            "Environment": arg("env"),
            "DB":          (arg("table") or "").split(".")[0] or None,
            "Query":       arg("table"),
            "QueryType":   "QUERY",
        }

    if type_name == "MONGODB":
        return {
            "Type":        "MONGODB",
            "Environment": arg("env"),
            "DB":          arg("db"),
            "Query":       arg("collection"),
            "QueryType":   "COLLECTION",
        }

    fmt = (arg("format") or "").upper()
    delimiter = arg("delimiter") or DEFAULT_DELIMITERS.get(fmt)

    file_config = {
        "Format":      fmt,
        "Delimiter":   delimiter,
        "Header":      not arg("no_header"),
        "SkipRows":    arg("skip_rows") or 0,
        "FooterRows":  arg("footer_rows") or 0,
        "Encoding":    arg("encoding"),
        "NullValues":  ["NULL", "null", "N/A", "na", "", "-", "none"],
        "Columns":     load_columns(arg("columns")) if arg("columns") else [],
    }
    if fmt == "FIXED_WIDTH":
        # Fixed-width fields are padded to their width; the padding is not
        # data. A column with no Transform of its own is trimmed.
        for col in file_config["Columns"]:
            if isinstance(col, dict) and not col.get("Transform"):
                col["Transform"] = "TRIM"
    if fmt in MULTI_TABLE_FORMATS:
        # One file, several tables: which one this side compares.
        table = getattr(args, "file_table", None)
        if table:
            file_config["Table"] = table.strip().upper()
        for key in ("Delimiter", "Header", "SkipRows", "FooterRows", "Columns"):
            file_config.pop(key, None)              # not used by these parsers
        file_config = {k: v for k, v in file_config.items() if v is not None}
    extra = arg("file_config")
    if extra:
        file_config.update(load_file_config(extra))

    definition = {
        "Type":         type_name,
        "Path":         arg("path"),
        "FileName":     arg("file"),
        "FilePattern":  arg("pattern"),
        "PickStrategy": (arg("pick") or "LATEST").upper(),
        "FileConfig":   file_config,
    }

    if arg("credential"):
        definition["CredentialKey"] = arg("credential")

    return definition


def validate_side(side: str, args) -> None:
    """Enforces the argument rules for one side."""
    type_name = (getattr(args, f"{side}_type") or "").upper()
    prefix = f"--{side}"

    if type_name not in ALL_TYPES:
        raise SystemExit(
            f"ERROR: {prefix}-type must be one of: {', '.join(ALL_TYPES)}"
        )

    if type_name == "ORACLE":
        if not getattr(args, f"{side}_env") or not getattr(args, f"{side}_table"):
            raise SystemExit(
                f"ERROR: ORACLE {side} requires {prefix}-env and {prefix}-table"
            )
        return

    if type_name == "MONGODB":
        if not getattr(args, f"{side}_env") or \
                not getattr(args, f"{side}_collection"):
            raise SystemExit(
                f"ERROR: MONGODB {side} requires {prefix}-env and "
                f"{prefix}-collection"
            )
        return

    fmt = (getattr(args, f"{side}_format") or "").upper()
    if not fmt:
        raise SystemExit(
            f"ERROR: {side} is a file type ({type_name}) — "
            f"{prefix}-format is required. One of: {', '.join(FILE_FORMATS)}"
        )
    if fmt not in FILE_FORMATS:
        raise SystemExit(
            f"ERROR: {prefix}-format must be one of: {', '.join(FILE_FORMATS)}"
        )
    if not getattr(args, f"{side}_pattern") and not getattr(args, f"{side}_file"):
        raise SystemExit(
            f"ERROR: file {side} requires {prefix}-pattern or {prefix}-file"
        )
    if fmt in ("FIXED_WIDTH", "PDF") and not getattr(args, f"{side}_columns"):
        raise SystemExit(
            f"ERROR: {fmt} {side} requires {prefix}-columns "
            f"(JSON file with Name/Position/Width/Type per column)"
        )
    table = getattr(args, "file_table", None)
    if fmt in MULTI_TABLE_FORMATS and table:
        from Libraries.File_Table_Mapper import parser_module
        TABLE_NAMES = parser_module(fmt).TABLE_NAMES
        if table.strip().upper() not in TABLE_NAMES:
            raise SystemExit(f"ERROR: --file-table {table!r} is not a {fmt} table. "
                             f"One of: {', '.join(TABLE_NAMES)}")


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def ask(label: str, default: str = "", choices: list = None,
        required: bool = False) -> str:
    if choices:
        label = f"{label} [{'/'.join(choices)}]"
    if default:
        label = f"{label} (default: {default})"
    while True:
        value = input(f"{label}: ").strip() or default
        if not value and required:
            print("  This value is required.")
            continue
        if choices:
            # Match case-insensitively and treat spaces/dashes as underscores,
            # so "Test Connection" resolves to TEST_CONNECTION. The canonical
            # spelling from `choices` is what gets returned.
            canonical = {c.replace(" ", "_").replace("-", "_").upper(): c
                         for c in choices}
            key = value.replace(" ", "_").replace("-", "_").upper()
            if key not in canonical:
                print(f"  Please choose one of: {', '.join(choices)}")
                continue
            return canonical[key]
        return value


def interactive_side(side: str, args) -> None:
    print(f"\n--- {side.upper()} ---")

    # TEST CONNECTION MODE — offered on the source prompt only. Remove
    # `choices` back to plain ALL_TYPES to drop it.
    choices = ALL_TYPES + ([TEST_CONNECTION_TYPE] if side == "source" else ["NONE"])

    # Target is optional: NONE generates source-only test cases.
    type_name = ask(f"{side} type", default="NONE" if side == "target" else "",
                    choices=choices).upper()
    if side == "target" and type_name == "NONE":
        setattr(args, "target_type", None)
        return
    setattr(args, f"{side}_type", type_name)

    # TEST CONNECTION MODE — main() sees the type and short circuits.
    if is_test_connection(type_name):
        return

    if type_name == "ORACLE":
        setattr(args, f"{side}_env", ask("  environment", "ORACLE_HTA_PROD",
                                         required=True))
        setattr(args, f"{side}_table", ask("  table (OWNER.TABLE)",
                                           required=True))
        return

    if type_name == "MONGODB":
        setattr(args, f"{side}_env", ask("  environment", "MONGODB_HTA_PROD",
                                         required=True))
        setattr(args, f"{side}_db", ask("  database", required=True))
        setattr(args, f"{side}_collection", ask("  collection", required=True))
        return

    fmt = ask("  file format", choices=FILE_FORMATS).upper()
    setattr(args, f"{side}_format", fmt)
    setattr(args, f"{side}_path", ask("  path", required=True))
    setattr(args, f"{side}_pattern", ask("  file pattern", "*.txt",
                                         required=True))

    if fmt == "FIXED_WIDTH":
        setattr(args, f"{side}_columns", ask("  column layout JSON file",
                                             required=True))
    else:
        setattr(args, f"{side}_delimiter",
                ask("  delimiter", DEFAULT_DELIMITERS.get(fmt, "")))
        setattr(args, f"{side}_no_header",
                ask("  has header row", "Y", ["Y", "N"]).upper() == "N")


# ---------------------------------------------------------------------------
# Prompt assembly and output
# ---------------------------------------------------------------------------

def build_user_prompt(profile: dict, source_def: dict, target_def: dict,
                      args) -> str:
    source_fmt = (args.source_format or "").upper()
    target_fmt = (args.target_format or "").upper()

    # Module and Stage are optional — omit the line entirely when not given,
    # so the model is not shown an empty label to guess at.
    context_lines = []
    if (args.module or "").strip():
        context_lines.append(f"Module: {args.module.strip()}")
    if (args.stage or "").strip():
        context_lines.append(f"Stage:  {args.stage.strip()}")
    context = ("\n" + "\n".join(context_lines) + "\n") if context_lines else ""

    wanted = getattr(args, "count", DEFAULT_MIN_TESTCASES)
    if target_def:
        first = (f"  - one SRC_TGT comparing the two sides (first in the array)\n"
                 f"  - NO Record_Count — SRC_TGT already compares the counts\n")
    else:
        first = (f"  - there is NO target: no SRC_TGT, no Target on any test case\n"
                 f"  - one Record_Count on the source\n")
    context += (
        f"\nGENERATE AT LEAST {wanted} TEST CASES. Cover the validation "
        f"types that fit the profile rather than repeating one type:\n"
        + first +
        f"  - one Null_Check on the key fields (IDs, codes)\n"
        f"  - Duplicate_Check on the key columns\n"
        f"  - Format_Check per column with a detected pattern\n"
        f"  - InApprovedValue for low-cardinality columns\n"
    )

    edi_allowed = side_is_edi835(source_def) or side_is_edi835(target_def)
    context += (
        "\nEDI835: " + (
            "one side is an X12 835 file — EDI835_* validation types are "
            "allowed.\n"
            if edi_allowed else
            "neither side is an X12 835 file — do NOT generate any "
            "EDI835_Format, EDI835_Balance or EDI835_Data test cases.\n"
        )
    )

    return USER_PROMPT_TEMPLATE.format(
        context       = context,
        source_type   = (args.source_type or "").upper(),
        source_format = f" (format: {source_fmt})" if source_fmt else "",
        target_type   = (args.target_type or "NONE (source only)").upper(),
        target_format = f" (format: {target_fmt})" if target_fmt else "",
        source_def    = json.dumps(source_def, indent=2),
        target_def    = (json.dumps(target_def, indent=2) if target_def
                         else "none — source-only test cases"),
        profile       = json.dumps(profile, indent=2, default=str),
    )


def output_filename(args) -> str:
    """
    Builds a safe, timestamped file name for the generated test cases.

    Module and Stage are optional, so the name is assembled from whichever
    labels are present. With neither, it falls back to the source and target
    types, which are always there.
    """
    def slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_")

    parts = [slug(args.module), slug(args.stage)]
    parts = [p for p in parts if p]

    if not parts:
        parts = [p for p in (slug(args.source_type), slug(args.target_type))
                 if p] or ["generated"]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"testcases_{'_'.join(parts)}_{stamp}.json"


# ---------------------------------------------------------------------------
# Post-generation normalisation
# ---------------------------------------------------------------------------
#
# The model is asked for a schema but cannot be relied on to honour it. Every
# structural guarantee the framework depends on is enforced here instead, so a
# sloppy reply still produces a runnable test case file.
#
# Enforced:
#   1. TestCaseNumber sequential from 1
#   2. "Viceversa" spelled correctly (models mangle it: Vicevaeis, ViceVersa...)
#   3. Source and Target both present, taken from the real definitions
#   4. SRC_TGT carries JoinFields and CompareFields
# ---------------------------------------------------------------------------

VICEVERSA_KEY = "Viceversa"

# The framework matches ValidationType with ==, so a typo like "Null_Chek"
# silently matches no branch and the test case does nothing. Model output is
# snapped back to these exact spellings.
CANONICAL_VALIDATION_TYPES = [
    "SRC_TGT", "Record_Count", "Null_Check", "Duplicate_Check",
    "Format_Check", "InApprovedValue",
    "EDI835_Format", "EDI835_Balance", "EDI835_Data",
]


def _canonical_validation_type(value: str, index: int) -> str:
    """
    Snaps a ValidationType to its exact framework spelling, repairing the
    typos small models produce (Null_Chek, Format_Chek, SRC-TGT, ...).
    """
    import difflib

    raw = str(value or "").strip()
    if raw in CANONICAL_VALIDATION_TYPES:
        return raw

    def squash(text):
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    target = squash(raw)
    if not target:
        return raw

    best, best_score = None, 0.0
    for candidate in CANONICAL_VALIDATION_TYPES:
        score = difflib.SequenceMatcher(None, target, squash(candidate)).ratio()
        if score > best_score:
            best, best_score = candidate, score

    if best and best_score >= 0.85:
        if best != raw:
            logger.warning(
                f"TestCase {index}: corrected ValidationType "
                f"'{raw}' to '{best}'."
            )
        return best

    logger.debug(
        f"TestCase {index}: ValidationType '{raw}' does not match any known "
        f"type and was left as-is — it will not run."
    )
    return raw


def _canonical_viceversa(case: dict) -> dict:
    """
    Renames any near-miss spelling of Viceversa to the exact key the framework
    reads. Compares letters-only and lowercased, so ViceVersa, vice_versa and
    typos like Vicevaeis all collapse onto the same target.
    """
    import difflib

    target = "viceversa"
    for key in list(case.keys()):
        if key == VICEVERSA_KEY:
            continue
        squashed = "".join(ch for ch in key.lower() if ch.isalpha())
        if not squashed or abs(len(squashed) - len(target)) > 3:
            continue
        # Threshold measured against the real key set: the worst observed
        # typo ("Vicevaeis") scores 0.667, while the closest legitimate key
        # ("DataFilters") scores only 0.400 — 0.60 sits safely between. The
        # "vice" prefix check catches mangled tails regardless of ratio.
        ratio = difflib.SequenceMatcher(None, squashed, target).ratio()
        if (squashed == target
                or ratio >= 0.60
                or (squashed.startswith("vice") and len(squashed) >= 7)):
            value = case.pop(key)
            case[VICEVERSA_KEY] = value
            logger.warning(f"Renamed misspelled key '{key}' to '{VICEVERSA_KEY}'.")
    return case


def _profile_field_names(side_profile: dict) -> list:
    """
    Column names for one side of the profile. File profiles carry
    'suggested_name' (with 'position' as fallback); Oracle and MongoDB
    carry 'name'.
    """
    names = []
    for col in (side_profile or {}).get("columns", []) or []:
        name = (col.get("name")
                or col.get("suggested_name")
                or col.get("position"))
        if name is not None:
            names.append(str(name))
    return names


# Word-parts that usually mark a column as part of the row identity. Matched
# against tokenised names, so both Cardholder_ID and ElectionStartDate hit.
KEY_TOKENS = {
    "id", "key", "code", "no", "num", "number", "date", "dt",
    "seq", "ref", "identifier",
}

# Word-parts that disqualify a column from being a join key even when the rest
# of the name looks like one. In effective-dated data the START of a period is
# part of the row identity while the END is an attribute of it: a row is keyed
# by (member, start date) and the end date is what you verify. Without this,
# ElectionEndDate joins on itself and a changed end date reads as a missing
# row instead of a mismatch.
NON_KEY_TOKENS = {
    "end", "thru", "through", "expiry", "expiration", "expire", "expires",
    "term", "termination", "terminated", "stop", "close", "closed", "to",
}


def _name_tokens(name: str) -> list:
    """
    Splits a column name into lowercase word tokens, handling snake_case,
    camelCase and PascalCase alike: ElectionStartDate -> election, start, date.
    """
    if not name:
        return []
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _looks_like_key_name(name: str) -> bool:
    """
    True when a column name reads like part of the row identity: it carries
    identifier vocabulary and none of the period-ending words that mark an
    attribute rather than a key.
    """
    tokens = _name_tokens(name)
    if any(token in NON_KEY_TOKENS for token in tokens):
        return False
    return any(token in KEY_TOKENS for token in tokens)

# Ceiling on auto-selected join keys — beyond this a join is almost certainly
# being over-constrained by the heuristic rather than by the data.
MAX_JOIN_FIELDS = 4


def _source_column_stats(profile: dict) -> dict:
    """Maps source column name -> its profile entry, for uniqueness lookups."""
    stats = {}
    for col in ((profile or {}).get("source") or {}).get("columns", []) or []:
        name = col.get("name") or col.get("suggested_name") or col.get("position")
        if name is not None:
            stats[str(name)] = col
    return stats


def _select_join_fields(field_pairs: list, profile: dict) -> list:
    """
    Picks the join key(s) from the mapped pairs. A join may legitimately need
    several fields (e.g. Cardholder_ID + ElectionStartDate), so this returns
    every column that looks like part of the row identity, not just one.

    A column qualifies when it is near-unique in the profile, or when its name
    matches the usual identifier vocabulary (ID, Key, Code, Date, ...).
    """
    # A file side carries the key found in its data (Data_Profiler
    # find_unique_key): the smallest set of paired columns that identifies
    # every row. Use it whenever all its columns are paired.
    for side, field in (("source", "SourceField"), ("target", "TargetField")):
        key = ((profile or {}).get(side) or {}).get("unique_key") or []
        by_name = {str(p.get(field)): p for p in field_pairs}
        if key and all(str(k) in by_name for k in key):
            return copy.deepcopy([by_name[str(k)] for k in key])

    stats = _source_column_stats(profile)
    chosen = []

    for pair in field_pairs:
        name = pair.get("SourceField", "")
        col = stats.get(name, {})

        distinct = col.get("distinct_count")
        rows     = col.get("row_count")
        near_unique = bool(
            distinct and rows and rows > 0 and (distinct / rows) >= 0.9
        )

        excluded = any(t in NON_KEY_TOKENS for t in _name_tokens(name))
        if not excluded and (near_unique or _looks_like_key_name(name)):
            chosen.append(pair)

    if not chosen:
        # Nothing looks like a key — fall back to the first mapped pair so the
        # join is at least populated, and say so.
        if field_pairs:
            logger.warning(
                "No column looked like a join key — defaulting to "
                f"'{field_pairs[0].get('SourceField')}'. Check JoinFields "
                "before running."
            )
        return copy.deepcopy(field_pairs[:1])

    if len(chosen) > MAX_JOIN_FIELDS:
        logger.warning(
            f"{len(chosen)} columns looked like join keys — keeping the first "
            f"{MAX_JOIN_FIELDS}. Review JoinFields before running."
        )
        chosen = chosen[:MAX_JOIN_FIELDS]

    return copy.deepcopy(chosen)


def _select_compare_fields(field_pairs: list, join_pairs: list) -> list:
    """
    Fields whose VALUES are checked — everything except the join keys.

    Join keys are how rows are matched, so they are equal by construction on
    every matched row; repeating them in CompareFields only adds comparisons
    that can never fail.
    """
    join_keys = {
        (jf.get("SourceField"), jf.get("TargetField")) for jf in (join_pairs or [])
    }
    remaining = [
        fp for fp in field_pairs
        if (fp.get("SourceField"), fp.get("TargetField")) not in join_keys
    ]

    if not remaining and field_pairs:
        # Every mapped column is a join key — comparing nothing is useless,
        # so compare them all and say why.
        logger.warning(
            "Every mapped column was selected as a join key, leaving nothing "
            "to compare — using all fields in CompareFields. Narrow "
            "JoinFields by hand."
        )
        return copy.deepcopy(field_pairs)

    return copy.deepcopy(remaining)


MAX_KEY_FIELDS = 5


def profile_key_fields(profile: dict, join_pairs: list = None) -> list:
    """
    Source fields that identify a record: the join keys first, then columns
    whose names read like keys (ID, Code, Key, Number, start Date …), keeping
    only those present in the profile with no nulls in the sample.
    """
    columns = ((profile or {}).get("source") or {}).get("columns", []) or []
    by_name = {}
    for col in columns:
        name = col.get("name") or col.get("suggested_name") or col.get("position")
        if name is not None:
            by_name[str(name)] = col
    candidates = [str(jp.get("SourceField")) for jp in (join_pairs or []) if jp.get("SourceField")]
    candidates += [str(k) for k in ((profile or {}).get("source") or {}).get("unique_key") or []]
    candidates += [n for n in by_name if _looks_like_key_name(n)]
    keys = []
    for name in candidates:
        col = by_name.get(name)
        if col is None or name in keys:
            continue
        if (col.get("null_count") or 0) > 0:
            continue
        keys.append(name)
    return keys[:MAX_KEY_FIELDS]


def _build_check_fields(validation_type: str, profile: dict,
                        join_pairs: list) -> list:
    """
    Builds CheckFields for the single-sided checks, in the shape the
    comparison code reads: {SourceField, ResultFields} plus FormatType,
    Pattern and Value for Format_Check.

    ResultFields maps 1:1 to SourceField — each entry reports on exactly the
    field it checks, so results line up one-for-one with the checked column.
    """
    columns = ((profile or {}).get("source") or {}).get("columns", []) or []
    key_names = [jp["SourceField"] for jp in (join_pairs or [])]

    def name_of(col):
        value = col.get("name") or col.get("suggested_name") or col.get("position")
        return str(value) if value is not None else None

    fields = []

    if validation_type == "NULL_CHECK":
        # Only fields that must always have a value: the keys (join keys and
        # ID / code / start-date style names) that have no nulls in the
        # profiled sample. Optional fields that already contain nulls are not
        # checked — such a test would fail on its first run and say nothing.
        for name in profile_key_fields(profile, join_pairs):
            fields.append({"SourceField": name, "ResultFields": [name]})

    elif validation_type == "DUPLICATE_CHECK":
        # Duplicates are checked on the key columns (for a file without a
        # target: the key found in its data).
        if not key_names:
            key_names = [str(k) for k in ((profile or {}).get("source") or {}).get("unique_key") or []]
        for name in key_names:
            fields.append({"SourceField": name,
                           "ResultFields": [name]})
        if not fields:
            for col in columns:
                name = name_of(col)
                if name and _looks_like_key_name(name):
                    fields.append({"SourceField": name,
                                   "ResultFields": [name]})

    elif validation_type == "FORMAT_CHECK":
        # Only columns where the profiler detected a pattern can be checked.
        for col in columns:
            name = name_of(col)
            pattern = col.get("sample_pattern")
            if name and pattern:
                fields.append({
                    "SourceField":  name,
                    "FormatType":   "REGEX",
                    "Pattern":      str(pattern),
                    "Value":        "",
                    "ResultFields": [name],
                })

    return fields


def _normalize_check_fields(value, validation_type: str) -> list:
    """Coerces model-supplied CheckFields into the expected element shape."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    normalized = []
    for entry in value:
        if isinstance(entry, str):
            entry = {"SourceField": entry}
        if not isinstance(entry, dict):
            continue

        source_field = (entry.get("SourceField")
                        or entry.get("SourceFields")
                        or entry.get("Field") or "")
        if not source_field:
            continue

        item = {"SourceField": str(source_field)}

        # ResultFields is 1:1 with SourceField — whatever the model supplied,
        # each entry reports on exactly the field it checks.
        item["ResultFields"] = [str(source_field)]

        if validation_type == "FORMAT_CHECK":
            item["FormatType"] = str(entry.get("FormatType") or "REGEX")
            item["Pattern"]    = str(entry.get("Pattern") or "")
            item["Value"]      = entry.get("Value", "")

        normalized.append(item)

    return normalized


def _apply_testcase_shape(case: dict, validation_type: str, profile: dict,
                          join_pairs: list, compare_pairs: list,
                          target_def: dict, index: int) -> dict:
    """
    Enforces the per-type field specification: fills what is required, keeps
    what is allowed, and removes everything else so each test case carries
    only the fields its validation type actually uses.
    """
    shape = TESTCASE_SHAPE.get(validation_type)
    if not shape:
        return case               # unknown type (EDI835_*) — leave alone

    required = shape["required"]
    allowed  = set(required) | set(shape["allowed"])

    # -- fill required fields ------------------------------------------
    if "Target" in required and target_def:
        case["Target"] = copy.deepcopy(target_def)

    if "JoinFields" in required and not case.get("JoinFields"):
        case["JoinFields"] = copy.deepcopy(join_pairs)

    if "CompareFields" in required and not case.get("CompareFields"):
        case["CompareFields"] = copy.deepcopy(compare_pairs)

    if "CheckFields" in required:
        existing = _normalize_check_fields(case.get("CheckFields"),
                                           validation_type)
        if validation_type == "NULL_CHECK":
            existing = []           # always the key fields, never the model's pick
        case["CheckFields"] = existing or _build_check_fields(
            validation_type, profile, join_pairs
        )
        if not case["CheckFields"] and validation_type not in ("NULL_CHECK", "FORMAT_CHECK"):
            logger.warning(
                f"TestCase {index} ({case.get('ValidationType')}) needs "
                f"CheckFields but none could be derived from the profile — "
                f"fill them in before running."
            )
    elif "CheckFields" in case:
        case["CheckFields"] = _normalize_check_fields(case.get("CheckFields"),
                                                      validation_type)

    if "ApprovedValues" in required:
        # Record_Count (expected source count) and InApprovedValue (approved
        # list) need values only a person knows. Anything the model put here
        # is a guess, so it is cleared and collect_approved_values() asks.
        case["ApprovedValues"] = []

    if "ExpectedResult" in required and case.get("ExpectedResult") is None:
        # These checks pass when the query returns nothing.
        case["ExpectedResult"] = 0

    # -- drop fields this type does not use ----------------------------
    for key in list(case.keys()):
        if key in COMMON_FIELDS or key in allowed:
            continue
        removed = case.pop(key, None)
        logger.debug(
            f"TestCase {index}: removed '{key}' — not used by "
            f"{case.get('ValidationType')}."
        )

    return case


def _normalize_data_filters(value) -> list:
    """
    Coerces DataFilters into the array form the framework reads:

        [{"SourceField": "...", "DefaultValue": "", "OperatorType": "="},
         {"TargetField": "...", "DefaultValue": "", "OperatorType": "="}]

    Each entry names exactly one side. Entries naming neither are dropped;
    entries naming both are split into a source entry and a target entry.
    """
    if not value:
        return []

    # A dict of {field: value} is a shape models often emit — convert it.
    if isinstance(value, dict):
        value = [{"SourceField": k, "DefaultValue": v} for k, v in value.items()]

    if not isinstance(value, list):
        return []

    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            continue

        source_field = entry.get("SourceField") or ""
        target_field = entry.get("TargetField") or ""

        # Tolerate loose key names from the model.
        if not source_field and not target_field:
            source_field = entry.get("Field") or entry.get("field") or ""
            if not source_field:
                continue

        default  = entry.get("DefaultValue", "")
        operator = entry.get("OperatorType") or entry.get("Operator") or "="

        if source_field:
            normalized.append({
                "SourceField":  str(source_field),
                "DefaultValue": "" if default is None else default,
                "OperatorType": str(operator),
            })
        if target_field:
            normalized.append({
                "TargetField":  str(target_field),
                "DefaultValue": "" if default is None else default,
                "OperatorType": str(operator),
            })

    return normalized


def _build_field_pairs(profile: dict) -> list:
    """
    Builds [{"SourceField": ..., "TargetField": ...}] from the profiler's
    learned_mappings, falling back to same-named columns on both sides.
    """
    pairs, seen = [], set()

    for mapping in (profile or {}).get("learned_mappings", []) or []:
        src = mapping.get("source_col")
        tgt = mapping.get("target_col")
        if src is None or tgt is None:
            continue
        key = (str(src), str(tgt))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"SourceField": str(src), "TargetField": str(tgt)})

    if not pairs:
        # No mappings — pair columns that share a name on both sides.
        src_names = _profile_field_names((profile or {}).get("source", {}))
        tgt_lookup = {t.lower(): t
                      for t in _profile_field_names((profile or {}).get("target", {}))}
        for name in src_names:
            match = tgt_lookup.get(name.lower())
            if match:
                pairs.append({"SourceField": name, "TargetField": match})

    return pairs


def side_is_edi835(definition: dict) -> bool:
    """
    True when a Source/Target definition refers to an X12 835 file.
    Recognised by an EDI835 file format, an API_CALL retrieval type (the
    835 download pipeline), or an .835 file name / pattern.
    """
    if not isinstance(definition, dict):
        return False

    file_config = definition.get("FileConfig") or {}
    if str(file_config.get("Format") or "").upper() == "EDI835":
        return True

    if str(definition.get("Type") or "").upper() == "API_CALL":
        return True

    for key in ("FileName", "FilePattern"):
        value = str(definition.get(key) or "").lower()
        if value.endswith(".835") or "835" in value.split("."):
            return True

    return False


# Short notes on test cases that were not created, printed after the run.
NOT_CREATED = []


def normalize_testcases(testcases: list, profile: dict,
                        source_def: dict, target_def: dict) -> list:
    """
    Applies every structural guarantee to the generated cases. Returns the
    same list, modified in place.
    """
    field_pairs = _build_field_pairs(profile)
    join_pairs    = _select_join_fields(field_pairs, profile)
    compare_pairs = _select_compare_fields(field_pairs, join_pairs)

    # EDI835 checks only make sense when one side really is an 835 file.
    allow_edi835 = side_is_edi835(source_def) or side_is_edi835(target_def)

    # Drop disallowed EDI835 cases BEFORE numbering, so the numbers stay
    # contiguous from 1 with no gaps where a case was removed.
    kept = []
    for case in testcases:
        if not isinstance(case, dict):
            continue
        validation_type = str(case.get("ValidationType") or "").upper()
        if validation_type == "RECORD_COUNT" and target_def:
            logger.info("Dropped a Record_Count test case — SRC_TGT already "
                        "compares the source and target record counts.")
            continue
        if validation_type == "SRC_TGT" and not target_def:
            logger.info("Dropped a SRC_TGT test case — no target was given.")
            continue
        if validation_type in EDI835_TYPES and not allow_edi835:
            logger.warning(
                f"Dropped a {case.get('ValidationType')} test case — neither "
                f"source nor target is an EDI835 file."
            )
            continue
        kept.append(case)

    testcases = kept

    for index, case in enumerate(testcases, start=1):
        if not isinstance(case, dict):
            continue

        # 1 — sequential numbering, always overwritten
        case["TestCaseNumber"] = index

        # 2 — canonical spellings: Viceversa key and ValidationType value
        _canonical_viceversa(case)
        case["ValidationType"] = _canonical_validation_type(
            case.get("ValidationType"), index
        )

        validation_type = str(case.get("ValidationType") or "").upper()

        # 3 — real Source/Target definitions, never the model's paraphrase.
        # Target belongs only on validation types that actually read it:
        # SRC_TGT and Record_Count compare two sides, and the EDI835 types
        # cross-validate against the other side. Everything else is a
        # single-sided check on the source, so any Target is removed.
        if source_def:
            case["Source"] = copy.deepcopy(source_def)

        # Target is applied by the shape pass for the types that need it;
        # EDI835_* is not in the shape table, so handle it here.
        two_sided = validation_type in EDI835_TYPES

        # Viceversa drives the reverse target-to-source pass, which only
        # SRC_TGT performs. It is true there and false everywhere else.
        case[VICEVERSA_KEY] = (validation_type == "SRC_TGT")

        if two_sided:
            if target_def:
                case["Target"] = copy.deepcopy(target_def)
        elif "Target" in case:
            case.pop("Target", None)
            logger.debug(
                f"Removed Target from TestCase {index} "
                f"({case.get('ValidationType')}) — single-sided check."
            )

        # 4 — per-type field specification: fill the mandatory fields, keep
        # the optional ones, drop everything the type does not use.
        case = _apply_testcase_shape(case, validation_type, profile,
                                     join_pairs, compare_pairs,
                                     target_def, index)
        testcases[index - 1] = case

        if validation_type == "SRC_TGT" and not case.get("JoinFields"):
            logger.warning(
                f"TestCase {index} is SRC_TGT but no column mappings were "
                f"found — JoinFields and CompareFields are empty and must "
                f"be filled in by hand before running."
            )

        case["DataFilters"] = _normalize_data_filters(case.get("DataFilters"))

    # Remove ignored fields (Audit.*) the model may still name.
    for c in testcases:
        for key in ("CheckFields", "CompareFields", "JoinFields"):
            if isinstance(c.get(key), list):
                c[key] = [f for f in c[key] if not (isinstance(f, dict) and
                          (is_ignored_field(f.get("SourceField")) or is_ignored_field(f.get("Field"))))]

    # Drop what cannot run: types the model invented (e.g. "JoinFields",
    # "DataFilter"), a Null_Check with no key field, a Format_Check with no
    # field that has a detectable pattern. Each is noted for the run summary.
    kept = []
    for c in testcases:
        vt = str(c.get("ValidationType") or "")
        if vt not in CANONICAL_VALIDATION_TYPES:
            NOT_CREATED.append(f"'{vt}' from the model — not a validation type")
        elif vt.upper() == "NULL_CHECK" and not c.get("CheckFields"):
            NOT_CREATED.append("Null_Check — no key field without nulls in the profile")
        elif vt.upper() == "FORMAT_CHECK" and not c.get("CheckFields"):
            NOT_CREATED.append("Format_Check — no field with a detectable value pattern "
                               "(add one by hand if a field has a fixed format)")
        else:
            kept.append(c)
    testcases = kept

    # ── Remove test cases that do the same work ────────────────────────
    # Run after the shape pass, so derived CheckFields/JoinFields are visible.
    # Two Null_Checks worded differently both end up checking the same
    # columns; the description differs but the executed test does not, and
    # only the executed test matters. TestDescription and TestCaseNumber are
    # deliberately excluded from the signature.
    unique, seen = [], set()
    for case in testcases:
        if not isinstance(case, dict):
            continue
        signature = (
            str(case.get("ValidationType") or ""),
            json.dumps(case.get("CheckFields"),    sort_keys=True, default=str),
            json.dumps(case.get("JoinFields"),     sort_keys=True, default=str),
            json.dumps(case.get("CompareFields"),  sort_keys=True, default=str),
            json.dumps(case.get("ApprovedValues"), sort_keys=True, default=str),
            json.dumps(case.get("DataFilters"),    sort_keys=True, default=str),
        )
        if signature in seen:
            # logger.warning(
            #     f"Removed a duplicate {case.get('ValidationType')} test case "
            #     f"— it checked the same fields as an earlier one."
            # )
            continue
        seen.add(signature)
        unique.append(case)

    testcases = unique
    for index, case in enumerate(testcases, start=1):
        case["TestCaseNumber"] = index

    # ── Guarantee a SRC_TGT case whenever there are two sides ──────────
    # SRC_TGT is the longest case to emit (it carries JoinFields and
    # CompareFields), so it is the first casualty of a truncated reply and
    # the one most often missing. Synthesise it rather than lose the core
    # comparison; every value comes from the profile, not the model.
    if target_def:
        has_src_tgt = any(
            str(c.get("ValidationType") or "").upper() == "SRC_TGT"
            for c in testcases if isinstance(c, dict)
        )
        if not has_src_tgt:
            if field_pairs:
                described = ", ".join(
                    fp["SourceField"] for fp in field_pairs[:3]
                )
                if len(field_pairs) > 3:
                    described += f" and {len(field_pairs) - 3} more"
            else:
                described = "mapped fields"

            testcases.insert(0, {
                "TestCaseNumber":  None,
                "Module":          "",
                "Stage":           "",
                "ValidationType":  "SRC_TGT",
                "TestDescription": f"Compare source and target on {described}",
                VICEVERSA_KEY:     True,
                "Source":          copy.deepcopy(source_def) if source_def else {},
                "Target":          copy.deepcopy(target_def),
                "JoinFields":      copy.deepcopy(join_pairs),
                "CompareFields":   copy.deepcopy(compare_pairs),
                "DataFilters":     [],
            })
            logger.warning(
                "No SRC_TGT test case came back from the model — added one "
                "built from the profiled column mappings. Review its "
                "JoinFields before running."
            )
            # Renumber after the insert so numbering stays contiguous.
            for index, case in enumerate(testcases, start=1):
                if isinstance(case, dict):
                    case["TestCaseNumber"] = index

    # ── Source only: one Record_Count ──────────────────────────────────
    # Without a target there is no SRC_TGT to compare counts, so the source
    # count is checked against an expected count (asked for afterwards).
    if not target_def and source_def and not side_is_edi835(source_def):
        counts = [c for c in testcases
                  if str(c.get("ValidationType") or "").upper() == "RECORD_COUNT"]
        for extra in counts[1:]:
            testcases.remove(extra)
        if not counts:
            testcases.append({
                "TestCaseNumber":  None,
                "Module":          "",
                "Stage":           "",
                "ValidationType":  "Record_Count",
                "TestDescription": "Source record count matches the expected count",
                VICEVERSA_KEY:     False,
                "Source":          copy.deepcopy(source_def),
                "ApprovedValues":  [],
                "DataFilters":     [],
            })
        for index, case in enumerate(testcases, start=1):
            case["TestCaseNumber"] = index

    return testcases


# ---------------------------------------------------------------------------
# Checks on the target
# ---------------------------------------------------------------------------
# SRC_TGT already compares every mapped column, so a target check is added
# only where it finds something SRC_TGT cannot:
#   Duplicate_Check  on the target key — a reloaded or re-run ETL can insert
#                    the same row twice, which a join-based compare can miss
#   Null_Check       on the target key and on target columns SRC_TGT does not
#                    compare (load dates, surrogate keys, derived columns)
#                    that have no nulls today
#   InApprovedValue  on target code columns SRC_TGT does not compare (values
#                    asked for, like the source ones)
# Each runs on the target table alone: the test case's Source is the target.

MAX_TARGET_NULL_FIELDS = 10


def _target_table_of(target: dict) -> str:
    """'SELECT … FROM DW.CONTRACT_DEFAULT_PAY' → 'DW.CONTRACT_DEFAULT_PAY'."""
    query = str((target or {}).get("Query") or "")
    m = re.search(r"\bFROM\s+([A-Za-z0-9_$#.\"]+)", query, re.IGNORECASE)
    return m.group(1).replace('"', "") if m else query.strip()


def _duplicate_check_fields(stype: str, keys: list) -> list:
    if stype == "ORACLE" and len(keys) > 1:
        # One entry, all key columns: GROUP BY the combination.
        return [{"SourceField": list(keys), "ResultFields": list(keys)}]
    # MongoDB pipelines check all CheckFields together as one key.
    return [{"SourceField": k, "ResultFields": [k]} for k in keys]


def _target_case(args, vt, target_def, description, **fields):
    case = {
        "TestCaseNumber":  None,
        "Module":          (getattr(args, "module", None) or "").strip(),
        "Stage":           (getattr(args, "stage", None) or "").strip(),
        "ValidationType":  vt,
        "TestDescription": description,
        VICEVERSA_KEY:     False,
        "Source":          copy.deepcopy(target_def),
        "DataFilters":     [],
    }
    case.update(fields)
    return case


def build_target_checks(testcases: list, profile: dict, target_def: dict, args) -> list:
    """Target-side Duplicate_Check, Null_Check and InApprovedValue test cases."""
    if not isinstance(target_def, dict) or getattr(args, "no_target_checks", False):
        return []
    stype = str(target_def.get("Type") or "").upper()
    if stype not in ("ORACLE", "MONGODB"):
        return []
    tgt = (profile or {}).get("target") or {}
    cols = {}
    for c in tgt.get("columns", []) or []:
        name = c.get("name") or c.get("suggested_name")
        if name is not None and not is_ignored_field(name):
            cols[str(name)] = c
    if not cols:
        return []
    label = target_def.get("Query") or target_def.get("Collection") or "target"
    src_tgt = next((c for c in testcases
                    if str(c.get("ValidationType") or "").upper() == "SRC_TGT"), {})
    joins = [str(f["TargetField"]) for f in (src_tgt.get("JoinFields") or [])
             if isinstance(f, dict) and f.get("TargetField")]
    compared = {str(f["TargetField"]).upper()
                for f in (src_tgt.get("JoinFields") or []) + (src_tgt.get("CompareFields") or [])
                if isinstance(f, dict) and f.get("TargetField")}
    # Target-side DataFilters of the SRC_TGT case apply here too (the target
    # is this test case's Source, so TargetField becomes SourceField).
    filters = [{"SourceField": f["TargetField"], "DefaultValue": f.get("DefaultValue", ""),
                "OperatorType": f.get("OperatorType") or f.get("Operator") or "="}
               for f in (src_tgt.get("DataFilters") or []) if isinstance(f, dict) and f.get("TargetField")]
    row_count = tgt.get("row_count")
    cases = []

    # Duplicate_Check on the target key
    keys = [k for k in joins if k in cols] or \
           [n for n, c in cols.items() if _looks_like_key_name(n)
            and isinstance(c.get("distinct_count"), int) and isinstance(row_count, int)
            and row_count > 0 and c["distinct_count"] >= 0.99 * row_count][:1]
    if keys:
        cases.append(_target_case(args, "Duplicate_Check", target_def,
                                  f"Target {label}: no duplicate {', '.join(keys)}",
                                  CheckFields=_duplicate_check_fields(stype, keys),
                                  ExpectedResult=0, DataFilters=copy.deepcopy(filters)))
    else:
        NOT_CREATED.append(f"Target Duplicate_Check on {label} — no key column found")

    # Null_Check on keys + columns SRC_TGT does not compare, without nulls today
    null_fields = []
    for name, c in cols.items():
        is_key = name in keys or _looks_like_key_name(name)
        not_compared = name.upper() not in compared
        if (is_key or not_compared) and not (c.get("null_count") or 0) > 0:
            null_fields.append(name)
    null_fields = sorted(null_fields, key=lambda n: (n not in keys, not _looks_like_key_name(n)))
    null_fields = null_fields[:MAX_TARGET_NULL_FIELDS]
    if null_fields:
        cases.append(_target_case(args, "Null_Check", target_def,
                                  f"Target {label}: keys and load columns are populated",
                                  CheckFields=[{"SourceField": n, "ResultFields": [n]} for n in null_fields],
                                  ExpectedResult=0, DataFilters=copy.deepcopy(filters)))

    # InApprovedValue on code columns SRC_TGT does not compare (values asked later)
    n_sampled = tgt.get("sample_size") or 1000
    candidates = []
    for name, c in cols.items():
        n = c.get("distinct_count")
        ctype = str(c.get("type") or "").upper()
        if name.upper() in compared or not isinstance(n, int):
            continue
        near_unique = isinstance(row_count, int) and row_count > 0 and n >= 0.9 * min(row_count, n_sampled)
        if 2 <= n <= APPROVED_VALUES_MAX_DISTINCT and not near_unique \
                and "DATE" not in ctype and "TIME" not in ctype:
            candidates.append((n, name))
    for _, name in sorted(candidates)[:MAX_APPROVED_CANDIDATES]:
        case = _target_case(args, "InApprovedValue", target_def,
                            f"Target {label}: {name} has only approved values",
                            CompareFields=[{"SourceField": name}], ApprovedValues=[],
                            DataFilters=copy.deepcopy(filters))
        case["_side"] = "target"
        cases.append(case)
    return cases


def child_target_duplicates(child_cases: list, args) -> list:
    """Duplicate_Check on each array (child) table's key."""
    if getattr(args, "no_target_checks", False):
        return []
    out = []
    for child in child_cases:
        target = child.get("Target") or {}
        keys = [str(f["TargetField"]) for f in (child.get("JoinFields") or [])
                if isinstance(f, dict) and f.get("TargetField")]
        table = _target_table_of(target)
        if not keys or not table:
            continue
        table_def = {"Type": "ORACLE", "Environment": target.get("Environment"),
                     "DB": target.get("DB"), "Query": table, "QueryType": "QUERY"}
        out.append(_target_case(args, "Duplicate_Check", table_def,
                                f"Target {table}: no duplicate {', '.join(keys)}",
                                CheckFields=_duplicate_check_fields("ORACLE", keys),
                                ExpectedResult=0))
    set_expected_results(out)
    return out


# ---------------------------------------------------------------------------
# Fields never tested
# ---------------------------------------------------------------------------
# Bookkeeping fields such as Audit.InsertedByID / Audit.InsertedName are not
# mandatory for any test, so they are removed from the profile right after
# profiling: the model never sees them and no test case is built on them.
IGNORED_FIELD_ROOTS = ("audit",)


def is_ignored_field(name) -> bool:
    root = str(name or "").split(".")[0].strip().lower()
    return root in IGNORED_FIELD_ROOTS


def drop_ignored_fields(profile: dict) -> list:
    """Removes ignored fields from the profile in place; returns their names."""
    dropped = []
    for side in ("source", "target"):
        part = (profile or {}).get(side) or {}
        cols = part.get("columns")
        if not isinstance(cols, list):
            continue
        keep = []
        for col in cols:
            name = col.get("name") or col.get("suggested_name")
            if is_ignored_field(name):
                dropped.append(str(name))
            else:
                keep.append(col)
        part["columns"] = keep
    if isinstance((profile or {}).get("learned_mappings"), list):
        profile["learned_mappings"] = [
            m for m in profile["learned_mappings"]
            if not is_ignored_field(m.get("source_col")) and not is_ignored_field(m.get("target_col"))]
    return dropped


# ---------------------------------------------------------------------------
# Approved values — asked for, never guessed
# ---------------------------------------------------------------------------
# Record_Count (source only) needs the expected source record count, and
# InApprovedValue needs the list of allowed values. Only a person knows them,
# so after generation the run asks for each one. They can also be given on the
# command line (--expected-count, --approved-values) for unattended runs.
# A test case left without values is not created.

def _parse_approved_option(text: str) -> dict:
    """'Status=A,I;Plan.Type=HMO,PPO' → {'Status': ['A','I'], 'Plan.Type': [...]}"""
    out = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        field, values = part.split("=", 1)
        vals = [v.strip() for v in values.split(",") if v.strip()]
        if field.strip() and vals:
            out[field.strip()] = vals
    return out


def _can_prompt(args) -> bool:
    if getattr(args, "no_prompt", False):
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _source_label(source_def: dict) -> str:
    if not isinstance(source_def, dict):
        return "source"
    obj = source_def.get("Query") or source_def.get("FilePattern") or source_def.get("FileName") or ""
    db = source_def.get("DB")
    return f"{db}.{obj}" if db and "." not in str(obj) else str(obj or "source")


APPROVED_VALUES_MAX_DISTINCT = 25
MAX_APPROVED_CANDIDATES = 5     # fields offered from the profile per run


# Parsed rows of file sides profiled in this run, by definition — used for the
# current values of a field (InApprovedValue). Kept in memory for the run only.
FILE_FRAMES = {}


def _definition_key(definition: dict) -> str:
    return json.dumps(definition or {}, sort_keys=True, default=str)


def _current_values(source_def: dict, field: str, limit: int = 50) -> list:
    """
    The distinct values a field has in the source right now, read directly
    from MongoDB / Oracle (or the parsed file) so the person can see what they
    are approving. Shown on screen only — never sent to the model. Empty list
    on any error.
    """
    try:
        stype = str((source_def or {}).get("Type") or "").upper()
        frame = FILE_FRAMES.get(_definition_key(source_def))
        if frame is not None and stype not in ("MONGODB", "ORACLE"):
            raw = frame[field].dropna().unique().tolist() if field in frame.columns else []
        elif stype == "MONGODB":
            from Libraries.MongoDB import Collection
            coll = Collection(source_def.get("DB"), source_def.get("Query"),
                              str(source_def.get("Environment") or "").upper())
            raw = coll.DbCollection.distinct(field)
        elif stype == "ORACLE":
            from Libraries.OracleDB import OracleConnection
            table = _oracle_table(source_def)
            col = _oracle_ident(field)
            rows = OracleConnection(str(source_def.get("Environment") or "").upper()).fetchAllDict(
                f"SELECT * FROM (SELECT DISTINCT {col} AS V FROM {table} ORDER BY 1) "
                f"WHERE ROWNUM <= {limit + 1}")
            raw = [r.get("V") for r in rows]
        else:
            return []
    except Exception as exc:
        logger.debug(f"Could not read current values of {field}: {exc}")
        return []
    values = sorted({str(v) for v in raw if v not in (None, "")}, key=lambda x: (len(x), x))
    return values[: limit + 1]


def _is_file_side(definition) -> bool:
    return isinstance(definition, dict) and \
        str(definition.get("Type") or "").upper() in FILE_RETRIEVAL_TYPES


def full_row_duplicate_cases(testcases: list, source_def, target_def, args) -> list:
    """
    One Duplicate_Check per file (per file table for EDI271/834/837) with
    CheckFields "ALL": no row may repeat in every field, within each file.
    Covers the file on either side (file vs database, database vs file, file
    only). A file already having such a check gets no second one.
    """
    if getattr(args, "no_row_duplicates", False):
        return []
    sides = [source_def, target_def]
    for case in testcases:
        sides += [case.get("Source"), case.get("Target")]
    seen, out = set(), []
    for case in testcases:
        if str(case.get("ValidationType") or "").upper() == "DUPLICATE_CHECK" and \
                any(str(f.get("SourceField") or "").upper() == "ALL" for f in case.get("CheckFields") or []):
            seen.add(_file_identity(case.get("Source")))
    for side in sides:
        if not _is_file_side(side):
            continue
        fc = side.get("FileConfig") or {}
        if str(fc.get("Format") or "").upper() in MULTI_TABLE_FORMATS and not fc.get("Table"):
            continue                    # the per-table test cases carry the table
        ident = _file_identity(side)
        if ident in seen:
            continue
        seen.add(ident)
        fc = side.get("FileConfig") or {}
        what = side.get("FilePattern") or side.get("FileName") or side.get("Path") or "file"
        table = str(fc.get("Table") or "").upper()
        fmt = str(fc.get("Format") or "").upper()
        if table:
            what += f" ({fmt.replace('EDI', '')} {table})"
        out.append({
            "TestCaseNumber":  None,
            "Module":          (args.module or "").strip(),
            "Stage":           (args.stage or "").strip(),
            "ValidationType":  "Duplicate_Check",
            "TestDescription": f"File {what}: no row repeated in every field",
            "Viceversa":       False,
            "Source":          copy.deepcopy(side),
            "CheckFields":     [{"SourceField": "ALL", "ResultFields": ["ALL"]}],
            "DataFilters":     [],
        })
    return out


def _file_identity(definition) -> str:
    """Which file (and file table) a definition reads — for de-duplication."""
    d = definition if isinstance(definition, dict) else {}
    fc = d.get("FileConfig") or {}
    return json.dumps([str(d.get(k) or "").upper() for k in ("Type", "Path", "FileName", "FilePattern")]
                      + [str(fc.get("Format") or "").upper(), str(fc.get("Table") or "").upper()])


def set_expected_results(testcases: list) -> None:
    """
    ExpectedResult is a description for the reader (Data_Validation does not
    evaluate it). Written in the same style as the existing test cases and
    the same on every run, so re-running the generator does not show it as
    a change.
    """
    for case in testcases:
        vt = str(case.get("ValidationType") or "").upper()
        approved = case.get("ApprovedValues") or []
        fields = []
        for f in case.get("CheckFields") or []:
            if isinstance(f, dict) and f.get("SourceField"):
                value = f["SourceField"]
                fields += [str(v) for v in value] if isinstance(value, list) else [str(value)]
        names = ", ".join(fields)
        if vt == "SRC_TGT":
            text = "Source and target should match: no missing records and no mismatched values"
        elif vt == "RECORD_COUNT":
            n = str(approved[0]) if approved else ""
            text = "Should not return data" if n == "0" else f"Should return {n} records"
        elif vt == "INAPPROVEDVALUE":
            cf = case.get("CompareFields") or [{}]
            field = cf[0].get("SourceField", "") if isinstance(cf[0], dict) else ""
            listed = ", ".join(f"'{v}'" for v in approved)
            text = f"{field} values other than approved [{listed}] should not have any counts"
        elif vt == "NULL_CHECK":
            text = f"{names} should not have null or empty values"
        elif vt == "DUPLICATE_CHECK" and [f.upper() for f in fields] == ["ALL"]:
            text = "No two rows of the file should be identical in every field"
        elif vt == "DUPLICATE_CHECK":
            text = (f"{names} should not have duplicate values" if len(fields) < 2
                    else f"No two records should share the same {names}")
        elif vt == "FORMAT_CHECK":
            text = f"{names} values should match the expected format"
        else:
            continue
        case["ExpectedResult"] = text


def collect_approved_values(testcases: list, profile: dict, source_def: dict,
                            args, ask_fn=None) -> list:
    """
    Fills ApprovedValues for Record_Count and InApprovedValue test cases from
    the command line or by asking. Splits an InApprovedValue that names several
    fields into one test case per field (Data_Validation checks the first
    CompareField only). Drops the ones left without values. Returns the list,
    renumbered.
    """
    ask_fn = ask_fn or input
    interactive = _can_prompt(args)
    approved_opt = _parse_approved_option(getattr(args, "approved_values", None))
    expected = getattr(args, "expected_count", None)
    src = (profile or {}).get("source") or {}
    row_count = src.get("row_count")
    distinct = {str(c.get("name") or c.get("suggested_name")): c.get("distinct_count")
                for c in src.get("columns", []) or []}
    label = _source_label(source_def)

    # InApprovedValue candidates do not depend on the model: every profiled
    # field with a few fixed values (status, type, code …) is offered, plus any
    # field named in --approved-values.
    covered = set()
    for case in testcases:
        if (str(case.get("ValidationType") or "").upper() == "INAPPROVEDVALUE"
                and case.get("_side") != "target"):
            for f in case.get("CompareFields") or []:
                if isinstance(f, dict) and f.get("SourceField"):
                    covered.add(f["SourceField"])
    candidates = []
    n_sampled = src.get("sample_size") or src.get("sampled_rows") or 1000   # profiler samples up to 1000
    for col in src.get("columns", []) or []:
        name = str(col.get("name") or col.get("suggested_name") or "")
        n = col.get("distinct_count")
        ctype = str(col.get("type") or col.get("suggested_type") or "").upper()
        # Skip near-unique fields (IDs): every value differs, so an allowed
        # list would just be today's data.
        near_unique = isinstance(row_count, int) and row_count > 0 and n is not None \
            and isinstance(n, int) and n >= 0.9 * min(row_count, n_sampled or row_count)
        if (name and isinstance(n, int) and 2 <= n <= APPROVED_VALUES_MAX_DISTINCT
                and not near_unique
                and "DATE" not in ctype and "TIME" not in ctype):
            candidates.append((n, name))
    wanted = [name for _, name in sorted(candidates)][:MAX_APPROVED_CANDIDATES]
    wanted += [f for f in approved_opt if f not in wanted]
    for name in wanted:
        if name in covered:
            continue
        covered.add(name)
        testcases.append({
            "TestCaseNumber":  None,
            "Module":          (getattr(args, "module", None) or "").strip(),
            "Stage":           (getattr(args, "stage", None) or "").strip(),
            "ValidationType":  "InApprovedValue",
            "TestDescription": f"{name} has only approved values",
            VICEVERSA_KEY:     False,
            "Source":          copy.deepcopy(source_def),
            "CompareFields":   [{"SourceField": name}],
            "ApprovedValues":  [],
            "DataFilters":     [],
        })

    # One field per InApprovedValue test case.
    expanded = []
    for case in testcases:
        vt = str(case.get("ValidationType") or "").upper()
        fields = [f for f in (case.get("CompareFields") or [])
                  if isinstance(f, dict) and f.get("SourceField")]
        if vt == "INAPPROVEDVALUE" and len(fields) > 1:
            for f in fields:
                one = copy.deepcopy(case)
                one["CompareFields"] = [copy.deepcopy(f)]
                one["TestDescription"] = f"{f['SourceField']} has only approved values"
                expanded.append(one)
        else:
            expanded.append(case)
    seen_fields, testcases = set(), []
    for case in expanded:
        if str(case.get("ValidationType") or "").upper() == "INAPPROVEDVALUE":
            fields = case.get("CompareFields") or []
            name = fields[0].get("SourceField") if fields and isinstance(fields[0], dict) else None
            key = (case.get("_side", "source"), name)
            if key in seen_fields:
                continue
            seen_fields.add(key)
        testcases.append(case)

    def side_stats(side):
        part = (profile or {}).get(side) or {}
        cols = part.get("columns", []) or []
        return ({str(c.get("name") or c.get("suggested_name")): c.get("distinct_count") for c in cols},
                {str(c.get("name") or c.get("suggested_name")): str(c.get("type") or "").upper() for c in cols})
    stats = {"source": side_stats("source"), "target": side_stats("target")}

    # An approved list only makes sense for a field with a few fixed values
    # (status, type, code). Free text, IDs and dates are not offered.
    def unsuitable(field, side="source"):
        if field in approved_opt:
            return None                      # given explicitly — always kept
        distinct_s, types_s = stats[side]
        if field not in distinct_s:
            return "not in the profile"
        n = distinct_s.get(field)
        if "DATE" in types_s.get(field, "") or "TIME" in types_s.get(field, ""):
            return "date field"
        if isinstance(n, int) and n > APPROVED_VALUES_MAX_DISTINCT:
            return f"{n:,} different values"
        return None

    asked_header = False
    def header():
        nonlocal asked_header
        if not asked_header:
            print("\nA few test cases need values only you know (press Enter to skip one):")
            asked_header = True

    not_offered = []
    kept = []
    for case in testcases:
        vt = str(case.get("ValidationType") or "").upper()

        if vt == "RECORD_COUNT":
            value = expected
            if value is None and interactive:
                header()
                hint = f" — it returns {row_count:,} now" if isinstance(row_count, int) else ""
                print(f"\n  Record_Count — checks how many records {label} returns{hint}.")
                print("  Enter 0 if it should return no data (e.g. an exception view),")
                answer = ask_fn("  or the expected number of records: ").strip()
                value = answer.replace(",", "") if answer else None
                if value is None:
                    print("    skipped — Record_Count not created")
                    NOT_CREATED.append("Record_Count — skipped")
                    continue
            if value in (None, ""):
                logger.warning(f"Record_Count on {label} not created — no expected count. "
                               f"Pass --expected-count N to create it.")
                continue
            try:
                # Stored as text, like the existing test cases (["0"]).
                case["ApprovedValues"] = [str(int(str(value).strip()))]
            except ValueError:
                logger.warning(f"Record_Count on {label} not created — expected count "
                               f"'{value}' is not a whole number.")
                continue

        elif vt == "INAPPROVEDVALUE":
            fields = case.get("CompareFields") or []
            field = fields[0].get("SourceField") if fields and isinstance(fields[0], dict) else None
            if not field:
                continue
            side = case.get("_side", "source")
            side_def = case.get("Source") if isinstance(case.get("Source"), dict) else source_def
            side_label = _source_label(side_def)
            why = unsuitable(field, side)
            if why:
                not_offered.append(f"{field} ({why})")
                continue
            values = approved_opt.get(field)
            if values is None and interactive:
                header()
                current = _current_values(side_def, field)
                where = f" (target {side_label})" if side == "target" else ""
                print(f"\n  InApprovedValue{where} — checks that every '{field}' value is in an allowed list.")
                if current:
                    more = "" if len(current) <= 20 else f" … ({len(current)} in all)"
                    print(f"  Values in {side_label} now: {', '.join(current[:20])}{more}")
                hint = " ('all' = allow the values above)" if current else ""
                answer = ask_fn(f"  Allowed values, comma-separated{hint}: ").strip()
                if answer.lower() == "all" and current:
                    values = list(current)
                else:
                    values = [v.strip() for v in answer.split(",") if v.strip()] if answer else None
                if not values:
                    print(f"    skipped — InApprovedValue on '{field}' not created")
                    NOT_CREATED.append(f"InApprovedValue on '{field}'{where} — skipped")
                    continue
            if not values:
                logger.warning(f"InApprovedValue on '{field}' not created — no approved values. "
                               f"Pass --approved-values \"{field}=V1,V2\" to create it.")
                continue
            case["ApprovedValues"] = values

        kept.append(case)

    set_expected_results(kept)
    for case in kept:
        case.pop("_side", None)

    if not_offered:
        NOT_CREATED.append(f"InApprovedValue on {', '.join(not_offered)} — an allowed list only "
                           f"suits fields with up to {APPROVED_VALUES_MAX_DISTINCT} fixed values")

    for index, case in enumerate(kept, start=1):
        case["TestCaseNumber"] = index
    return kept


# ---------------------------------------------------------------------------
# Source / Target queries
# ---------------------------------------------------------------------------
# Data_Validation reads MongoDB through a VIEW or an aggregation PIPELINE and
# Oracle through a SQL QUERY. Instead of pointing a test case at a bare
# collection or table name, every database side is turned into a query that
# returns exactly the fields that test case uses:
#
#   SRC_TGT          JoinFields + CompareFields
#   Record_Count     the rows to count (Data_Validation counts the rows each
#                    side returns, so this is not a SELECT COUNT(*))
#   InApprovedValue  CompareFields
#   Null / Duplicate / Format_Check   CheckFields (+ ResultFields, DisplayField)
#
# DataFilters become part of the query as placeholders, so DefaultValue and
# run-time -f values keep working:
#   Oracle   WHERE STATUS = :STATUS
#   MongoDB  {"$match": {"Status": "$$Status"}}
# IN / NOT IN / BETWEEN filters: Oracle gets the DefaultValue list written
# into the SQL; MongoDB keeps the field in $project and the framework appends
# the filter as a final $match at run time.

_SIMPLE_FILTER_OPS = {"=": None, "!=": "$ne", "<>": "$ne", ">": "$gt", ">=": "$gte",
                      "<": "$lt", "<=": "$lte", "LIKE": "$regex"}


def _filter_op(entry: dict) -> str:
    return str(entry.get("Operator") or entry.get("OperatorType") or "=").strip().upper()


def _side_field_key(side: str) -> str:
    return "SourceField" if side == "Source" else "TargetField"


def _side_filters(case: dict, side: str) -> list:
    key = _side_field_key(side)
    return [f for f in (case.get("DataFilters") or [])
            if isinstance(f, dict) and f.get(key)]


def _fields_for_side(case: dict, side: str, record_count_fields: list = None) -> tuple:
    """(data_fields, null_check_fields) the test case reads on one side."""
    key = _side_field_key(side)
    vt = str(case.get("ValidationType") or "").upper()
    fields, nullable = [], []

    def add(name, target=fields):
        name = str(name or "").strip()
        if name and name not in target:
            target.append(name)

    if vt == "SRC_TGT":
        for group in ("JoinFields", "CompareFields"):
            for f in case.get(group) or []:
                if isinstance(f, dict):
                    add(f.get(key))
    elif vt == "RECORD_COUNT" and side == "Source":
        # Only the rows are counted; fetch the key fields so a failure shows
        # which records came back, without pulling whole documents.
        for name in record_count_fields or []:
            add(name)
    elif vt == "INAPPROVEDVALUE" and side == "Source":
        for f in case.get("CompareFields") or []:
            if isinstance(f, dict):
                add(f.get("SourceField"))
    elif vt in ("NULL_CHECK", "DUPLICATE_CHECK", "FORMAT_CHECK") and side == "Source":
        for f in case.get("CheckFields") or []:
            if not isinstance(f, dict):
                continue
            name = f.get("SourceField") or f.get("Field")
            for one in (name if isinstance(name, list) else [name]):
                add(one)
                if vt == "NULL_CHECK":
                    add(one, nullable)
            for extra in (f.get("ResultFields") or []):
                add(extra)
            add(f.get("DisplayField"))
    return fields, nullable


def _oracle_ident(name: str) -> str:
    name = str(name)
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", name) else '"' + name.replace('"', '""') + '"'


def _oracle_table(definition: dict) -> str:
    query = str(definition.get("Query") or "").strip()
    if "." in query:
        return query
    db = definition.get("DB")
    return f"{db}.{query}" if db else query


def build_oracle_query(case: dict, side: str, definition: dict,
                       record_count_fields: list = None) -> str:
    """SELECT the fields this test case uses, with DataFilters as :placeholders."""
    from Libraries.parameter_reading import _to_oracle_value
    vt = str(case.get("ValidationType") or "").upper()
    fields, _ = _fields_for_side(case, side, record_count_fields)
    key = _side_field_key(side)

    if fields:
        select = ", ".join(_oracle_ident(f) for f in fields)
    else:
        select = "*"

    conditions = []
    for f in _side_filters(case, side):
        field = f[key]
        col = _oracle_ident(field)
        op = _filter_op(f)
        default = "" if f.get("DefaultValue") is None else str(f.get("DefaultValue")).strip()
        if op in ("IN", "NOT IN") and default:
            values = ", ".join(_to_oracle_value(v.strip()) for v in default.split(","))
            conditions.append(f"{col} {op} ({values})")
        elif op == "BETWEEN" and default.count(",") == 1:
            low, high = (v.strip() for v in default.split(","))
            conditions.append(f"{col} BETWEEN {_to_oracle_value(low)} AND {_to_oracle_value(high)}")
        else:
            # Empty DefaultValue keeps the placeholder; the loader then reports
            # the missing DefaultValue instead of loading a broken query.
            sql_op = "=" if op not in _SIMPLE_FILTER_OPS else ("!=" if op == "<>" else op)
            conditions.append(f"{col} {sql_op} :{field}")

    sql = f"SELECT {select} FROM {_oracle_table(definition)}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    return sql


def _drop_child_paths(paths: list) -> list:
    """$project rejects 'Plan' together with 'Plan.Id' — keep the parent."""
    kept = []
    for p in paths:
        if any(p.startswith(other + ".") for other in paths if other != p):
            continue
        kept.append(p)
    return kept


def build_mongo_pipeline(case: dict, side: str, record_count_fields: list = None) -> list:
    """Aggregation returning the fields this test case uses."""
    vt = str(case.get("ValidationType") or "").upper()
    fields, nullable = _fields_for_side(case, side, record_count_fields)
    key = _side_field_key(side)

    match, appended = {}, []
    for f in _side_filters(case, side):
        field = f[key]
        op = _filter_op(f)
        if op in _SIMPLE_FILTER_OPS:
            mongo_op = _SIMPLE_FILTER_OPS[op]
            placeholder = f"$${field}"
            match[field] = placeholder if mongo_op is None else {mongo_op: placeholder}
        else:
            appended.append(field)          # IN / NOT IN / BETWEEN — $match added at run time

    stages = []
    if match:
        stages.append({"$match": match})

    project_fields = _drop_child_paths(list(dict.fromkeys(fields + appended)))
    projection = {}
    if "_id" not in project_fields:
        projection["_id"] = 0
    for name in project_fields:
        # Null_Check: a missing field comes back as null, so it is counted.
        projection[name] = {"$ifNull": [f"${name}", None]} if name in nullable else 1
    if vt == "RECORD_COUNT" and not appended and not project_fields:
        projection = {"_id": 1}
    if len(projection) == 1 and projection.get("_id") == 0:
        projection = {"_id": 1}             # nothing named — rows still counted
    stages.append({"$project": projection})
    return stages


def build_side_queries(testcases: list, record_count_fields: list = None) -> list:
    """
    Rewrites every MongoDB side as QueryType PIPELINE (Collection + Query) and
    every Oracle side as QueryType QUERY (a SELECT), built from the fields each
    test case uses. File and EDI835 sides are left as they are. In place.
    """
    for case in testcases:
        if not isinstance(case, dict):
            continue
        for side in ("Source", "Target"):
            definition = case.get(side)
            if not isinstance(definition, dict):
                continue
            stype = str(definition.get("Type") or "").upper()
            qtype = str(definition.get("QueryType") or "").upper()
            query = str(definition.get("Query") or "")

            if stype == "MONGODB" and qtype in ("COLLECTION", "VIEW", "TABLE", ""):
                pipeline = build_mongo_pipeline(case, side, record_count_fields)
                case[side] = {
                    "Type":        "MONGODB",
                    "Environment": definition.get("Environment"),
                    "DB":          definition.get("DB"),
                    "Collection":  definition.get("Collection") or query,
                    "QueryType":   "PIPELINE",
                    "Query":       json.dumps(pipeline),
                }
            elif stype == "ORACLE" and "select " not in query.lower():
                case[side] = {
                    "Type":        "ORACLE",
                    "Environment": definition.get("Environment"),
                    "DB":          definition.get("DB"),
                    "QueryType":   "QUERY",
                    "Query":       build_oracle_query(case, side, definition,
                                                      record_count_fields),
                }
    return testcases


def save_testcases(testcases: list, testcase_dir: str, args) -> str:
    """Writes the test case array into TestCase_Dir and returns the path."""
    os.makedirs(testcase_dir, exist_ok=True)
    path = os.path.join(testcase_dir, output_filename(args))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(testcases, fh, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SHORTHAND_HELP = r"""
Short form  (-s for the source, -t for the target; -t is optional)
  Database  TYPE:ENVIRONMENT:OBJECT
    MONGODB:MCPQA1:MCP.Contract          database MCP, collection Contract
    ORACLE:ORACLE_HTAQA:DW.CONTRACT      owner DW, table CONTRACT
  File      TYPE:FORMAT:FOLDER\FILE_OR_PATTERN
    UNC_PATH:CSV:\\server\share\In\MEMBER_*.csv
    LOCAL_PATH:PIPE:C:\Data\claims_20261001.txt
    SFTP:CSV:/outbound/members/MBR_*.csv     (add --source-credential KEY)
  Ollama check only
    -s TEST_CONNECTION

Examples
  python Test_Case_Generator.py -m MCPDW -s MONGODB:MCPQA1:MCP.Contract -t ORACLE:ORACLE_HTAQA:DW.CONTRACT
  python Test_Case_Generator.py -m M3P -s MONGODB:MCPQA1:M3P.Test_M3P_Cap_Data -t MONGODB:MCPQA1:M3P.Test_M3P_Stg_Data --profile-only
  One collection stored in several tables (arrays in child tables):
  python Test_Case_Generator.py -m MCPDW -s MONGODB:MCPQA1:MCP.Contract -t ORACLE:ORACLE_HTAQA:DW.CONTRACT*
  (or repeat -t per table; fix a wrong match with --map "CONTRACT_ADD_ATTR=AdditionalAttribute")
  Source only (asks for the expected count and approved values):
  python Test_Case_Generator.py -m MCPDW -s MONGODB:MCPQA1:MCP.Contract
  Source only, nothing asked:
  python Test_Case_Generator.py -m MCPDW -s MONGODB:MCPQA1:MCP.Contract --expected-count 2412 --approved-values "Status.StatusID=1,2"

Files (any format Data_Validation reads; file vs database, database vs file, file vs file)
  python Test_Case_Generator.py -m MEMBERFEED -s UNC_PATH:CSV:\\server\share\In\MEMBER_*.csv -t ORACLE:ORACLE_HTAQA:DW.MEMBER
  python Test_Case_Generator.py -m MEMBERFEED -s ORACLE:ORACLE_HTAQA:DW.MEMBER -t UNC_PATH:PIPE:\\server\share\Out\MEMBER_*.txt
  python Test_Case_Generator.py -m CLAIMS -s LOCAL_PATH:XML:C:\Data\claims.xml --source-file-config "{\"RecordTag\": \"Claim\"}" -t MONGODB:MCPQA1:MCP.Claims
  python Test_Case_Generator.py -m CLAIMS -s LOCAL_PATH:FIXED_WIDTH:C:\Data\claims.dat --source-columns layout.json --source-no-header -t ORACLE:ORACLE_HTAQA:DW.CLAIM
  EDI 271 — one file into many tables (each table matched to its 271 table; fix with --map TABLE=BENEFIT or TABLE=SKIP):
  python Test_Case_Generator.py -m ELIG271 -s UNC_PATH:EDI271:\\server\share\271\*.x12 -t ORACLE:ORACLE_HTAQA:DW.ELIG271_*
  python Test_Case_Generator.py -m ELIG271 -s UNC_PATH:EDI271:\\server\share\271\*.x12 --file-table BENEFIT     (file checks only)
  Tables joined by generated IDs (RECON.EDI_271_*): a mapping file says how the file is loaded:
  python Test_Case_Generator.py -m ENROLL834 -s UNC_PATH:EDI834:\\server\share\834\*.x12 -t ORACLE:ORACLE_HTAQA:DW.ENROLLMENT_*
  python Test_Case_Generator.py -m ENROLL834 -s UNC_PATH:EDI834:\\server\share\834\*.x12 --file-table COVERAGE -t ORACLE:ORACLE_HTAQA:DW.ENROLLMENT_COVERAGE
  python Test_Case_Generator.py -m CLAIMS837 -s UNC_PATH:EDI837:\\server\share\837\*.x12 -t ORACLE:ORACLE_HTAQA:DW.CLAIM_*
  python Test_Case_Generator.py -m CLAIMS837 -s UNC_PATH:EDI837:\\server\share\837\*.x12 --file-table LINE     (file checks only)
  python Test_Case_Generator.py -m EDI271_RECON -s UNC_PATH:EDI271:\\server\share\271\R-*.x12 -t ORACLE:ORACLE_HTAQA:RECON.EDI_271_* --map-file ..\Config\EDI271_RECON_Map.json --file-detail-id 12345

The long options (--source-type, --source-env, ...) still work and can be
combined with the short form; a long option given explicitly wins.
"""

_TYPE_ALIASES = {"MONGO": "MONGODB", "ORA": "ORACLE"}


def _shorthand_error(side: str, spec: str, why: str) -> SystemExit:
    return SystemExit(
        f"ERROR: -{side[0]} {spec!r}: {why}\n"
        f"  Database: -{side[0]} MONGODB:MCPQA1:MCP.Contract   or   "
        f"-{side[0]} ORACLE:ORACLE_HTAQA:DW.CONTRACT\n"
        f"  File:     -{side[0]} UNC_PATH:CSV:\\\\server\\share\\folder\\MEMBER_*.csv"
    )


def expand_shorthand(args) -> None:
    """
    Expands -s / -t TYPE:ENV:OBJECT (or TYPE:FORMAT:LOCATION for files) into
    the existing --source-* / --target-* attributes. Values given with the long
    options are never overwritten, so both styles can be mixed.
    """
    # -t may be repeated: the first one fills --target-*, the rest are kept
    # for the multi-table (array) flow.
    targets = getattr(args, "target", None)
    if isinstance(targets, list):
        args.target = targets[0] if targets else None
        args.extra_targets = targets[1:]
    elif not hasattr(args, "extra_targets"):
        args.extra_targets = []

    for side in ("source", "target"):
        spec = getattr(args, side, None)
        if not spec:
            continue

        def put(name, value):
            if value not in (None, "") and not getattr(args, f"{side}_{name}", None):
                setattr(args, f"{side}_{name}", value)

        parts = [p.strip() for p in spec.split(":", 2)]
        type_name = _TYPE_ALIASES.get(parts[0].upper(), parts[0].upper())
        put("type", type_name)

        if is_test_connection(type_name):
            continue
        if len(parts) < 3 or not parts[1] or not parts[2]:
            raise _shorthand_error(side, spec, "expected three parts separated by ':'")

        if type_name == "ORACLE":
            if "." not in parts[2]:
                raise _shorthand_error(side, spec, "Oracle object must be OWNER.TABLE")
            put("env", parts[1])
            put("table", parts[2])
        elif type_name == "MONGODB":
            put("env", parts[1])
            db, sep, coll = parts[2].partition(".")
            if sep:
                put("db", db)
                put("collection", coll)
            else:
                put("collection", parts[2])
        else:
            # File source: TYPE:FORMAT:FOLDER\NAME — NAME with * or ? is a pattern
            put("format", parts[1].upper())
            location = parts[2]
            cut = max(location.rfind("\\"), location.rfind("/"))
            folder, name = (location[:cut], location[cut + 1:]) if cut >= 0 else ("", location)
            if not name:
                raise _shorthand_error(side, spec, "give a file name or pattern after the folder")
            put("path", folder or None)
            if any(ch in name for ch in "*?"):
                put("pattern", name)
            else:
                put("file", name)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="Test_Case_Generator.py",
        description="Profile a source and target, generate test cases via "
                    "Ollama, load them into DataRegressionSet and save them as "
                    "JSON into TestCase_Dir. Incomplete or invalid test cases "
                    "are not loaded; the reason is shown in the run log and "
                    "written into the JSON file under _LoadStatus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=SHORTHAND_HELP,
    )

    p.add_argument("--config", help="Path to config INI "
                                    "(default: agent_config.ini)")
    p.add_argument("--interactive", action="store_true",
                   help="Prompt for each value instead of using flags")
    p.add_argument("-m", "--module", default="",
                   help="Optional module label for the generated test cases")
    p.add_argument("-s", "--source", metavar="TYPE:ENV:OBJECT",
                   help="Short form of the --source-* options, e.g. "
                        "MONGODB:MCPQA1:MCP.Contract (see examples below)")
    p.add_argument("-t", "--target", metavar="TYPE:ENV:OBJECT", action="append",
                   help="Short form of the --target-* options, e.g. "
                        "ORACLE:ORACLE_HTAQA:DW.CONTRACT. Repeat -t, or use a "
                        "wildcard (DW.CONTRACT*), when one MongoDB collection "
                        "is stored in several Oracle tables")
    p.add_argument("--map", metavar="TABLE=ARRAY;...",
                   help="Which array of the collection each Oracle table holds, "
                        "when the automatic match is wrong, e.g. "
                        "\"CONTRACT_ADD_ATTR=AdditionalAttribute;"
                        "CONTRACT_PRICING_SOURCE_PRIORITY=ContractConfigX.PricingSources.PriorityOrder\". "
                        "ROOT = the document itself, SKIP = leave the table out")
    p.add_argument("--map-file", metavar="JSON",
                   help="Mapping file for a file loaded into parent/child tables "
                        "joined by generated IDs, e.g. Config\\EDI271_RECON_Map.json: "
                        "which file table each database table holds, its columns and "
                        "its parent. Tables listed there use it; others are matched "
                        "automatically")
    p.add_argument("--file-detail-id", metavar="ID",
                   help="With --map-file: the file's ID in the database "
                        "(FILE_DETAIL_ID_NH), so only this file's rows are compared. "
                        "Asked for when not given")
    p.add_argument("--file-table", metavar="TABLE",
                   help="EDI271 / EDI834 / EDI837 files: which table of the file to "
                        "compare or check (271: MEMBER, BENEFIT, BENEFIT_DATE …; "
                        "834: MEMBER, COVERAGE, COB …; 837: CLAIM, LINE, CLAIM_DIAGNOSIS …). "
                        "Not needed with Oracle targets: each table is matched "
                        "to its file table automatically (fix with --map)")
    p.add_argument("--stage", default="",
                   help="Optional stage label for the generated test cases")
    p.add_argument("--model", help="Override OLLAMA_MODEL from config")
    p.add_argument("--ollama-api", help="Override OLLAMA_API from config")
    p.add_argument("--testcase-dir", help="Override TestCase_Dir from config")
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                   help=f"Sampling temperature (default {DEFAULT_TEMPERATURE} "
                        f"— greedy, so repeated runs match). Raise it only if "
                        f"you want varied suggestions.")
    p.add_argument("--count", type=int, default=DEFAULT_MIN_TESTCASES,
                   help=f"Minimum test cases to generate (default "
                        f"{DEFAULT_MIN_TESTCASES}). Enforced by the response "
                        f"schema, so the model cannot stop short.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"Ollama sampling seed (default {DEFAULT_SEED}). Same "
                        f"seed plus same prompt gives the same test cases.")
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                   help=f"Ollama context window, prompt + response "
                        f"(default {DEFAULT_NUM_CTX}). Raise this if the "
                        f"generated JSON comes back truncated.")
    p.add_argument("--num-predict", type=int, default=DEFAULT_NUM_PREDICT,
                   help=f"Max tokens in the reply (default {DEFAULT_NUM_PREDICT})")
    p.add_argument("--no-json-format", action="store_true",
                   help="Disable Ollama JSON mode (it constrains output to "
                        "valid JSON; turn off only if the model misbehaves)")
    p.add_argument("--timeout", type=int, default=120,
                   help="Ollama request timeout in seconds")
    p.add_argument("--prompt-only", action="store_true",
                   help="Print the prompt and exit — do not call Ollama")
    p.add_argument("--profile-only", action="store_true",
                   help="Print the data profile and exit")
    # Test cases are always loaded into DataRegressionSet after generation.
    # These switches are kept hidden: --load is accepted (and ignored) so older
    # commands still work; --no-load / --load-preview are for troubleshooting.
    p.add_argument("--load", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-load", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--load-preview", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--expected-count", type=int, metavar="N",
                   help="Expected source record count for the Record_Count test "
                        "case (source-only runs). Asked for when not given.")
    p.add_argument("--approved-values", metavar="FIELD=V1,V2;FIELD2=...",
                   help="Approved values for InApprovedValue test cases, e.g. "
                        "\"Status.StatusID=1,2,3;Business.Business_ID=IND\". "
                        "Asked for when not given.")
    p.add_argument("--no-target-checks", action="store_true",
                   help="Do not add Duplicate_Check / Null_Check / InApprovedValue "
                        "test cases on the target table(s)")
    p.add_argument("--no-row-duplicates", action="store_true",
                   help="Do not add the Duplicate_Check on every file that reports rows "
                        "repeated in all fields (added by default for each file side)")
    p.add_argument("--no-prompt", action="store_true",
                   help="Never ask questions; test cases still needing values "
                        "are not created")
    p.add_argument("--save-prompt", action="store_true",
                   help="Also save the prompt alongside the test case file")
    p.add_argument("--quiet", action="store_true",
                   help="Print only the output file path")
    p.add_argument("--debug", action="store_true",
                   help="Print full tracebacks and debug logging")

    for side in ("source", "target"):
        g = p.add_argument_group(f"{side} definition")
        # TEST CONNECTION MODE — mentioned in the source help text only.
        type_help = f"One of: {', '.join(ALL_TYPES)}"
        if side == "source":
            type_help += (f". {TEST_CONNECTION_TYPE} tests the Ollama "
                          f"endpoint and exits")
        g.add_argument(f"--{side}-type", help=type_help)
        g.add_argument(f"--{side}-format",
                       help=f"Required for file types. One of: "
                            f"{', '.join(FILE_FORMATS)}")
        g.add_argument(f"--{side}-env", help="Environment key from environment.ini")
        g.add_argument(f"--{side}-table", help="ORACLE only — OWNER.TABLE")
        g.add_argument(f"--{side}-db", help="MONGODB only — database name")
        g.add_argument(f"--{side}-collection", help="MONGODB only — collection")
        g.add_argument(f"--{side}-path", help="Directory path")
        g.add_argument(f"--{side}-file", help="Exact file name")
        g.add_argument(f"--{side}-pattern", help="File pattern, e.g. PAY_*.txt")
        g.add_argument(f"--{side}-pick", default="LATEST",
                       choices=["LATEST", "ALL"])
        g.add_argument(f"--{side}-delimiter")
        g.add_argument(f"--{side}-no-header", action="store_true",
                       help="File has no header row")
        g.add_argument(f"--{side}-skip-rows", type=int, default=0)
        g.add_argument(f"--{side}-footer-rows", type=int, default=0)
        g.add_argument(f"--{side}-encoding")
        g.add_argument(f"--{side}-credential", help="CredentialKey for SFTP/S3")
        g.add_argument(f"--{side}-columns",
                       help="JSON file with column layout "
                            "(required for FIXED_WIDTH and PDF)")
        g.add_argument(f"--{side}-file-config", metavar="JSON",
                       help="Extra FileConfig keys as JSON text or a .json file, "
                            "e.g. '{\"RecordTag\": \"Claim\"}' (XML), "
                            "'{\"RecordPath\": \"data.items\"}' (JSON), "
                            "'{\"Sheet\": \"Members\"}' (EXCEL)")

    return p


def log(message: str, quiet: bool) -> None:
    if not quiet:
        print(message)


# ---------------------------------------------------------------------------
# One collection, several Oracle tables (arrays stored in child tables)
# ---------------------------------------------------------------------------

def _parse_map_option(text: str) -> dict:
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            table, path = part.split("=", 1)
            out[table.strip().split(".")[-1].upper()] = path.strip()
    return out


def _list_oracle_tables(env: str, spec: str) -> list:
    """DW.CONTRACT* → ['DW.CONTRACT', 'DW.CONTRACT_ADD_ATTR', ...]"""
    from Libraries.OracleDB import OracleConnection
    owner, _, pattern = spec.partition(".")
    like = pattern.upper().replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    like = like.replace("*", "%").replace("?", "_")
    rows = OracleConnection(env.upper()).fetchAllDict(
        f"SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = '{owner.upper()}' "
        f"AND TABLE_NAME LIKE '{like}' ESCAPE '\\' ORDER BY TABLE_NAME")
    return [f"{owner.upper()}.{r.get('TABLE_NAME') or r.get('table_name')}" for r in rows]


def _oracle_columns(env: str, table: str) -> list:
    profile = DataProfiler().profile_oracle({"Environment": env, "Query": table})
    return [str(c["name"]) for c in profile.get("columns", []) if c.get("name")]


def _file_format(definition: dict) -> str:
    return str(((definition or {}).get("FileConfig") or {}).get("Format") or "").upper()


def _resolve_oracle_targets(args) -> tuple:
    """[(env, OWNER.TABLE)] from -t (repeated) and wildcards; and whether there are several."""
    extra = getattr(args, "extra_targets", None) or []
    specs = [(args.target_env, args.target_table)]
    for spec in extra:
        parts = [p.strip() for p in spec.split(":", 2)]
        if len(parts) != 3 or _TYPE_ALIASES.get(parts[0].upper(), parts[0].upper()) != "ORACLE" \
                or "." not in parts[2]:
            raise SystemExit(f"ERROR: -t {spec!r}: every extra target must be "
                             f"ORACLE:ENVIRONMENT:OWNER.TABLE")
        specs.append((parts[1], parts[2]))
    multi = len(specs) > 1 or any("*" in t or "?" in t for _, t in specs)

    tables = []
    for env, table in specs:
        if "*" in table or "?" in table:
            found = _list_oracle_tables(env, table)
            if not found:
                raise SystemExit(f"ERROR: no Oracle table matches {table} in {env}")
            tables += [(env, t) for t in found]
        else:
            tables.append((env, table.upper() if table.isupper() else table))
    seen, unique = set(), []
    for env, table in tables:
        if table.upper() not in seen:
            seen.add(table.upper())
            unique.append((env, table))
    return unique, multi


def plan_file_tables(args, source_def: dict, target_def: dict) -> dict:
    """
    EDI271 / EDI834 / EDI837 (one file, several tables). Returns the same plan shape as
    plan_target_tables.
      no target            → the usual flow on --file-table (default MEMBER)
      a MONGODB target, or
      --file-table given   → the usual flow on that table
      ORACLE table(s)      → each table matched to its file table; one ready
                             SRC_TGT per table, plus a Duplicate_Check on the
                             file table's key and on the database table's key
    """
    from Libraries import File_Table_Mapper as FT
    plan = {"target_def": target_def, "child_cases": [], "model_flow": True, "lines": [],
            "file_checks": []}
    fmt = _file_format(source_def)
    file_config = source_def.setdefault("FileConfig", {})
    extra = getattr(args, "extra_targets", None) or []
    ttype = str(getattr(args, "target_type", None) or "").upper()

    if not target_def or ttype != "ORACLE":
        if extra:
            raise SystemExit("ERROR: several targets (-t … -t …) are supported with ORACLE tables only")
        if not file_config.get("Table"):
            file_config["Table"] = DEFAULT_FILE_TABLE.get(fmt, "MEMBER")
            plan["lines"] = [f"  {fmt} table {file_config['Table']} (choose another with --file-table)"]
        return plan

    tables, _ = _resolve_oracle_targets(args)
    try:
        file_tables = FT.read_file_tables(source_def)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise SystemExit(f"ERROR: could not read the {fmt} file: {exc}")
    total = {k: len(v) for k, v in file_tables.items() if len(v)}
    plan["lines"].append("  file tables with rows: " + ", ".join(f"{k}={v}" for k, v in total.items()))

    spec = None
    if getattr(args, "map_file", None):
        try:
            spec = FT.load_map(args.map_file)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"ERROR: --map-file {args.map_file}: {exc}")
        filter_value = _file_detail_id(args, spec)
        if spec.get("file_filter_column"):
            plan["lines"].append(
                f"  rows of {spec['file_filter_column']} = {filter_value} only" if filter_value not in (None, "")
                else f"  {spec['file_filter_column']} not given — whole tables are compared")
        if filter_value in (None, "") and spec.get("file_filter_column"):
            NOT_CREATED.append(f"No {spec['file_filter_column']} given: test cases compare the whole "
                               f"tables, so rows of other files show as missing. Use --file-detail-id")

    forced = _parse_map_option(getattr(args, "map", None))
    if file_config.get("Table") and len(tables) == 1:
        # --file-table with one table: that is the table it holds
        forced.setdefault(tables[0][1].split(".")[-1].upper(), file_config["Table"])
    for env, table in tables:
        try:
            columns = _oracle_columns(env, table)
        except Exception as exc:
            plan["lines"].append(f"  {table:<40} not read — {exc}")
            NOT_CREATED.append(f"{table} — columns could not be read")
            continue
        name = table.split(".")[-1].upper()
        if spec and table.upper() in spec["tables"]:
            _plan_spec_table(plan, spec, table.upper(), env, columns, source_def, file_tables,
                             filter_value, args)
            continue
        file_table, why = FT.match_table(table, columns, file_tables, forced.get(name), fmt)
        if file_table is None:
            plan["lines"].append(f"  {table:<40} not matched — {why}")
            if "skipped" not in why:
                NOT_CREATED.append(f"{table} — no {fmt} table matches it; "
                                   f"use --map \"{name}=BENEFIT\" (or SKIP)")
            continue
        frame = file_tables[file_table]
        case, info = FT.build_child_testcase(table, columns, file_table, frame, source_def, env, fmt)
        joins = ", ".join(f"{p['field']}={p['column']}" for p in info["joins"]) or "NONE FOUND"
        plan["lines"].append(f"  {table:<40} <- {fmt} {file_table} ({info['rows']} row(s) in the file; {why})")
        plan["lines"].append(f"  {'':<40}    join on {joins}; {len(info['compares'])} column(s) compared"
                             + (f"; not compared: {', '.join(info['not_compared'])}"
                                if info["not_compared"] else ""))
        if not info["joins"]:
            NOT_CREATED.append(f"{table} — no key column pairs with {file_table}; add JoinFields by hand")
            continue
        if not info["join_unique"]:
            NOT_CREATED.append(f"{table} — join {joins} is not unique in the file; check JoinFields")
        plan["child_cases"].append(case)
        if not getattr(args, "no_target_checks", False) and info["rows"]:
            plan["file_checks"].append(FT.file_duplicate_case(case))

    plan["target_def"] = None
    plan["model_flow"] = False
    return plan


def _file_detail_id(args, spec: dict):
    """--file-detail-id, or asked for; None compares whole tables."""
    value = getattr(args, "file_detail_id", None)
    if value not in (None, "") or not spec.get("file_filter_column"):
        return value
    if _can_prompt(args):
        try:
            value = input(f"{spec['file_filter_column']} of this file in the database "
                          f"(Enter = compare whole tables): ").strip()
        except EOFError:
            value = ""
        args.file_detail_id = value or None
    return value or None


def _plan_spec_table(plan: dict, spec: dict, table: str, env: str, columns: list,
                     source_def: dict, file_tables: dict, filter_value, args) -> None:
    """Test cases for one table listed in the mapping file."""
    from Libraries import File_Table_Mapper as FT
    missing = FT.spec_columns_missing(spec, table, columns)
    if missing:
        plan["lines"].append(f"  {table:<40} columns in the mapping file but not in the table: "
                             f"{', '.join(missing)}")
        NOT_CREATED.append(f"{table} — mapping names columns the table does not have: "
                           f"{', '.join(missing)}")
        return
    for test in spec["tables"][table].get("tests") or []:
        file_table = test["file_table"].upper()
        frame = file_tables.get(file_table)
        if frame is None:
            NOT_CREATED.append(f"{table} — {file_table} is not a {FT.label(_file_format(source_def))} table")
            continue
        try:
            case, info = FT.build_spec_testcase(spec, table, test, source_def, env, frame, filter_value)
        except ValueError as exc:
            NOT_CREATED.append(str(exc))
            continue
        how = "" if info["linked"] else "  [presence only — table has no link to the file]"
        plan["lines"].append(f"  {table:<40} <- {FT.label(_file_format(source_def))} {file_table} "
                             f"({info['rows']} row(s) in the file; "
                             f"mapping file){how}")
        plan["lines"].append(f"  {'':<40}    join on {info['joins']}; {info['compared']} column(s) compared")
        if info["note"]:
            plan["lines"].append(f"  {'':<40}    note: {info['note']}")
        plan["file_checks"].append(case)
        if info["linked"] and not getattr(args, "no_target_checks", False):
            plan["file_checks"].append(FT.spec_target_duplicate(case))


def plan_target_tables(args, source_def: dict, target_def: dict) -> dict:
    """
    For a MongoDB source and Oracle target(s): works out which table holds the
    document itself and which hold its arrays.

    Returns {"target_def", "child_cases", "model_flow", "lines"}:
      target_def  — the table for the usual flow (the document-level table),
                    or None when every table holds an array
      child_cases — one SRC_TGT per array table, ready to run
      model_flow  — False when there is no document-level table
      lines       — the table → array summary for the run log
    A single target that holds the document itself returns the usual flow
    unchanged.
    """
    from Libraries import Array_Profiler as AP
    plan = {"target_def": target_def, "child_cases": [], "model_flow": True, "lines": []}
    extra = getattr(args, "extra_targets", None) or []
    if _file_format(source_def) in MULTI_TABLE_FORMATS:
        return plan_file_tables(args, source_def, target_def)
    if (str(source_def.get("Type") or "").upper() != "MONGODB"
            or str(args.target_type or "").upper() != "ORACLE"):
        if extra:
            raise SystemExit("ERROR: several targets (-t … -t …) are supported for a "
                             "MONGODB source or an EDI271/EDI834/EDI837 file with ORACLE tables only")
        return plan

    tables, multi = _resolve_oracle_targets(args)

    collection = source_def.get("Query") or ""
    try:
        docs, schema = AP.read_collection(source_def)
        levels = AP.discover_levels(docs, schema)
    except Exception as exc:
        if not multi:
            logger.warning(f"Could not read the arrays of {collection}: {exc}")
            return plan
        raise SystemExit(f"ERROR: could not read the arrays of {collection}: {exc}")

    has_arrays = len(levels) > 1
    if not multi and not has_arrays:
        return plan

    forced = _parse_map_option(getattr(args, "map", None))
    roots, lines = [], []
    for env, table in tables:
        try:
            columns = _oracle_columns(env, table)
        except Exception as exc:
            lines.append(f"  {table:<40} not read — {exc}")
            NOT_CREATED.append(f"{table} — columns could not be read")
            continue
        name = table.split(".")[-1].upper()
        path, why = AP.match_table(table, columns, levels, collection, forced.get(name))
        if path is None:
            lines.append(f"  {table:<40} not matched — {why}")
            if "skipped" not in why:
                NOT_CREATED.append(f"{table} — no array of {collection} matches it; "
                                   f"use --map \"{name}=Array.Path\"")
            continue
        if path == "":
            roots.append((env, table))
            lines.append(f"  {table:<40} <- {collection} (the document itself)")
            continue
        path, flattened = AP.flatten_level(columns, levels, path)
        if flattened:
            why += f"; inner array {', '.join(f + '[]' for f in flattened)} flattened into the same rows"
        case, info = AP.build_child_testcase(table, columns, levels, path, source_def, env,
                                             collection, flattened)
        joins = ", ".join(j["field"].split(".")[-1] for j in info["joins"]) or "NONE FOUND"
        lines.append(f"  {table:<40} <- {AP.describe_level(path, collection)}   ({why})")
        lines.append(f"  {'':<40}    join on {joins}; {len(info['compares'])} field(s) compared"
                     + (f"; not compared: {', '.join(info['not_compared'])}" if info["not_compared"] else ""))
        plan["child_cases"].append(case)

    if not multi and not plan["child_cases"]:
        return plan                            # single table holding the document: usual flow
    plan["lines"] = lines
    if roots:
        env, table = roots[0]
        plan["target_def"] = {"Type": "ORACLE", "Environment": env,
                              "DB": table.split(".")[0], "Query": table, "QueryType": "QUERY"}
        args.target_env, args.target_table = env, table
        for env2, extra_root in roots[1:]:
            NOT_CREATED.append(f"{extra_root} — also matches the document itself; only "
                               f"{table} is compared (use --map to point it at an array)")
    else:
        plan["target_def"] = None
        plan["model_flow"] = False
    return plan


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    NOT_CREATED.clear()
    FILE_FRAMES.clear()
    expand_shorthand(args)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    ollama_api   = args.ollama_api   or config["OLLAMA_API"]
    ollama_model = args.model        or config["OLLAMA_MODEL"]
    testcase_dir = args.testcase_dir or config["TestCase_Dir"]

    # TEST CONNECTION MODE — flag form, before anything else is touched.
    if is_test_connection(args.source_type):
        return run_test_connection(ollama_api, ollama_model)

    if args.interactive or not args.source_type:
        print("QA Automation Framework — AI Test Case Generator")
        if config.get("_config_file"):
            print(f"Config: {config['_config_file']}")
        print(f"Ollama: {ollama_api}   Model: {ollama_model}")
        print(f"Output: {testcase_dir}")

        # Source and target are the only mandatory input. Module and Stage
        # are optional labels — supply them with --module / --stage.
        interactive_side("source", args)

        # TEST CONNECTION MODE — interactive form.
        if is_test_connection(args.source_type):
            print()
            return run_test_connection(ollama_api, ollama_model)

        interactive_side("target", args)
        print()

    # Target is optional. Without one, only source-only checks are generated
    # (Record_Count, Null_Check, Duplicate_Check, Format_Check, InApprovedValue).
    validate_side("source", args)
    if args.target_type:
        validate_side("target", args)

    source_def = build_definition("source", args)
    target_def = build_definition("target", args) if args.target_type else None

    # -- One collection stored in several tables ----------------------------
    # Each Oracle table is matched to the level of the collection it holds:
    # the document itself (usual flow below) or one of its arrays (a ready
    # SRC_TGT that unwinds that array).
    if _file_format(source_def) in MULTI_TABLE_FORMATS:
        plan = plan_file_tables(args, source_def, target_def)
    else:
        plan = plan_target_tables(args, source_def, target_def) if target_def else \
            {"target_def": None, "child_cases": [], "model_flow": True, "lines": []}
    target_def = plan["target_def"]
    if plan["lines"] and not args.quiet:
        what = (f"{source_def.get('DB')}.{source_def.get('Query')}"
                if source_def.get("Type") in ("MONGODB", "ORACLE")
                else (source_def.get("FilePattern") or source_def.get("FileName")))
        print(f"Tables for {what}:")
        for line in plan["lines"]:
            print(line)

    started = time.time()
    testcases = []
    if plan["model_flow"]:
        # -- Profile (BRD Section 9.3) ------------------------------------------

        log("Profiling source and target...", args.quiet)
        profiler = DataProfiler()

        try:
            profile = profiler.profile(source_def, target_def)
        except FileNotFoundError as exc:
            print(f"ERROR: FILE_NOT_FOUND — {exc}", file=sys.stderr)
            return 2
        except PermissionError as exc:
            print(f"ERROR: ACCESS_DENIED — cannot read file, check permissions "
                  f"— {exc}", file=sys.stderr)
            return 3
        except Exception as exc:
            import traceback
            print(f"ERROR: PROFILE_ERROR ({type(exc).__name__}) — {exc}",
                  file=sys.stderr)
            # Always show traceback so the failing line is visible
            traceback.print_exc(file=sys.stderr)
            return 4

        # Parsed file rows (for current values of a field), this run only.
        for side, definition in (("source", source_def), ("target", target_def)):
            frame = (getattr(profiler, "frames", None) or {}).get(side)
            if frame is not None and definition:
                FILE_FRAMES[_definition_key(definition)] = frame

        # Audit.* and similar bookkeeping fields are never tested.
        ignored = drop_ignored_fields(profile)
        if ignored:
            logger.debug(f"Not tested (bookkeeping fields): {', '.join(ignored)}")

        if args.profile_only:
            print(json.dumps(profile, indent=2, default=str))
            return 0

        user_prompt = build_user_prompt(profile, source_def, target_def, args)

        if args.prompt_only:
            print(json.dumps({
                "model":   ollama_model,
                "system":  SYSTEM_PROMPT,
                "user":    user_prompt,
                "options": {"temperature": args.temperature,
                            "seed": args.seed, "top_k": 1, "top_p": 1.0,
                            "num_ctx": args.num_ctx,
                            "num_predict": args.num_predict, "stop": ["```"]},
            }, indent=2))
            return 0

        # -- Call Ollama (BRD Section 9.1) --------------------------------------

        client = OllamaClient(ollama_api, ollama_model, timeout=args.timeout,
                              num_ctx=args.num_ctx, num_predict=args.num_predict,
                              json_format=not args.no_json_format,
                              seed=args.seed, min_testcases=args.count)

        if not client.health_check():
            print(f"ERROR: AI_UNAVAILABLE — no Ollama at {ollama_api}. "
                  f"You can still create test cases manually.", file=sys.stderr)
            return 5

        log(f"Generating test cases via {ollama_api} ({ollama_model})...",
            args.quiet)
        started = time.time()

        # A fingerprint of the exact prompt. Two runs with the same fingerprint
        # and the same seed must produce the same test cases; if the counts differ
        # while the fingerprints match, the difference came from Ollama, not from
        # the profile. Different fingerprints mean the profiled data changed.
        prompt_hash = hashlib.sha256(
            (SYSTEM_PROMPT + user_prompt).encode("utf-8")
        ).hexdigest()[:12]
        # log(f"Prompt fingerprint: {prompt_hash} "
        #     f"({len(user_prompt):,} chars, seed={args.seed}, "
        #     f"temp={args.temperature}, num_ctx={args.num_ctx})",
        #     args.quiet)

        try:
            raw = client.generate(SYSTEM_PROMPT, user_prompt, args.temperature)
        except Exception as exc:
            print(f"ERROR: AI_ERROR — Ollama call failed: {exc}", file=sys.stderr)
            return 6

        testcases = client.extract_json_array(raw)

        # -- Retry once on parse failure (BRD Section 9.2) ----------------------

        if not testcases:
            log("Response was not valid JSON — retrying once...", args.quiet)
            try:
                raw = client.generate(SYSTEM_PROMPT, user_prompt + RETRY_SUFFIX,
                                      args.temperature)
                testcases = client.extract_json_array(raw)
            except Exception as exc:
                print(f"ERROR: AI_ERROR — retry failed: {exc}", file=sys.stderr)
                return 6

        if not testcases:
            # Save the raw reply — guessing at a truncated console line is not
            # diagnosable, the actual bytes plus Ollama's own counters are.
            try:
                os.makedirs(testcase_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dump = os.path.join(testcase_dir, f"raw_reply_{stamp}.txt")
                with open(dump, "w", encoding="utf-8") as fh:
                    fh.write(f"# Ollama stats: {client.last_stats}\n")
                    fh.write(f"# prompt chars: {len(user_prompt)}\n")
                    fh.write("# ---- raw reply below ----\n")
                    fh.write(raw or "(empty)")
                print(f"Raw reply saved for inspection: {dump}", file=sys.stderr)
                stats = client.last_stats or {}
                print(f"Ollama reported: done_reason={stats.get('done_reason')!r}, "
                      f"response tokens={stats.get('eval_count')}, "
                      f"prompt tokens={stats.get('prompt_eval_count')}, "
                      f"num_ctx={stats.get('num_ctx')}",
                      file=sys.stderr)
                if stats.get("done_reason") == "length":
                    print("The reply hit the token cap — raise --num-ctx.",
                          file=sys.stderr)
                elif stats.get("prompt_eval_count") and stats.get("num_ctx") and \
                        stats["prompt_eval_count"] > stats["num_ctx"] * 0.8:
                    print("The prompt fills most of the context window — raise "
                          "--num-ctx so the reply has room.", file=sys.stderr)
                else:
                    print("The model stopped on its own before finishing the JSON "
                          "— this is typical of a small model. Try a larger one "
                          "(e.g. --model qwen2.5-coder:7b or llama3.1:8b).",
                          file=sys.stderr)
            except OSError:
                pass

            print("ERROR: PARSE_ERROR — AI could not generate valid suggestions. "
                  "Please try again or create test cases manually.",
                  file=sys.stderr)
            return 7

        # -- Report what the model returned before normalisation ----------------
        # Run-to-run count differences are almost always a short or truncated
        # reply, not a change in the data. Log the raw count and the stop reason
        # so a drop is visible rather than silent.
        stats = client.last_stats or {}
        # Printed, not logged: this is the line that explains a run-to-run change
        # in test case count, so it must be visible without --debug.
        #log(f"Model returned {len(testcases)} test case(s)  - "
            # f"done_reason={stats.get('done_reason')!r}, "
            # f"response tokens={stats.get('eval_count')}, "
            # f"prompt tokens={stats.get('prompt_eval_count')}",
            # args.quiet)
        if stats.get("done_reason") == "length":
            logger.warning(
                "The reply was cut off at the token limit — some test cases were "
                "lost. Raise --num-ctx for a complete, repeatable set."
            )

        # A prompt close to the context size is the quiet killer: Ollama drops the
        # oldest tokens to make room, so part of the profile silently disappears
        # and the same command can behave differently between runs.
        prompt_tokens = stats.get("prompt_eval_count") or 0
        if prompt_tokens and prompt_tokens > args.num_ctx * 0.75:
            logger.warning(
                f"The prompt is {prompt_tokens:,} tokens against a context of "
                f"{args.num_ctx:,}. Ollama may be discarding part of it, which "
                f"makes results vary run to run. Raise --num-ctx, or profile "
                f"fewer columns."
            )

        # A very short reply after a very short generation usually means the model
        # gave up rather than ran out of room.
        eval_tokens = stats.get("eval_count") or 0
        if eval_tokens and eval_tokens < 200 and stats.get("done_reason") == "stop":
            logger.warning(
                f"The model stopped after only {eval_tokens} tokens — it ended "
                f"early rather than hitting a limit. A larger model gives a more "
                f"consistent set (e.g. --model qwen2.5-coder:7b)."
            )

        # -- Enforce structure the framework requires ---------------------------
        # Numbering, Viceversa spelling, Source/Target, and SRC_TGT field lists
        # are applied here rather than trusted to the model.
        testcases = normalize_testcases(testcases, profile, source_def, target_def)

        # Module / Stage from the command line always win over the model's copy.
        for case in testcases:
            if (args.module or "").strip():
                case["Module"] = args.module.strip()
            if (args.stage or "").strip():
                case["Stage"] = args.stage.strip()

        # Checks on the target table itself, where SRC_TGT cannot see.
        testcases += build_target_checks(testcases, profile, target_def, args)

        # Expected count (Record_Count) and approved lists (InApprovedValue).
        testcases = collect_approved_values(testcases, profile, source_def, args)

        # MongoDB sides become aggregation pipelines, Oracle sides SELECT queries,
        # each returning only the fields that test case uses.
        key_fields = profile_key_fields(
            profile, _select_join_fields(_build_field_pairs(profile), profile) if target_def else None)
        testcases = build_side_queries(testcases, record_count_fields=key_fields)

    # Array tables: one SRC_TGT each, already in PIPELINE / QUERY form.
    for case in plan["child_cases"]:
        case["Module"] = (args.module or "").strip()
        case["Stage"] = (args.stage or "").strip()
        testcases.append(case)
    # …and a Duplicate_Check on each array table's key.
    testcases += build_side_queries(child_target_duplicates(plan["child_cases"], args))
    # Multi-table files: a Duplicate_Check on each file table's key.
    file_checks = plan.get("file_checks") or []
    for case in file_checks:
        case["Module"] = (args.module or "").strip()
        case["Stage"] = (args.stage or "").strip()
    set_expected_results(file_checks)
    testcases += file_checks
    # Every file: no row repeated in all its fields (--no-row-duplicates to skip).
    row_checks = full_row_duplicate_cases(testcases, source_def, plan.get("target_def") or target_def, args)
    set_expected_results(row_checks)
    testcases += row_checks
    for index, case in enumerate(testcases, start=1):
        case["TestCaseNumber"] = index

    elapsed = time.time() - started

    # -- Save into TestCase_Dir ---------------------------------------------

    # -- Load into DataRegressionSet ----------------------------------------
    # Every complete, valid test case is inserted (or updated / left unchanged
    # if the module already has it). Incomplete or invalid ones are not loaded;
    # the reason is recorded on the test case under "_LoadStatus" so it shows
    # in the JSON file saved below, and in the report printed after it.
    load_results, load_error = None, None
    preview = args.load_preview
    if not args.no_load:
        from Libraries.TestCase_Loader import (upsert_testcases, annotate_testcases,
                                               failed_results)
        try:
            load_results = upsert_testcases(testcases, preview=preview)
        except Exception as exc:
            load_error = str(exc)
            load_results = failed_results(
                testcases, f"DataRegressionSet could not be reached: {exc}")
        annotate_testcases(testcases, load_results, preview=preview)

    try:
        out_path = save_testcases(testcases, testcase_dir, args)
    except OSError as exc:
        print(f"ERROR: WRITE_ERROR — could not write to {testcase_dir}: {exc}",
              file=sys.stderr)
        return 8

    if args.save_prompt:
        prompt_path = out_path.replace(".json", "_prompt.json")
        with open(prompt_path, "w", encoding="utf-8") as fh:
            json.dump({"model": ollama_model, "system": SYSTEM_PROMPT,
                       "user": user_prompt}, fh, indent=2)
        log(f"Prompt saved:     {prompt_path}", args.quiet)

    #log(f"Generated {len(testcases)} test case(s) in {elapsed:.1f}s", args.quiet)
    # log("Types: " + ", ".join(
    #     sorted({tc.get("ValidationType", "?") for tc in testcases})), args.quiet)

    if not args.quiet:
        types = {}
        for case in testcases:
            key = case.get("ValidationType", "?")
            types[key] = types.get(key, 0) + 1
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(types.items()))
        print(f"Generated {len(testcases)} test case(s) — {breakdown}")
        if NOT_CREATED:
            print("Not created:")
            for note in dict.fromkeys(NOT_CREATED):
                print(f"  - {note}")

    print(out_path if args.quiet else f"Saved to: {out_path}")

    # -- Report what happened in DataRegressionSet --------------------------
    if load_results is not None:
        from Libraries.TestCase_Loader import print_report
        if load_error:
            print(f"ERROR: LOAD_ERROR — test cases saved to the file but not loaded "
                  f"into DataRegressionSet: {load_error}", file=sys.stderr)
            print(f"       Load them later with: python ..\\Libraries\\Load_Test_Cases.py "
                  f"\"{out_path}\"", file=sys.stderr)
            return 9
        print_report(load_results, preview=preview, json_path=out_path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")
    sys.exit(main())