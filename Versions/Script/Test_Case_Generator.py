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
    "HL7", "NCPDP_D0", "NCPDP_SCRIPT",
]

ALL_TYPES = DB_TYPES + FILE_RETRIEVAL_TYPES

DEFAULT_DELIMITERS = {"CSV": ",", "TSV": "\t", "PIPE": "|"}


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
- Each test case must have:
  TestCaseNumber (null — assigned on save),
  Module, Stage, ValidationType, TestDescription,
  Viceversa (true for SRC_TGT), Source, DataFilters
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

    def __init__(self, host: str, model: str, timeout: int = 120):
        self.host    = host.rstrip("/")
        self.model   = model
        self.timeout = timeout
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
                 temperature: float = 0.1) -> str:
        """
        Calls Ollama with retry on timeout.
        Low temperature for consistent JSON output.
        """
        options = {
            "temperature": temperature,
            "num_predict": 4096,
            "stop": ["```"],
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]

        last_error = None
        for attempt in range(3):
            try:
                if self._client:
                    response = self._client.chat(
                        model=self.model, messages=messages, options=options
                    )
                else:
                    response = self._post("/api/chat", {
                        "model":    self.model,
                        "messages": messages,
                        "options":  options,
                        "stream":   False,
                    })
                return response["message"]["content"]
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    raise
                logger.warning(f"Ollama call failed (attempt {attempt + 1}) — "
                               f"retrying: {exc}")
                time.sleep(2)

        raise RuntimeError(f"Ollama call failed: {last_error}")

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def extract_json_array(text: str) -> list:
        """
        Safely extracts JSON array from model output.
        Handles cases where model adds prose before/after JSON.
        """
        try:
            start = text.index("[")
            end   = text.rindex("]") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
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
    p.add_argument("--temperature", type=float, default=0.1)
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
                        "num_predict": 4096, "stop": ["```"]},
        }, indent=2))
        return 0

    # -- Call Ollama (BRD Section 9.1) --------------------------------------

    client = OllamaClient(ollama_api, ollama_model, timeout=args.timeout)

    if not client.health_check():
        print(f"ERROR: AI_UNAVAILABLE — no Ollama at {ollama_api}. "
              f"You can still create test cases manually.", file=sys.stderr)
        return 5

    log(f"Generating test cases via {ollama_api} ({ollama_model})...",
        args.quiet)
    started = time.time()

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
        print("ERROR: PARSE_ERROR — AI could not generate valid suggestions. "
              "Please try again or create test cases manually.",
              file=sys.stderr)
        return 7

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

    log(f"Generated {len(testcases)} test case(s) in {elapsed:.1f}s", args.quiet)
    log("Types: " + ", ".join(
        sorted({tc.get("ValidationType", "?") for tc in testcases})), args.quiet)

    print(out_path if args.quiet else f"Saved to: {out_path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")
    sys.exit(main())
