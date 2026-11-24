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
    6. Saves the test case file into the folder configured as TestCase_Dir

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
Only the source and target definitions are mandatory. --module and --stage
are optional labels; when omitted they are simply left out of the prompt and
of the generated file name.

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
    "HL7", "NCPDP_D0", "NCPDP_SCRIPT", "EDI835",
]

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
        "required": ["Target", "ApprovedValues"],
        "allowed":  ["JoinFields", "CompareFields", "ExpectedResult"],
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
TWO_SIDED_TYPES = {"SRC_TGT", "RECORD_COUNT"}

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
- Record_Count: Compare row counts between source and target
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
- null_count > 0 → generate Null_Check
- distinct_count == row_count → candidate for Duplicate_Check key
- pattern [0-9]{10} → generate Format_Check regex ^\\d{10}$
- source row_count != target row_count → generate Record_Count
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
  ValidationType is SRC_TGT, Record_Count, or an EDI835_* type.
  Null_Check, Duplicate_Check, Format_Check and InApprovedValue
  examine the source alone: omit "Target" entirely for them,
  do not emit it as null or {}
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
    if fmt == "FIXED_WIDTH" and not getattr(args, f"{side}_columns"):
        raise SystemExit(
            f"ERROR: FIXED_WIDTH {side} requires {prefix}-columns "
            f"(JSON file with Name/Position/Width/Type per column)"
        )


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
    choices = ALL_TYPES + ([TEST_CONNECTION_TYPE] if side == "source" else [])

    type_name = ask(f"{side} type", choices=choices).upper()
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
    context += (
        f"\nGENERATE AT LEAST {wanted} TEST CASES. Cover the validation "
        f"types that fit the profile rather than repeating one type:\n"
        f"  - one SRC_TGT comparing the two sides (first in the array)\n"
        f"  - Record_Count when both sides have rows\n"
        f"  - one Null_Check per column with nulls\n"
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
        target_type   = (args.target_type or "").upper(),
        target_format = f" (format: {target_fmt})" if target_fmt else "",
        source_def    = json.dumps(source_def, indent=2),
        target_def    = json.dumps(target_def, indent=2),
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

    logger.warning(
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
        # Columns that actually contain nulls; if none do, check the keys.
        for col in columns:
            name = name_of(col)
            if name and (col.get("null_count") or 0) > 0:
                fields.append({"SourceField": name,
                               "ResultFields": [name]})
        if not fields:
            for name in key_names:
                fields.append({"SourceField": name,
                               "ResultFields": [name]})

    elif validation_type == "DUPLICATE_CHECK":
        # Duplicates are checked on the key columns.
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
        case["CheckFields"] = existing or _build_check_fields(
            validation_type, profile, join_pairs
        )
        if not case["CheckFields"]:
            logger.warning(
                f"TestCase {index} ({case.get('ValidationType')}) needs "
                f"CheckFields but none could be derived from the profile — "
                f"fill them in before running."
            )
    elif "CheckFields" in case:
        case["CheckFields"] = _normalize_check_fields(case.get("CheckFields"),
                                                      validation_type)

    if "ApprovedValues" in required and not case.get("ApprovedValues"):
        # Record_Count reads ApprovedValues[0] as the tolerated difference and
        # raises without it; 0 means the counts must match exactly.
        case["ApprovedValues"] = ["0"] if validation_type == "RECORD_COUNT" else []
        if validation_type != "RECORD_COUNT":
            logger.warning(
                f"TestCase {index} ({case.get('ValidationType')}) needs "
                f"ApprovedValues — add the approved list before running."
            )

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

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="Test_Case_Generator.py",
        description="Profile a source and target, generate test cases via "
                    "Ollama, and save them as JSON into TestCase_Dir.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument("--config", help="Path to config INI "
                                    "(default: agent_config.ini)")
    p.add_argument("--interactive", action="store_true",
                   help="Prompt for each value instead of using flags")
    p.add_argument("--module", default="",
                   help="Optional module label for the generated test cases")
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
                            "(required for FIXED_WIDTH)")

    return p


def log(message: str, quiet: bool) -> None:
    if not quiet:
        print(message)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

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

    validate_side("source", args)
    validate_side("target", args)

    source_def = build_definition("source", args)
    target_def = build_definition("target", args)

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

    elapsed = time.time() - started

    # -- Save into TestCase_Dir ---------------------------------------------

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

    print(out_path if args.quiet else f"Saved to: {out_path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")
    sys.exit(main())
