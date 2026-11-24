"""
Data_Profiler.py
================
QA Automation Framework — AI Agent
Location: Libraries/

Extracts statistical profiles from data sources (files, Oracle, MongoDB).
The profile output is what gets sent to Ollama for test case generation.

Reference: AI_Agent_Technical_BRD.md
    Section 3   — Data Profiler
    Section 3.2 — Profile output structure
    Section 3.3 — Pattern detection rules
    Section 3.4 — Oracle schema profiling
    Section 7   — Column mapping algorithm

Per BRD Section 3.1: the profiler reads data sources and produces
statistical summaries containing no PHI.
"""

import os
import io
import re
import glob
import json
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section 3.3 — Pattern detection rules
# ---------------------------------------------------------------------------

# No actual values stored — only patterns detected
PATTERN_RULES = [
    # Pattern regex to test        Pattern label returned
    (r"^T\d{10}$",              "T[0-9]{10}"),
    (r"^\d{10}$",               "[0-9]{10}"),       # NPI
    (r"^\d{9}$",                "[0-9]{9}"),        # TIN
    (r"^\d{8}$",                "[0-9]{8}"),        # Date YYYYMMDD
    (r"^\d{4}-\d{2}-\d{2}$",    "YYYY-MM-DD"),
    (r"^\$[\d,]+\.\d{2}$",      r"\$[0-9,]+\.[0-9]{2}"),
    (r"^[YN]$",                 "[YN]"),            # Boolean
    (r"^[01]$",                 "[01]"),            # Boolean
]


def detect_pattern(sample: pd.Series) -> Optional[str]:
    """Test pattern against sample — return label if 90%+ match"""
    for regex, label in PATTERN_RULES:
        if sample.str.match(regex).mean() >= 0.90:
            return label
    return None


# ---------------------------------------------------------------------------
# Section 7.2 — Auto-mapping thresholds
# ---------------------------------------------------------------------------

AUTO_MAP_THRESHOLD    = 0.85  # Auto-include in JoinFields
SUGGEST_MAP_THRESHOLD = 0.60  # Show to user for confirmation
IGNORE_THRESHOLD      = 0.30  # Do not suggest


# ---------------------------------------------------------------------------
# Section 4.1 — Framework types and transforms
# ---------------------------------------------------------------------------

TYPE_STRING   = "STRING"
TYPE_DECIMAL  = "DECIMAL"
TYPE_INTEGER  = "INTEGER"
TYPE_DATE     = "DATE"
TYPE_DATETIME = "DATETIME"
TYPE_BOOLEAN  = "BOOLEAN"

# Column name hints derived from detected pattern only — never from values
PATTERN_NAME_HINTS = {
    "T[0-9]{10}":  "MEMBER_ID",
    "[0-9]{10}":   "NPI",
    "[0-9]{9}":    "TIN",
    "[0-9]{8}":    "ServiceDate",
    "YYYY-MM-DD":  "ServiceDate",
    "[YN]":        "ActiveFlag",
    "[01]":        "ActiveFlag",
}

CURRENCY_RE   = re.compile(r"^\s*\(?\$?-?[\d,]+\.?\d*\)?\s*$")
INTEGER_RE    = re.compile(r"^\s*-?\d+\s*$")
DECIMAL_RE    = re.compile(r"^\s*-?[\d,]*\.\d+\s*$")
DATE8_RE      = re.compile(r"^\d{8}$")
DATE_DASH_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_SLASH_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
BOOL_YN_RE    = re.compile(r"^[YN]$")
BOOL_10_RE    = re.compile(r"^[01]$")
BOOL_TF_RE    = re.compile(r"^(?i:true|false)$")


# ---------------------------------------------------------------------------
# Data Profiler
# ---------------------------------------------------------------------------

class DataProfiler:
    """
    Produces the profile document described in BRD Section 3.2.

    Usage:
        profiler = DataProfiler()
        profile  = profiler.profile(source_def, target_def, learned)
    """

    SAMPLE_SIZE = 1000          # rows sampled for pattern detection
    MONGO_SAMPLE_SIZE = 1000    # documents sampled for MongoDB profiling

    # -- Top level -----------------------------------------------------------

    def profile(self, source_def, target_def: Optional[dict] = None,
                learned: Optional[list] = None) -> dict:
        """
        Builds the full profile document: source, target, learned_mappings.
        """
        learned = learned or []

        source_profile = self.profile_source(source_def)
        target_profile = self.profile_target(target_def) if target_def else {}

        result = {
            "source": source_profile,
            "target": target_profile,
        }

        if source_profile.get("columns") and target_profile.get("columns"):
            result["learned_mappings"] = self.map_columns(
                source_profile["columns"],
                target_profile["columns"],
                learned
            )

        return result

    def profile_source(self, source_def) -> dict:
        """Dispatches on source Type. Pipeline sources use step 1."""
        if isinstance(source_def, list):
            # Pipeline source — profile the first step definition
            source_def = source_def[0] if source_def else {}

        stype = (source_def.get("Type") or "").upper()

        if stype in ("ORACLE",):
            return self.profile_oracle(source_def)
        if stype in ("MONGODB",):
            return self.profile_mongodb(source_def)
        return self.profile_file(source_def)

    def profile_target(self, target_def: Optional[dict]) -> dict:
        """Dispatches on target Type."""
        if not target_def:
            return {}

        ttype = (target_def.get("Type") or "").upper()

        if ttype == "ORACLE":
            return self.profile_oracle(target_def)
        if ttype == "MONGODB":
            return self.profile_mongodb(target_def)
        return self.profile_file(target_def)

    # -- File profiling ------------------------------------------------------

    def profile_file(self, source_def: dict) -> dict:
        """
        Resolves the file from the source definition, reads it, and profiles.

        Raises:
            FileNotFoundError — no file matches the pattern
            PermissionError   — file cannot be read
        """
        path         = source_def.get("Path") or ""
        file_name    = source_def.get("FileName")
        file_pattern = source_def.get("FilePattern")
        pick         = (source_def.get("PickStrategy") or "LATEST").upper()
        file_config  = source_def.get("FileConfig") or {}

        resolved = self._resolve_file(path, file_name, file_pattern, pick)

        with open(resolved, "rb") as fh:
            content = fh.read()

        profile = self.profile_content(content, file_config)
        profile["type"] = (source_def.get("Type") or "LOCAL_PATH").upper()
        profile["file"] = file_pattern or file_name or os.path.basename(resolved)
        return profile

    def _resolve_file(self, path: str, file_name: Optional[str],
                      file_pattern: Optional[str], pick: str) -> str:
        """Resolves a concrete file path from name or pattern."""
        if file_name:
            candidate = os.path.join(path, file_name) if path else file_name
            if not os.path.exists(candidate):
                raise FileNotFoundError(candidate)
            return candidate

        if not file_pattern:
            raise FileNotFoundError(path)

        matches = glob.glob(os.path.join(path, file_pattern))
        if not matches:
            raise FileNotFoundError(os.path.join(path, file_pattern))

        if pick == "LATEST":
            return max(matches, key=os.path.getmtime)
        return sorted(matches)[0]

    def profile_content(self, content: bytes, file_config: dict) -> dict:
        """
        Profiles raw file bytes against a FileConfig.
        Returns the source profile structure from BRD Section 3.2.
        """
        fmt       = (file_config.get("Format") or "CSV").upper()
        delimiter = file_config.get("Delimiter")
        header    = bool(file_config.get("Header", False))

        # PDF is binary and cannot be profiled as a DataFrame.
        # Return a minimal profile — column structure comes from FileConfig.Columns.
        if fmt == "PDF":
            logger.warning(
                "Data_Profiler: PDF format skipped for tabular profiling. "
                "Column structure comes from FileConfig.Columns in the test case."
            )
            return {
                "type":      "LOCAL_PATH",
                "row_count": 0,
                "format":    "PDF",
                "delimiter": None,
                "header":    False,
                "columns":   [],
            }

        df = self._read_dataframe(content, file_config)

        # Everything is profiled as text — no value coercion, no value retained
        df = df.astype(str)

        columns = []
        for position, col in enumerate(df.columns):
            columns.append(
                self._profile_column(df[col], position, col if header else None)
            )

        return {
            "type":      "LOCAL_PATH",
            "row_count": int(len(df)),
            "format":    fmt,
            "delimiter": delimiter,
            "header":    header,
            "columns":   columns,
        }

    def _read_dataframe(self, content: bytes, file_config: dict) -> pd.DataFrame:
        """Reads file bytes into a DataFrame according to FileConfig."""
        fmt         = (file_config.get("Format") or "CSV").upper()
        delimiter   = file_config.get("Delimiter")
        header      = 0 if file_config.get("Header", False) else None
        skip_rows   = int(file_config.get("SkipRows") or 0)
        footer_rows = int(file_config.get("FooterRows") or 0)
        encoding    = file_config.get("Encoding") or "utf-8-sig"
        comment     = file_config.get("CommentChar")
        null_values = file_config.get("NullValues") or [
            "NULL", "null", "N/A", "na", "", "-", "none"
        ]

        # ── Strip UTF-8 BOM before pandas sees the content ────────────────
        # A BOM followed by a quote char makes pandas' python-engine
        # _check_for_bom() raise "ValueError: substring not found" while
        # hunting for the closing quote. Stripping it here avoids that
        # entirely and is harmless for files without a BOM.
        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]
            logger.info("Data_Profiler: stripped UTF-8 BOM from file content.")

        buffer = io.BytesIO(content)

        if fmt == "PDF":
            logger.warning(
                "Data_Profiler: PDF format cannot be profiled as a DataFrame — "
                "returning empty profile. Column definitions come from "
                "FileConfig.Columns in the test case."
            )
            return pd.DataFrame()

        if fmt == "EXCEL":
            df = pd.read_excel(buffer, header=header, skiprows=skip_rows,
                               na_values=null_values, dtype=str)

        elif fmt == "JSON":
            df = pd.json_normalize(json.loads(content.decode(encoding)))

        elif fmt == "XML":
            df = pd.read_xml(io.BytesIO(content))

        elif fmt == "FIXED_WIDTH":
            widths = [c.get("Width") for c in file_config.get("Columns", [])]
            if not widths or None in widths:
                raise ValueError("FIXED_WIDTH requires Width on every column")
            df = pd.read_fwf(buffer, widths=widths, header=header,
                             skiprows=skip_rows, na_values=null_values,
                             dtype=str, encoding=encoding)

        else:
            # CSV, PIPE, TSV and other delimited formats
            sep = delimiter or {"CSV": ",", "TSV": "\t", "PIPE": "|"}.get(fmt, ",")
            df = pd.read_csv(buffer, sep=sep, header=header,
                             skiprows=skip_rows, na_values=null_values,
                             dtype=str, encoding=encoding, comment=comment,
                             engine="python", keep_default_na=True)

        if footer_rows:
            df = df.iloc[:-footer_rows]

        return df

    def _profile_column(self, series: pd.Series, position: int,
                        header_name: Optional[str]) -> dict:
        """
        Profiles a single column into the structure of BRD Section 3.2.
        Only counts, patterns, and min/max are emitted — never row values.
        """
        row_count = int(len(series))

        clean = series.replace(
            {"nan": None, "NaN": None, "None": None, "": None}
        ).dropna()
        clean = clean.astype(str).str.strip()
        clean = clean[clean != ""]

        null_count     = row_count - int(len(clean))
        distinct_count = int(clean.nunique())

        sample  = clean.head(self.SAMPLE_SIZE)
        pattern = detect_pattern(sample) if len(sample) else None

        col = {
            "position":       position,
            "null_count":     null_count,
            "distinct_count": distinct_count,
            "row_count":      row_count,
        }

        has_currency = bool(
            len(sample) and sample.str.contains(r"[$,]", regex=True).mean() >= 0.90
        )
        if has_currency:
            col["has_currency_symbol"] = True

        suggested_type = self._detect_type(sample, pattern)

        if suggested_type in (TYPE_DECIMAL, TYPE_INTEGER):
            numeric = pd.to_numeric(
                clean.str.replace(r"[$,]", "", regex=True)
                     .str.replace(r"^\((.*)\)$", r"-\1", regex=True),
                errors="coerce"
            ).dropna()
            if len(numeric):
                col["min"] = float(numeric.min()) if suggested_type == TYPE_DECIMAL \
                    else int(numeric.min())
                col["max"] = float(numeric.max()) if suggested_type == TYPE_DECIMAL \
                    else int(numeric.max())

        if suggested_type == TYPE_DATE and len(clean):
            col["date_range_min"] = str(clean.min())
            col["date_range_max"] = str(clean.max())

        if pattern:
            col["sample_pattern"] = pattern

        col["suggested_name"] = self._suggest_name(
            header_name, pattern, suggested_type, has_currency, distinct_count
        )
        col["suggested_type"] = suggested_type

        transform = self._suggest_transform(
            suggested_type, pattern, has_currency, distinct_count, sample
        )
        if transform:
            col["requires_transform"] = transform

        return col

    def _detect_type(self, sample: pd.Series, pattern: Optional[str]) -> str:
        """Infers the framework type from the sampled values."""
        if not len(sample):
            return TYPE_STRING

        def ratio(regex) -> float:
            return sample.str.match(regex).mean()

        if ratio(BOOL_YN_RE) >= 0.90 or ratio(BOOL_TF_RE) >= 0.90:
            return TYPE_BOOLEAN

        if pattern in ("[0-9]{8}", "YYYY-MM-DD") or ratio(DATE_SLASH_RE) >= 0.90:
            return TYPE_DATE

        if pattern in ("T[0-9]{10}", "[0-9]{10}", "[0-9]{9}"):
            # Identifier patterns are strings even though they are all digits
            return TYPE_STRING

        if ratio(INTEGER_RE) >= 0.90:
            return TYPE_INTEGER

        if ratio(DECIMAL_RE) >= 0.90 or ratio(CURRENCY_RE) >= 0.90:
            return TYPE_DECIMAL

        if ratio(BOOL_10_RE) >= 0.90:
            return TYPE_BOOLEAN

        return TYPE_STRING

    def _suggest_name(self, header_name: Optional[str], pattern: Optional[str],
                      suggested_type: str, has_currency: bool,
                      distinct_count: int) -> Optional[str]:
        """
        Suggests a column name. Header name wins when present, otherwise the
        name is inferred from the detected pattern and type — never from values.
        """
        if header_name:
            return str(header_name).strip()

        if pattern and pattern in PATTERN_NAME_HINTS:
            return PATTERN_NAME_HINTS[pattern]

        if has_currency or suggested_type == TYPE_DECIMAL:
            return "PaidAmt"
        if suggested_type == TYPE_DATE:
            return "ServiceDate"
        if suggested_type == TYPE_BOOLEAN:
            return "ActiveFlag"
        if suggested_type == TYPE_INTEGER:
            return "EventID" if distinct_count == 1 else None
        if suggested_type == TYPE_STRING and distinct_count <= 5:
            return "StatusCode"

        return None

    def _suggest_transform(self, suggested_type: str, pattern: Optional[str],
                           has_currency: bool, distinct_count: int,
                           sample: pd.Series) -> Optional[str]:
        """Selects the transform per BRD Section 4.1 generation rules."""
        if suggested_type == TYPE_BOOLEAN:
            if len(sample) and sample.str.match(BOOL_10_RE).mean() >= 0.90:
                return "BOOLEAN_10"
            if len(sample) and sample.str.match(BOOL_TF_RE).mean() >= 0.90:
                return "BOOLEAN_TF"
            return "BOOLEAN_YN"

        if suggested_type == TYPE_DATE:
            if pattern == "[0-9]{8}":
                return "YYYYMMDD"
            if pattern == "YYYY-MM-DD":
                return "AUTO_DATE"
            if len(sample) and sample.str.match(DATE_SLASH_RE).mean() >= 0.90:
                return "MM/DD/YYYY"
            return "AUTO_DATE"

        if suggested_type == TYPE_DECIMAL and has_currency:
            return "STRIP_CURRENCY"

        if suggested_type == TYPE_STRING:
            if pattern:
                return "TRIM"
            if distinct_count <= 5:
                return "UPPERCASE"
            return "TRIM"

        return None

    # -- Section 3.4 — Oracle schema profiling, no data read -----------------

    def profile_oracle(self, target_def: dict) -> dict:
        """Wraps profile_oracle_schema into the profile structure."""
        environment = target_def.get("Environment")
        table       = target_def.get("Query") or target_def.get("Table") or ""

        schema = profile_oracle_schema(environment, table)

        columns = []
        for row in schema.get("columns", []):
            columns.append({
                "name":           row.get("COLUMN_NAME") or row.get("column_name"),
                "type":           row.get("DATA_TYPE") or row.get("data_type"),
                "null_count":     row.get("NUM_NULLS") or row.get("num_nulls") or 0,
                "distinct_count": row.get("NUM_DISTINCT") or row.get("num_distinct"),
            })

        return {
            "type":      "ORACLE",
            "table":     table,
            "row_count": self._oracle_row_count(environment, table),
            "columns":   columns,
        }

    def _oracle_row_count(self, environment: str, table: str) -> Optional[int]:
        """Reads NUM_ROWS from statistics — no SELECT on the data table."""
        try:
            conn = OracleConnection(environment)
            parts = table.split(".")
            owner = parts[0] if len(parts) > 1 else "FIN"
            tname = parts[1] if len(parts) > 1 else parts[0]
            rows = conn.fetchAllDict(
                f"SELECT num_rows FROM all_tables "
                f"WHERE owner = '{owner.upper()}' "
                f"AND table_name = '{tname.upper()}'"
            )
            if rows:
                value = rows[0].get("NUM_ROWS") or rows[0].get("num_rows")
                return int(value) if value is not None else None
        except Exception as exc:
            logger.warning(f"Could not read Oracle row count: {exc}")
        return None

    # -- MongoDB profiling ---------------------------------------------------

    def profile_mongodb(self, target_def: dict) -> dict:
        """
        Profiles a MongoDB collection from a sample of documents.
        Field names, counts and types only — no document values retained.
        """
        environment = target_def.get("Environment")
        db_name     = target_def.get("DB")
        collection  = target_def.get("Query")

        # Collection(db, collection, Environment) — framework signature
        coll = Collection(db_name, collection, environment)

        row_count = coll.getDataCount({})
        sample    = list(coll.getData({}))[:self.MONGO_SAMPLE_SIZE]

        df = pd.json_normalize(sample) if sample else pd.DataFrame()
        if "_id" in df.columns:
            df = df.drop(columns=["_id"])

        columns = []
        for field in df.columns:
            series = df[field]
            values = series.dropna()
            columns.append({
                "name":           field,
                "type":           self._mongo_type(values),
                "null_count":     int(len(series) - len(values)),
                "distinct_count": int(values.astype(str).nunique()),
            })

        return {
            "type":       "MONGODB",
            "collection": collection,
            "row_count":  int(row_count),
            "sampled":    int(len(sample)),
            "columns":    columns,
        }

    def _mongo_type(self, values: pd.Series) -> str:
        """Maps sampled BSON values to framework types."""
        if not len(values):
            return TYPE_STRING
        first = values.iloc[0]
        if isinstance(first, bool):
            return TYPE_BOOLEAN
        if isinstance(first, int):
            return TYPE_INTEGER
        if isinstance(first, float):
            return TYPE_DECIMAL
        if hasattr(first, "isoformat"):
            return TYPE_DATETIME
        return TYPE_STRING

    # -- Section 7 — Column mapping -----------------------------------------

    def map_columns(self, source_columns: list, target_columns: list,
                    learned: Optional[list] = None) -> list:
        """
        Scores every source column against every target column and returns
        the best match per source column above IGNORE_THRESHOLD.
        """
        learned = learned or []
        mappings = []

        # File profiles emit 'suggested_name'; Oracle/MongoDB emit 'name'.
        # score_mapping reads 'name', so normalize before scoring.
        normalized = []
        for tgt in target_columns:
            name = tgt.get("name") or tgt.get("suggested_name")
            if not name:
                continue
            merged = dict(tgt)
            merged["name"] = name
            if "type" not in merged and "suggested_type" in merged:
                merged["type"] = merged["suggested_type"]
            normalized.append(merged)

        for src in source_columns:
            best, best_score = None, 0.0
            for tgt in normalized:
                score = score_mapping(src, tgt, learned)
                if score > best_score:
                    best, best_score = tgt, score

            if best is None or best_score < IGNORE_THRESHOLD:
                continue

            mappings.append({
                "source_col": src.get("position"),
                "target_col": best.get("name"),
                "confidence": best_score,
                "reason":     self._mapping_reason(src, best, best_score),
                "auto_map":   best_score >= AUTO_MAP_THRESHOLD,
                "confirm":    SUGGEST_MAP_THRESHOLD <= best_score < AUTO_MAP_THRESHOLD,
            })

        return mappings

    def _mapping_reason(self, src: dict, tgt: dict, score: float) -> str:
        """Human readable justification for a mapping score."""
        reasons = []
        src_name = (src.get("suggested_name") or "").lower()
        tgt_name = (tgt.get("name") or "").lower()

        if src_name and src_name == tgt_name:
            reasons.append("exact name match")
        elif src_name.replace("_", "") == tgt_name.replace("_", ""):
            reasons.append("name match")
        elif src_name and tgt_name and (
            src_name.replace("_", "") in tgt_name.replace("_", "")
            or tgt_name.replace("_", "") in src_name.replace("_", "")
        ):
            reasons.append("name similarity")

        if src.get("sample_pattern") and \
                src.get("sample_pattern") == tgt.get("pattern"):
            reasons.append("pattern match")

        if src.get("min") is not None and tgt.get("min") is not None:
            reasons.append("value range match")

        if src.get("suggested_type") == _map_oracle_type(tgt.get("type", "")):
            reasons.append("type match")

        return " + ".join(reasons) if reasons else "low confidence match"


# ---------------------------------------------------------------------------
# Section 3.4 — Oracle schema profiling (module level, per BRD)
# ---------------------------------------------------------------------------

def profile_oracle_schema(environment: str, table: str) -> dict:
    """
    Reads Oracle schema only — no data values.
    Uses ALL_TAB_COLUMNS — no SELECT on data table.
    """
    try:
        conn = OracleConnection(environment)
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Oracle environment '{environment}': {e}. "
            f"Check that the environment key exists in Config/environment.ini "
            f"and that oracle_connection is set correctly."
        ) from e

    parts  = table.split(".")
    owner  = (parts[0] if len(parts) > 1 else "FIN").upper()
    tname  = (parts[1] if len(parts) > 1 else parts[0]).upper()

    # fetchAllDict accepts only the SQL string — values embedded directly.
    # Safe: owner and tname come from config/CLI args, not end-user input.
    schema_query = f"""
        SELECT
            column_name,
            data_type,
            data_length,
            nullable,
            num_distinct,
            num_nulls,
            low_value,
            high_value
        FROM all_tab_columns
        WHERE owner = '{owner}'
        AND table_name = '{tname}'
        ORDER BY column_id
    """
    rows = conn.fetchAllDict(schema_query)
    # low_value/high_value are RAW encoded — decode safely
    # They represent min/max without exposing actual data rows
    return {"columns": rows, "source": "ALL_TAB_COLUMNS"}


# ---------------------------------------------------------------------------
# Section 7.1 — Mapping confidence scoring
# ---------------------------------------------------------------------------

def score_mapping(source_col: dict, target_col: dict,
                  learned: list) -> float:
    """
    Scores how likely two columns are the same field.
    Returns 0.0 to 1.0 confidence score.
    """
    score = 0.0

    src_name = (source_col.get("suggested_name") or "").lower()
    tgt_name = (target_col.get("name") or "").lower()

    # Exact name match
    if src_name == tgt_name:
        score = max(score, 1.0)

    # Root word match (remove underscores, case-insensitive)
    src_root = src_name.replace("_", "")
    tgt_root = tgt_name.replace("_", "")
    if src_root == tgt_root:
        score = max(score, 0.95)

    # Partial name match
    if src_root in tgt_root or tgt_root in src_root:
        score = max(score, 0.85)

    # Pattern match
    if (source_col.get("sample_pattern") and
            source_col["sample_pattern"] == target_col.get("pattern")):
        score = max(score, 0.80)

    # Type match
    src_type = source_col.get("suggested_type", "")
    tgt_type = _map_oracle_type(target_col.get("type", ""))
    if src_type == tgt_type:
        score += 0.05

    # Value range match (for numeric columns)
    if (source_col.get("min") is not None and
            target_col.get("min") is not None):
        if (abs(source_col["min"] - target_col["min"]) < 0.01 and
                abs(source_col["max"] - target_col["max"]) < 0.01):
            score = max(score, 0.90)

    # Learned mapping bonus
    for mapping in learned:
        if (mapping.get("source_pattern") == source_col.get("sample_pattern")
                and mapping.get("target_column") == target_col.get("name")):
            score = min(1.0, score + 0.15 * mapping.get("confidence", 1.0))

    return round(min(score, 1.0), 2)


def _map_oracle_type(oracle_type: str) -> str:
    """Maps Oracle data types to framework types."""
    mapping = {
        "VARCHAR2": "STRING", "CHAR": "STRING", "CLOB": "STRING",
        "NUMBER":   "DECIMAL", "FLOAT": "DECIMAL",
        "INTEGER":  "INTEGER", "INT": "INTEGER",
        "DATE":     "DATE", "TIMESTAMP": "DATETIME"
    }
    return mapping.get((oracle_type or "").upper(), "STRING")


# ---------------------------------------------------------------------------
# Existing framework connections
# ---------------------------------------------------------------------------
# The profiler reuses the framework's existing connection classes.
# Imported at the bottom so the pure profiling functions above remain
# importable and unit testable without a database.

try:
    from Libraries.OracleDB import OracleConnection   # noqa: E402
    from Libraries.MongoDB import Collection     # noqa: E402
except ImportError:  # pragma: no cover
    logger.warning(
        "Framework connection classes not importable — "
        "file profiling available, database profiling disabled"
    )

    class OracleConnection:  # type: ignore
        def __init__(self, environment):
            raise RuntimeError("OracleConnection not available")

    class Collection:  # type: ignore
        def __init__(self, environment):
            raise RuntimeError("MongoConnection not available")
