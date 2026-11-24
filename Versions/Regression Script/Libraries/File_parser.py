import io
import os
import re
import logging
import chardet
import logging as _logging
import pandas as pd
# chardet logs every encoding probe at DEBUG — suppress to WARNING
_logging.getLogger("chardet").setLevel(_logging.WARNING)
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timezone
logger = logging.getLogger()
 
# Used by fetch_dynamic_pipeline() for lookup-step placeholder substitution
from .parameter_reading import _to_oracle_value, _to_mongo_value
import json as _json

# ─────────────────────────────────────────────────────────────────────────────
# Encoding detection
# ─────────────────────────────────────────────────────────────────────────────
def detect_encoding(content: bytes, specified: str = None) -> str:
    """
    Detects file encoding.
    If specified — use that directly.
    Otherwise auto-detect via chardet with confidence threshold.
    """
    if specified:
        return specified
    result     = chardet.detect(content[:65536])
    encoding   = result.get("encoding") or "utf-8"
    confidence = result.get("confidence") or 0
    if confidence < 0.90:
        logger.warning(
            f"File_parser: Encoding detection confidence low "
            f"({confidence:.0%}) — detected '{encoding}', "
            f"defaulting to utf-8."
        )
        return "utf-8"
    logger.info(
        f"File_parser: Encoding detected — '{encoding}' "
        f"(confidence {confidence:.0%})"
    )
    return encoding

# ─────────────────────────────────────────────────────────────────────────────
# Transform engine
# ─────────────────────────────────────────────────────────────────────────────
# Date format patterns
DATE_FORMATS = {
    "YYYYMMDD"     : "%Y%m%d",
    "MMDDYYYY"     : "%m%d%Y",
    "MM/DD/YYYY"   : "%m/%d/%Y",
    "DD/MM/YYYY"   : "%d/%m/%Y",
    "YYYY-MM-DD"   : "%Y-%m-%d",
    "YYYY/MM/DD"   : "%Y/%m/%d",
    "MM-DD-YYYY"   : "%m-%d-%Y",
    "DD-MON-YYYY"  : "%d-%b-%Y",
    "DD-MON-YY"    : "%d-%b-%y",
    "MM/DD/YY"     : "%m/%d/%y",
}
DATETIME_FORMATS = {
    "YYYY-MM-DD HH:MM:SS"  : "%Y-%m-%d %H:%M:%S",
    "YYYY-MM-DDTHH:MM:SS"  : "%Y-%m-%dT%H:%M:%S",
    "MM/DD/YYYY HH:MM:SS"  : "%m/%d/%Y %H:%M:%S",
    "YYYYMMDDHHMMSS"       : "%Y%m%d%H%M%S",
}
ALL_DATE_FORMATS = list(DATE_FORMATS.values()) + list(DATETIME_FORMATS.values())

def _parse_date(value: str, fmt: str = None) -> datetime:
    """
    Parses a date string using specified or auto-detected format.
    Handles two-digit years with pivot year 1950.
    """
    value = (value or "").strip()
    if not value:
        return None
    formats_to_try = []
    if fmt:
        # Map friendly name to strptime format
        strptime_fmt = (
            DATE_FORMATS.get(fmt) or
            DATETIME_FORMATS.get(fmt) or
            fmt  # assume it's already a strptime format
        )
        formats_to_try = [strptime_fmt]
    else:
        formats_to_try = ALL_DATE_FORMATS
    for strptime_fmt in formats_to_try:
        try:
            dt = datetime.strptime(value, strptime_fmt)
            # Handle two-digit year pivot
            if dt.year < 100:
                dt = dt.replace(year=dt.year + (2000 if dt.year < 50 else 1900))
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date '{value}' — no matching format found.")

def _apply_single_transform(value, transform: str, row: dict = None):
    """
    Applies a single named transform to a value.
    row is passed for conditional transforms that reference other columns.
    """
    if value is None:
        return None
    t = (transform or "").upper().strip()
    # String transforms
    if t == "TRIM":
        return str(value).strip()
    if t == "UPPERCASE":
        return str(value).upper()
    if t == "LOWERCASE":
        return str(value).lower()
    if t == "TRIM_LEADING_ZEROS":
        return str(value).lstrip("0") or "0"
    if t.startswith("PAD_LEADING_ZEROS:"):
        n = int(t.split(":")[1])
        return str(value).zfill(n)
    # Numeric transforms
    if t == "STRIP_CURRENCY":
        cleaned = re.sub(r"[$£€¥,]", "", str(value)).strip()
        return cleaned
    if t == "STRIP_COMMAS":
        return str(value).replace(",", "")
    if t == "PARENTHESES_NEGATIVE":
        s = str(value).strip()
        if s.startswith("(") and s.endswith(")"):
            return "-" + s[1:-1]
        return s
    if t == "EUROPEAN_DECIMAL":
        # 1.234,56 → 1234.56
        s = str(value).strip()
        s = s.replace(".", "").replace(",", ".")
        return s
    if t == "SCIENTIFIC_NOTATION":
        try:
            return str(float(str(value)))
        except Exception:
            return value
    if t.startswith("IMPLIED_DECIMAL:"):
        places = int(t.split(":")[1])
        try:
            num = int(str(value).strip())
            return str(Decimal(num) / Decimal(10 ** places))
        except Exception:
            return value
    if t == "DECIMAL":
        try:
            return float(str(value).strip())
        except Exception:
            return value
    if t == "INTEGER":
        try:
            return int(str(value).strip())
        except Exception:
            return value
    if t == "ZERO_TO_NULL":
        s = str(value).strip()
        return None if s in ("0", "0.0", "0.00") else value
    # Boolean transforms
    TRUE_VALUES  = {"y", "yes", "1", "t", "true", "x"}
    FALSE_VALUES = {"n", "no",  "0", "f", "false", " ", ""}
    if t in ("BOOLEAN_YN", "BOOLEAN_YESNO", "BOOLEAN_10",
             "BOOLEAN_TF", "BOOLEAN_TRUEFALSE", "BOOLEAN_X",
             "BOOLEAN_AUTO"):
        s = str(value).strip().lower()
        if s in TRUE_VALUES:
            return True
        if s in FALSE_VALUES:
            return False
        logger.warning(
            f"File_parser: Boolean transform '{t}' — "
            f"unrecognised value '{value}' — kept as-is."
        )
        return value
    # Date transforms
    if t in DATE_FORMATS or t in DATETIME_FORMATS:
        try:
            return _parse_date(str(value), t)
        except Exception:
            return value
    if t == "AUTO_DATE":
        try:
            return _parse_date(str(value), None)
        except Exception:
            return value
    logger.warning(f"File_parser: Unknown transform '{transform}' — skipped.")
    return value

def _evaluate_condition(row: dict, condition: dict) -> bool:
    """
    Evaluates a condition against a row dict.
    Returns True if condition is met.
    """
    col      = condition.get("Column", "")
    operator = (condition.get("Operator") or "=").upper()
    value    = condition.get("Value")
    row_value = row.get(col)
    if row_value is None:
        row_value = ""
    row_str = str(row_value).strip()
    val_str = str(value).strip() if value is not None else ""
    if operator == "=":
        return row_str == val_str
    if operator == "!=":
        return row_str != val_str
    if operator == ">":
        try:
            return float(row_str) > float(val_str)
        except Exception:
            return row_str > val_str
    if operator == ">=":
        try:
            return float(row_str) >= float(val_str)
        except Exception:
            return row_str >= val_str
    if operator == "<":
        try:
            return float(row_str) < float(val_str)
        except Exception:
            return row_str < val_str
    if operator == "<=":
        try:
            return float(row_str) <= float(val_str)
        except Exception:
            return row_str <= val_str
    if operator == "IN":
        values = [v.strip() for v in val_str.split(",")]
        return row_str in values
    if operator == "NOT IN":
        values = [v.strip() for v in val_str.split(",")]
        return row_str not in values
    if operator == "IS NULL":
        return not row_str
    if operator == "IS NOT NULL":
        return bool(row_str)
    if operator == "STARTS_WITH":
        return row_str.startswith(val_str)
    if operator == "ENDS_WITH":
        return row_str.endswith(val_str)
    if operator == "CONTAINS":
        return val_str in row_str
    return False

def apply_transforms(value, transforms, row: dict = None):
    """
    Applies transforms to a value.
    Transforms can be:
        str                 — single transform
        list                — multiple transforms or conditional transforms
        dict with REPLACE_MAP — value mapping
    Stops pipeline if value becomes None.
    """
    if transforms is None:
        return value
    # Normalise to list
    if isinstance(transforms, str):
        transforms = [transforms]
    elif isinstance(transforms, dict):
        transforms = [transforms]
    for transform in transforms:
        if value is None:
            break
        if isinstance(transform, str):
            # Simple transform — always apply
            value = _apply_single_transform(value, transform, row)
        elif isinstance(transform, dict):
            if "REPLACE_MAP" in transform:
                # Value mapping
                mapping  = transform["REPLACE_MAP"]
                str_val  = str(value).strip()
                value    = mapping.get(str_val, value)
            elif "Condition" in transform:
                # Conditional transform
                condition = transform.get("Condition", {})
                apply_t   = transform.get("Apply")
                if _evaluate_condition(row or {}, condition):
                    if isinstance(apply_t, dict) and "REPLACE" in apply_t:
                        value = apply_t["REPLACE"]
                    elif apply_t:
                        value = _apply_single_transform(value, apply_t, row)
    return value

def _normalise_null(value, null_values: list) -> object:
    """
    Converts value to None if it matches any null representation.
    Empty string and whitespace-only always treated as null.
    """
    if value is None:
        return None
    # pandas/numpy missing markers (NaN, NaT, pd.NA) are not None but are null.
    # Without this, str(nan) == "nan" flows into DATE/INTEGER conversion and
    # raises "Cannot parse date 'nan'".
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass   # arrays / unhashable types — fall through to string handling
    s = str(value).strip()
    if not s:
        return None

    # Literal string forms of missing values
    if s.lower() in ("nan", "nat", "none", "null", "<na>"):
        return None
    if null_values:
        for nv in null_values:
            if s == str(nv).strip():
                return None
    return value

# ─────────────────────────────────────────────────────────────────────────────
# Row processing
# ─────────────────────────────────────────────────────────────────────────────
def _process_row(row: dict, columns_config: list,
                 null_values: list) -> tuple:
    """
    Applies column config (rename, null, transform, type) to a row dict.
    Returns:
        (processed_row, parse_errors)
        parse_errors is list of error dicts — empty if no errors
    """
    if not columns_config:
        # No column config — apply null values only
        return (
            {k: _normalise_null(v, null_values) for k, v in row.items()},
            []
        )
    processed   = {}
    parse_errors = []
    # Normalised lookup: lowercase + underscores so config names like
    # "Claim_Pharm_ID_RX" match Excel headers like "CLAIM_PHARM_ID_RX",
    # and "Days Supply" matches "DAYS_SUPPLY".
    def _norm(k):
        return str(k).lower().replace(" ", "_") if k is not None else ""
    
    norm_lookup = {_norm(k): v for k, v in row.items()}

    for col_def in columns_config:
        # Output key: Name, else SourceName. Configs that only declare
        # SourceName (PDF anchor style) were producing an empty "" key,
        # collapsing every column into one entry.
        name        = col_def.get("Name") or col_def.get("SourceName", "")
        source_name = col_def.get("SourceName", "") or name
        if not name:
            continue   # nothing to key the output on
        col_type    = (col_def.get("Type") or "STRING").upper()
        required    = col_def.get("Required", False)
        transforms  = col_def.get("Transform")
        col_nulls   = col_def.get("NullValues", null_values)
 

        # Get raw value — exact match first, then case/underscore-normalised fallback
        raw = row.get(source_name)
        if raw is None and source_name != name:
            raw = row.get(name)
        if raw is None:
            raw = norm_lookup.get(_norm(source_name))
        # Null normalisation
        value = _normalise_null(raw, col_nulls)
        # Required check
        if required and value is None:
            parse_errors.append({
                "Column": name,
                "Value" : raw,
                "Reason": f"Required column '{name}' is null."
            })
            processed[name] = None
            continue
        if value is None:
            processed[name] = None
            continue
        # Apply transforms
        try:
            value = apply_transforms(value, transforms, row)
        except Exception as e:
            parse_errors.append({
                "Column": name,
                "Value" : raw,
                "Reason": f"Transform failed: {e}"
            })
            processed[name] = None
            continue
        # Type conversion
        if value is not None:
            try:
                if col_type == "INTEGER":
                    value = int(str(value).strip())
                elif col_type == "DECIMAL":
                    value = float(Decimal(str(value).strip()))
                elif col_type == "DATE":
                    if not isinstance(value, datetime):
                        value = _parse_date(str(value))
                elif col_type == "DATETIME":
                    if not isinstance(value, datetime):
                        value = _parse_date(str(value))
                elif col_type == "BOOLEAN":
                    if not isinstance(value, bool):
                        value = apply_transforms(value, "BOOLEAN_AUTO")
                else:
                    value = str(value)
            except Exception as e:
                parse_errors.append({
                    "Column": name,
                    "Value" : raw,
                    "Reason": f"Type conversion to {col_type} failed: {e}"
                })
                processed[name] = None
                continue
        processed[name] = value
    return processed, parse_errors

# ─────────────────────────────────────────────────────────────────────────────
# Pre-processing — strip empty, comment, header, footer rows
# ─────────────────────────────────────────────────────────────────────────────
def _preprocess_lines(lines: list, file_config: dict) -> tuple:
    """
    Pre-processes raw lines:
        1. Skip empty rows
        2. Skip comment rows
        3. Skip SkipRows
        4. Identify header row
        5. Identify footer rows
        6. Strip row type markers (StripIdentifier)
        7. Filter data rows (DataIdentifier)
    Returns:
        (header_line, data_lines, footer_lines, skipped_counts)
    """
    skip_rows         = int(file_config.get("SkipRows", 0) or 0)
    footer_rows       = int(file_config.get("FooterRows", 0) or 0)
    header_identifier = file_config.get("HeaderIdentifier")
    data_identifier   = file_config.get("DataIdentifier")
    footer_identifier = file_config.get("FooterIdentifier")
    strip_identifier  = file_config.get("StripIdentifier", False)
    comment_char      = file_config.get("CommentChar")
    has_header        = file_config.get("Header", True)
    # Normalise comment chars
    comment_chars = []
    if comment_char:
        if isinstance(comment_char, list):
            comment_chars = comment_char
        else:
            comment_chars = [comment_char]
    skipped_empty   = 0
    skipped_comment = 0
    working_lines   = []
    for line in lines:
        stripped = line.strip()
        # Skip empty
        if not stripped:
            skipped_empty += 1
            continue
        # Skip comments
        is_comment = False
        for cc in comment_chars:
            if stripped.startswith(cc):
                is_comment = True
                break
        if is_comment:
            skipped_comment += 1
            continue
        working_lines.append(line)
    # Skip leading metadata rows
    working_lines = working_lines[skip_rows:]
    # Identify header
    header_line  = None
    data_start   = 0
    if header_identifier:
        # Find first line matching header identifier
        for i, line in enumerate(working_lines):
            if line.strip().startswith(header_identifier):
                header_line = line
                if strip_identifier:
                    # Remove identifier prefix
                    header_line = ",".join(
                        line.strip().split(",")[1:]
                    )
                data_start = i + 1
                break
    elif has_header and working_lines:
        header_line = working_lines[0]
        data_start  = 1
    remaining = working_lines[data_start:]
    # Identify and separate footer rows
    footer_lines = []
    if footer_identifier:
        data_lines_filtered = []
        for line in remaining:
            if line.strip().startswith(footer_identifier):
                footer_lines.append(line)
            else:
                data_lines_filtered.append(line)
        remaining = data_lines_filtered
    elif footer_rows > 0:
        footer_lines = remaining[-footer_rows:]
        remaining    = remaining[:-footer_rows]
    # Filter by data identifier
    if data_identifier:
        filtered = []
        for line in remaining:
            if line.strip().startswith(data_identifier):
                if strip_identifier:
                    line = ",".join(line.strip().split(",")[1:])
                filtered.append(line)
        remaining = filtered
    logger.info(
        f"File_parser: Pre-processing — "
        f"skipped {skipped_empty} empty, "
        f"{skipped_comment} comment rows. "
        f"Data rows: {len(remaining)}"
    )
    return header_line, remaining, footer_lines, {
        "empty"  : skipped_empty,
        "comment": skipped_comment
    }

def _validate_footer(footer_lines: list, data_df: pd.DataFrame,
                     footer_config: dict, delimiter: str,
                     test_case_number: int = None) -> list:
    """
    Validates footer record count and amount total against parsed data.
    Returns list of validation errors.
    """
    errors      = []
    count_col   = footer_config.get("CountColumn")
    amount_col  = footer_config.get("AmountColumn")
    if not footer_lines:
        return errors
    # Parse first footer line
    footer_parts = footer_lines[0].strip().split(delimiter)
    if count_col is not None:
        try:
            idx             = int(count_col) - 1
            declared_count  = int(footer_parts[idx].strip())
            actual_count    = len(data_df)
            if declared_count != actual_count:
                errors.append(
                    f"Footer record count mismatch: "
                    f"declared={declared_count}, actual={actual_count}"
                )
            else:
                logger.info(
                    f"File_parser: Footer count validation — "
                    f"{declared_count} records ✓"
                )
        except Exception as e:
            errors.append(f"Footer count validation failed: {e}")
    if amount_col is not None:
        try:
            idx             = int(amount_col) - 1
            declared_amount = Decimal(str(footer_parts[idx]).strip())
            # Sum first numeric column that looks like an amount
            # Use CompareFields info if available — for now sum all numeric cols
            numeric_cols = data_df.select_dtypes(include=["float64", "int64"]).columns
            if len(numeric_cols) > 0:
                actual_amount = Decimal(str(data_df[numeric_cols[0]].sum()))
                if declared_amount != actual_amount:
                    errors.append(
                        f"Footer amount mismatch: "
                        f"declared={declared_amount}, actual={actual_amount}"
                    )
                else:
                    logger.info(
                        f"File_parser: Footer amount validation — "
                        f"{declared_amount} ✓"
                    )
        except Exception as e:
            errors.append(f"Footer amount validation failed: {e}")
    return errors

# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────
def _validate_schema(file_columns: list, config_columns: list,
                     strict: bool = False) -> list:
    """
    Validates file columns against configured column definitions.
    Rules:
        Required column missing from file → error
        Extra columns in file + strict    → error
        Extra columns in file + not strict → warning
    Returns list of schema errors.
    """
    errors = []
    if not config_columns:
        return errors
    config_names = set()
    for col in config_columns:
        source_name = col.get("SourceName", "") or col.get("Name", "")
        if source_name:
            config_names.add(source_name)
    file_col_set = set(file_columns)
    # Check required columns present
    for col in config_columns:
        source_name = col.get("SourceName", "") or col.get("Name", "")
        required    = col.get("Required", False)
        if required and source_name and source_name not in file_col_set:
            errors.append(
                f"Required column '{source_name}' not found in file. "
                f"Available: {list(file_col_set)}"
            )
    # Check for extra columns
    extra = file_col_set - config_names
    if extra:
        if strict:
            errors.append(
                f"StrictSchema violation — extra columns in file: {sorted(extra, key=str)}"
            )
        else:
            # logger.warning(
            #     f"File_parser: Extra columns in file (ignored): {sorted(extra, key=str)}"
            # )
            pass
    if not errors:
        logger.info(
            f"File_parser: Schema validation PASS — "
            f"all required columns present."
        )
    return errors

# ─────────────────────────────────────────────────────────────────────────────
# Format parsers
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_multi_delimiters(line: str, primary: str, alternates: list,
                                quote_char: str) -> str:
    """
    Replaces any alternate delimiter characters with the primary delimiter,
    skipping characters inside quoted segments so an alternate-delimiter
    character that appears as literal quoted text is preserved rather than
    treated as a split point. Used for MultiDelimiter support — a row may
    use either the primary delimiter or one of the alternates.
    """
    alt_set = (set(alternates) - {primary}) if alternates else set()
    if not alt_set:
        return line
    result    = []
    in_quotes = False
    for ch in line:
        if ch == quote_char:
            in_quotes = not in_quotes
            result.append(ch)
        elif ch in alt_set and not in_quotes:
            result.append(primary)
        else:
            result.append(ch)
    return "".join(result)
 
 
def _parse_delimited(content: bytes, file_config: dict,
                     filename: str = "") -> tuple:
    """
    Parses CSV, PIPE, TSV and other delimited formats.
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    encoding              = detect_encoding(content, file_config.get("Encoding"))
    delimiter             = file_config.get("Delimiter", ",") or ","
    multi_delimiter       = file_config.get("MultiDelimiter") or []
    quote_char            = file_config.get("QuoteChar", '"') or '"'
    escape_char           = file_config.get("EscapeChar")
    skip_trailing         = file_config.get("SkipTrailingDelimiter", False)
    null_values           = file_config.get("NullValues", []) or []
    columns_config        = file_config.get("Columns", []) or []
    strict_schema         = file_config.get("StrictSchema", False)
    on_col_mismatch       = (file_config.get("OnColumnMismatch") or "FAIL").upper()
    footer_validation_cfg = file_config.get("FooterValidation", {}) or {}
    deduplicate           = file_config.get("DeduplicateSource", False)
    # Handle TSV
    fmt = (file_config.get("Format") or "CSV").upper()
    if fmt == "TSV":
        delimiter = "\t"
    text  = content.decode(encoding, errors="replace")
    lines = text.splitlines()
    header_line, data_lines, footer_lines, skipped = _preprocess_lines(
        lines, file_config
    )
    # Build CSV text for pandas
    csv_lines = []
    if header_line:
        csv_lines.append(header_line)
    csv_lines.extend(data_lines)
 
    # MultiDelimiter — rows may use the primary delimiter or any alternate.
    # Normalise alternates to the primary delimiter (outside quoted text)
    # before pandas sees a single, consistent delimiter.
    if multi_delimiter:
        csv_lines = [
            _normalize_multi_delimiters(line, delimiter, multi_delimiter, quote_char)
            for line in csv_lines
        ]
        logger.info(
            f"File_parser: '{filename}' — MultiDelimiter {multi_delimiter} "
            f"normalised to primary delimiter '{delimiter}'."
        )
 
    csv_text = "\n".join(csv_lines)
    # Parse with pandas
    try:
        df = pd.read_csv(
            io.StringIO(csv_text),
            sep          = delimiter,
            quotechar    = quote_char,
            escapechar   = escape_char,
            header       = 0 if header_line else None,
            dtype        = str,
            keep_default_na = False,
            on_bad_lines = "warn" if on_col_mismatch == "TRUNCATE" else "error"
        )
    except Exception as e:
        raise ValueError(f"Failed to parse delimited file '{filename}': {e}")
    # Strip trailing delimiter column
    if skip_trailing:
        last_col = df.columns[-1]
        if df[last_col].isna().all() or (df[last_col] == "").all():
            df = df.drop(columns=[last_col])
    # Schema validation
    schema_errors = _validate_schema(
        list(df.columns), columns_config, strict_schema
    )
    if schema_errors:
        return pd.DataFrame(), [], schema_errors, []
    # Process rows
    parse_errors = []
    processed_rows = []
    for row_idx, row in df.iterrows():
        row_dict = row.to_dict()
        processed, errors = _process_row(
            row_dict, columns_config, null_values
        )
        if errors:
            for err in errors:
                err["RowNumber"]   = row_idx + 2  # +2 for header + 1-based
                err["RawContent"]  = delimiter.join(
                    str(v) for v in row_dict.values()
                )
            parse_errors.extend(errors)
        else:
            processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    # Deduplicate
    if deduplicate and not result_df.empty:
        before = len(result_df)
        result_df = result_df.drop_duplicates()
        after  = len(result_df)
        if before != after:
            logger.warning(
                f"File_parser: Deduplicated {before - after} duplicate row(s)."
            )
    # Footer validation
    footer_errors = []
    if footer_validation_cfg.get("Enabled") and footer_lines:
        footer_errors = _validate_footer(
            footer_lines, result_df,
            footer_validation_cfg, delimiter
        )
    logger.info(
        f"File_parser: '{filename}' — "
        f"parsed {len(result_df)} row(s), "
        f"{len(parse_errors)} parse error(s)."
    )
    return result_df, parse_errors, schema_errors, footer_errors

def _parse_fixed_width(content: bytes, file_config: dict,
                       filename: str = "") -> tuple:
    """
    Parses fixed-width files.
    Supports single layout and multi-layout (RecordSets).
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    encoding      = detect_encoding(content, file_config.get("Encoding"))
    null_values   = file_config.get("NullValues", []) or []
    record_sets   = file_config.get("RecordSets") or []
    rec_type_col  = file_config.get("RecordTypeColumn") or {}
    footer_config = file_config.get("FooterValidation", {}) or {}
    text  = content.decode(encoding, errors="replace")
    lines = text.splitlines()
    _, data_lines, footer_lines, _ = _preprocess_lines(lines, file_config)
    parse_errors = []
    all_rows     = []
    if record_sets:
        # Multi-layout — identify record type per row
        rt_pos   = int(rec_type_col.get("Position", 1)) - 1
        rt_width = int(rec_type_col.get("Width", 1))
        # Build lookup — record_type → columns_config
        rt_map = {}
        for rs in record_sets:
            rt     = rs.get("RecordType", "")
            skip   = rs.get("Skip", False)
            cols   = rs.get("Columns", [])
            rt_map[rt] = {"skip": skip, "columns": cols}
        for row_idx, line in enumerate(data_lines):
            if len(line) <= rt_pos:
                continue
            rt = line[rt_pos:rt_pos + rt_width].strip()
            rs_def = rt_map.get(rt)
            if rs_def is None:
                logger.warning(
                    f"File_parser: Unknown record type '{rt}' "
                    f"at row {row_idx + 1} — skipped."
                )
                continue
            if rs_def["skip"]:
                continue
            # Extract fields by position
            row_dict = {}
            for col_def in rs_def["columns"]:
                pos   = int(col_def.get("Position", 1)) - 1
                width = int(col_def.get("Width", 1))
                name  = col_def.get("SourceName", "") or col_def.get("Name", "")
                raw   = line[pos:pos + width] if len(line) >= pos + width else ""
                row_dict[name] = raw
            columns_config = rs_def["columns"]
            processed, errors = _process_row(
                row_dict, columns_config, null_values
            )
            if errors:
                for err in errors:
                    err["RowNumber"]  = row_idx + 1
                    err["RawContent"] = line
                parse_errors.extend(errors)
            else:
                all_rows.append(processed)
    else:
        # Single layout
        columns_config = file_config.get("Columns", []) or []
        for row_idx, line in enumerate(data_lines):
            row_dict = {}
            for col_def in columns_config:
                pos   = int(col_def.get("Position", 1)) - 1
                width = int(col_def.get("Width", 1))
                name  = col_def.get("SourceName", "") or col_def.get("Name", "")
                raw   = line[pos:pos + width] if len(line) >= pos + width else ""
                row_dict[name] = raw
            processed, errors = _process_row(
                row_dict, columns_config, null_values
            )
            if errors:
                for err in errors:
                    err["RowNumber"]  = row_idx + 1
                    err["RawContent"] = line
                parse_errors.extend(errors)
            else:
                all_rows.append(processed)
    result_df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
    # Footer validation
    footer_errors = []
    if footer_config.get("Enabled") and footer_lines:
        footer_errors = _validate_footer(
            footer_lines, result_df, footer_config, " "
        )
    logger.info(
        f"File_parser: '{filename}' — "
        f"fixed-width parsed {len(result_df)} row(s), "
        f"{len(parse_errors)} parse error(s)."
    )
    return result_df, parse_errors, [], footer_errors

def _parse_excel(content: bytes, file_config: dict,
                 filename: str = "") -> tuple:
    """
    Parses Excel XLSX/XLS files.
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    sheet             = file_config.get("Sheet", 0)
    skip_rows         = int(file_config.get("SkipRows", 0) or 0)
    footer_rows       = int(file_config.get("FooterRows", 0) or 0)
    has_header        = file_config.get("Header", True)
    use_formula_vals  = file_config.get("UseFormulaValues", True)
    include_hidden    = file_config.get("IncludeHidden", False)
    password          = file_config.get("Password")
    named_range       = file_config.get("NamedRange")
    null_values       = file_config.get("NullValues", []) or []
    columns_config    = file_config.get("Columns", []) or []
    strict_schema     = file_config.get("StrictSchema", False)
    footer_config     = file_config.get("FooterValidation", {}) or {}
    # Handle password protected Excel
    if password:
        try:
            import msoffcrypto
            encrypted = io.BytesIO(content)
            decrypted = io.BytesIO()
            office    = msoffcrypto.OfficeFile(encrypted)
            office.load_key(password=password)
            office.decrypt(decrypted)
            content   = decrypted.getvalue()
        except ImportError:
            raise ImportError(
                "msoffcrypto-tool required for password-protected Excel. "
                "Install with: pip install msoffcrypto-tool"
            )
    # Read Excel
    try:
        if sheet == "ALL":
            xl   = pd.ExcelFile(io.BytesIO(content))
            dfs  = []
            for sheet_name in xl.sheet_names:
                df = pd.read_excel(
                    io.BytesIO(content),
                    sheet_name = sheet_name,
                    skiprows   = skip_rows,
                    header     = 0 if has_header else None,
                    dtype      = str
                )
                df["_sheet"] = sheet_name
                dfs.append(df)
            df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        else:
            df = pd.read_excel(
                io.BytesIO(content),
                sheet_name = sheet,
                skiprows   = skip_rows,
                header     = 0 if has_header else None,
                dtype      = str
            )
    except Exception as e:
        raise ValueError(f"Failed to parse Excel file '{filename}': {e}")
    # Remove footer rows
    if footer_rows > 0:
        footer_df = df.tail(footer_rows)
        df        = df.iloc[:-footer_rows]
    else:
        footer_df = pd.DataFrame()
    # Drop hidden rows if needed — pandas doesn't expose row visibility
    # openpyxl needed for hidden row detection
    if not include_hidden:
        # Best effort — drop rows where all values are NaN
        df = df.dropna(how="all")
    df = df.fillna("")
    # Schema validation
    schema_errors = _validate_schema(
        list(df.columns), columns_config, strict_schema
    )
    if schema_errors:
        return pd.DataFrame(), [], schema_errors, []
    # Process rows
    parse_errors   = []
    processed_rows = []
    for row_idx, row in df.iterrows():
        row_dict = row.to_dict()
        processed, errors = _process_row(
            row_dict, columns_config, null_values
        )
        if errors:
            for err in errors:
                err["RowNumber"]  = row_idx + skip_rows + 2
                err["RawContent"] = str(row_dict)
            parse_errors.extend(errors)
        else:
            processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    # Footer validation
    footer_errors = []
    if footer_config.get("Enabled") and not footer_df.empty:
        footer_lines  = [",".join(str(v) for v in row) for _, row in footer_df.iterrows()]
        footer_errors = _validate_footer(
            footer_lines, result_df, footer_config, ","
        )
    logger.info(
        f"File_parser: '{filename}' — "
        f"Excel parsed {len(result_df)} row(s), "
        f"{len(parse_errors)} parse error(s)."
    )
    return result_df, parse_errors, schema_errors, footer_errors

def _parse_json(content: bytes, file_config: dict,
                filename: str = "") -> tuple:
    """
    Parses JSON files.
    Handles nested structures via RecordPath.
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    import json
    encoding       = detect_encoding(content, file_config.get("Encoding"))
    record_path    = file_config.get("RecordPath")
    null_values    = file_config.get("NullValues", []) or []
    columns_config = file_config.get("Columns", []) or []
    strict_schema  = file_config.get("StrictSchema", False)
    text = content.decode(encoding, errors="replace")
    try:
        data = json.loads(text)
    except Exception as e:
        raise ValueError(f"Failed to parse JSON file '{filename}': {e}")
    # Navigate to record path
    if record_path:
        for key in record_path.split("."):
            if isinstance(data, list):
                # Explode array
                data = [item.get(key) for item in data if isinstance(item, dict)]
                data = [item for sublist in data
                        for item in (sublist if isinstance(sublist, list)
                                     else [sublist])]
            elif isinstance(data, dict):
                data = data.get(key)
            else:
                raise ValueError(
                    f"Cannot navigate RecordPath '{record_path}' — "
                    f"unexpected type at '{key}'."
                )
    if not isinstance(data, list):
        data = [data] if data else []
    # Flatten to DataFrame
    try:
        df = pd.json_normalize(data)
        df = df.fillna("").astype(str)
    except Exception as e:
        raise ValueError(f"Failed to normalize JSON '{filename}': {e}")
    # Schema validation
    schema_errors = _validate_schema(
        list(df.columns), columns_config, strict_schema
    )
    if schema_errors:
        return pd.DataFrame(), [], schema_errors, []
    # Process rows
    parse_errors   = []
    processed_rows = []
    for row_idx, row in df.iterrows():
        row_dict  = row.to_dict()
        processed, errors = _process_row(
            row_dict, columns_config, null_values
        )
        if errors:
            for err in errors:
                err["RowNumber"]  = row_idx + 1
                err["RawContent"] = str(row_dict)
            parse_errors.extend(errors)
        else:
            processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    logger.info(
        f"File_parser: '{filename}' — "
        f"JSON parsed {len(result_df)} row(s)."
    )
    return result_df, parse_errors, schema_errors, []

def _parse_xml(content: bytes, file_config: dict,
               filename: str = "") -> tuple:
    """
    Parses XML files.
    Supports namespaces, attributes, multiple RecordSets, nested flattening.
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    import xml.etree.ElementTree as ET
    encoding          = detect_encoding(content, file_config.get("Encoding"))
    record_tag        = file_config.get("RecordTag", "")
    record_sets       = file_config.get("RecordSets") or []
    namespace_aware   = file_config.get("NamespaceAware", False)
    namespaces        = file_config.get("Namespaces") or {}
    include_attrs     = file_config.get("IncludeAttributes", False)
    record_filter     = file_config.get("RecordFilter")
    flatten_depth     = int(file_config.get("FlattenDepth", 2) or 2)
    null_values       = file_config.get("NullValues", []) or []
    columns_config    = file_config.get("Columns", []) or []
    strict_schema     = file_config.get("StrictSchema", False)
    text = content.decode(encoding, errors="replace")
    try:
        root = ET.fromstring(text)
    except Exception as e:
        raise ValueError(f"Failed to parse XML '{filename}': {e}")
    # Strip namespaces if not namespace-aware
    if not namespace_aware:
        for elem in root.iter():
            elem.tag = elem.tag.split("}")[1] if "}" in elem.tag else elem.tag
    def _elem_to_dict(elem, depth=0, prefix=""):
        """Recursively flattens XML element to dict."""
        result = {}
        # Attributes
        if include_attrs:
            for attr_name, attr_val in elem.attrib.items():
                key = f"@{attr_name}"
                result[f"{prefix}{key}" if prefix else key] = attr_val
        # Child elements
        for child in elem:
            tag = child.tag.split("}")[1] if "}" in child.tag else child.tag
            child_key = f"{prefix}{tag}" if prefix else tag
            if len(child) > 0 and depth < flatten_depth:
                # Has children — recurse
                child_dict = _elem_to_dict(child, depth + 1, f"{child_key}.")
                result.update(child_dict)
            else:
                result[child_key] = child.text or ""
        # Direct text
        if elem.text and elem.text.strip():
            result[prefix.rstrip(".") or elem.tag] = elem.text.strip()
        return result
    def _matches_filter(elem, flt):
        """Checks if element matches RecordFilter."""
        if not flt:
            return True
        attr     = flt.get("Attribute", "")
        operator = flt.get("Operator", "=")
        value    = str(flt.get("Value", ""))
        elem_val = elem.attrib.get(attr, "")
        if operator == "=":
            return elem_val == value
        if operator == "!=":
            return elem_val != value
        if operator == "IN":
            return elem_val in [v.strip() for v in value.split(",")]
        return True
    parse_errors   = []
    processed_rows = []
    # Determine which record sets to process
    active_record_sets = []
    if record_sets:
        for rs in record_sets:
            if not rs.get("Skip", False):
                active_record_sets.append(rs)
    elif record_tag:
        active_record_sets = [{
            "RecordTag"   : record_tag,
            "RecordFilter": record_filter,
            "Columns"     : columns_config,
            "Skip"        : False
        }]
    for rs in active_record_sets:
        rt         = rs.get("RecordTag", "")
        rs_filter  = rs.get("RecordFilter")
        rs_columns = rs.get("Columns", columns_config)
        # Find elements matching tag
        # Try with namespace prefix first, then without
        elements = root.findall(f".//{rt}", namespaces)
        if not elements:
            # Try stripping namespace
            plain_tag = rt.split(":")[-1] if ":" in rt else rt
            elements  = root.findall(f".//{plain_tag}")
        for elem in elements:
            if not _matches_filter(elem, rs_filter):
                continue
            row_dict           = _elem_to_dict(elem)
            processed, errors  = _process_row(
                row_dict, rs_columns, null_values
            )
            if errors:
                for err in errors:
                    err["RawContent"] = ET.tostring(elem, encoding="unicode")
                parse_errors.extend(errors)
            else:
                processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    # Schema validation on first record set only
    schema_errors = []
    if active_record_sets and not result_df.empty:
        schema_errors = _validate_schema(
            list(result_df.columns),
            active_record_sets[0].get("Columns", columns_config),
            strict_schema
        )
    logger.info(
        f"File_parser: '{filename}' — "
        f"XML parsed {len(result_df)} row(s)."
    )
    return result_df, parse_errors, schema_errors, []

def _parse_hl7(content: bytes, file_config: dict,
               filename: str = "") -> tuple:
    """
    Parses HL7 v2.x messages.
    Groups segments by MSH (one row per message).
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    encoding     = detect_encoding(content, file_config.get("Encoding"))
    record_sets  = file_config.get("RecordSets", []) or []
    null_values  = file_config.get("NullValues", []) or []
    version      = file_config.get("Version")
    text = content.decode(encoding, errors="replace")
    # Split into messages by MSH
    # HL7 uses \r as segment terminator
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    segments_raw = [s.strip() for s in text.split("\r") if s.strip()]
    # Group into messages
    messages = []
    current  = []
    for seg in segments_raw:
        if seg.startswith("MSH"):
            if current:
                messages.append(current)
            current = [seg]
        else:
            if current:
                current.append(seg)
    if current:
        messages.append(current)
    if not messages:
        raise ValueError(f"No HL7 messages found in '{filename}'.")
    # Build segment config lookup
    seg_map = {}
    for rs in record_sets:
        seg_id = rs.get("Segment", "")
        skip   = rs.get("Skip", False)
        cols   = rs.get("Columns", [])
        seg_map[seg_id.upper()] = {"skip": skip, "columns": cols}
    parse_errors   = []
    processed_rows = []
    for msg_idx, segments in enumerate(messages):
        # Parse each segment in message
        seg_data = {}
        for seg in segments:
            # Detect field separator from MSH
            if seg.startswith("MSH"):
                field_sep = seg[3] if len(seg) > 3 else "|"
                comp_sep  = seg[4] if len(seg) > 4 else "^"
            else:
                field_sep = "|"
                comp_sep  = "^"
            parts  = seg.split(field_sep)
            seg_id = parts[0].upper()
            if seg_id not in seg_map or seg_map[seg_id]["skip"]:
                continue
            # Extract fields by notation SEG.field.component
            seg_data[seg_id] = {}
            for col_def in seg_map[seg_id]["columns"]:
                source_name = col_def.get("SourceName", "")
                # Parse notation e.g. PID.3.1
                notation_parts = source_name.split(".")
                if len(notation_parts) >= 2:
                    field_idx = int(notation_parts[1]) - 1
                    # MSH field 1 is field separator itself — adjust
                    if seg_id == "MSH":
                        field_idx += 1
                    field_val = parts[field_idx] if len(parts) > field_idx else ""
                    if len(notation_parts) >= 3:
                        comp_idx  = int(notation_parts[2]) - 1
                        comps     = field_val.split(comp_sep)
                        field_val = comps[comp_idx] if len(comps) > comp_idx else ""
                    seg_data[seg_id][source_name] = field_val
        # Flatten all segments into one row
        row_dict = {}
        for seg_id, fields in seg_data.items():
            row_dict.update(fields)
        if not row_dict:
            continue
        # Process with column configs from all non-skipped segments
        all_columns = []
        for rs in record_sets:
            if not rs.get("Skip", False):
                all_columns.extend(rs.get("Columns", []))
        processed, errors = _process_row(row_dict, all_columns, null_values)
        if errors:
            for err in errors:
                err["RowNumber"]  = msg_idx + 1
                err["RawContent"] = "|".join(segments[:2])
            parse_errors.extend(errors)
        else:
            processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    logger.info(
        f"File_parser: '{filename}' — "
        f"HL7 parsed {len(result_df)} message(s)."
    )
    return result_df, parse_errors, [], []

def _parse_ncpdp_d0(content: bytes, file_config: dict,
                    filename: str = "") -> tuple:
    """
    Parses NCPDP D.0 fixed-length qualifier format.
    Returns (DataFrame, parse_errors, schema_errors, footer_errors)
    """
    encoding    = detect_encoding(content, file_config.get("Encoding"))
    record_sets = file_config.get("RecordSets", []) or []
    null_values = file_config.get("NullValues", []) or []
    text = content.decode(encoding, errors="replace")
    # NCPDP D.0 field lengths — standard positions
    # This is a subset — full dictionary from NCPDP spec
    NCPDP_FIELD_LENGTHS = {
        "AM": 2,   # Transaction count
        "EM": 2,   # Segment ID
        "BN": 8,   # Processor control number
        "C2": 20,  # Patient ID
        "C3": 8,   # Date of birth
        "C4": 1,   # Patient gender
        "C5": 2,   # Patient relationship
        "D1": 7,   # Date of service
        "D2": 11,  # Product/service ID
        "D3": 2,   # Quantity dispensed
        "D5": 10,  # Quantity dispensed (decimal)
        "D6": 4,   # Days supply
        "D7": 2,   # Compound code
        "D8": 3,   # Days supply
        "D9": 1,   # DAW product selection code
        "E1": 15,  # Prescriber ID
        "E2": 2,   # Prescriber ID qualifier
        "EE": 10,  # Primary care provider ID
    }
    # Build segment config lookup
    seg_map = {}
    for rs in record_sets:
        seg_id = rs.get("Segment", "").upper()
        skip   = rs.get("Skip", False)
        cols   = rs.get("Columns", [])
        seg_map[seg_id] = {"skip": skip, "columns": cols}
    parse_errors   = []
    processed_rows = []
    # Split into transactions — each starts with a segment identifier
    # NCPDP D.0 transactions are typically separated by newlines
    transactions = [t.strip() for t in text.split("\n") if t.strip()]
    for tx_idx, transaction in enumerate(transactions):
        # Parse qualifier-value pairs
        tx_data = {}
        i = 0
        while i < len(transaction) - 1:
            qualifier = transaction[i:i+2]
            length    = NCPDP_FIELD_LENGTHS.get(qualifier)
            if length is None:
                i += 1
                continue
            value    = transaction[i+2:i+2+length]
            tx_data[qualifier] = value.strip()
            i += 2 + length
        # Determine segment type from EM field or first qualifier
        seg_id = tx_data.get("EM", tx_data.get("AM", "")).strip().upper()
        if seg_id not in seg_map or seg_map[seg_id]["skip"]:
            continue
        columns_config = seg_map[seg_id]["columns"]
        processed, errors = _process_row(tx_data, columns_config, null_values)
        if errors:
            for err in errors:
                err["RowNumber"]  = tx_idx + 1
                err["RawContent"] = transaction[:50]
            parse_errors.extend(errors)
        else:
            processed_rows.append(processed)
    result_df = pd.DataFrame(processed_rows) if processed_rows else pd.DataFrame()
    logger.info(
        f"File_parser: '{filename}' — "
        f"NCPDP D.0 parsed {len(result_df)} transaction(s)."
    )
    return result_df, parse_errors, [], []

def _parse_ncpdp_script(content: bytes, file_config: dict,
                        filename: str = "") -> tuple:
    """
    Parses NCPDP SCRIPT XML format.
    Delegates to XML parser with SCRIPT-specific defaults.
    """
    # SCRIPT is XML — use XML parser
    # Add default namespace if not specified
    if not file_config.get("Namespaces"):
        version = file_config.get("Version", "10.6")
        file_config = dict(file_config)
        file_config["Namespaces"] = {
            "ncpdp": f"http://www.ncpdp.org/schema/script/{version}"
        }
        file_config["NamespaceAware"] = True
    return _parse_xml(content, file_config, filename)

# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def parse_file(filename: str, content: bytes,
               file_config: dict) -> dict:
    """
    Parses file content into a DataFrame using FileConfig.
    Args:
        filename    : Original filename (for logging/audit)
        content     : Raw file bytes (after decryption/decompression)
        file_config : FileConfig dict from test case document
    Returns dict:
    {
        "dataframe"    : pd.DataFrame,
        "parse_errors" : list of error dicts,
        "schema_errors": list of schema error strings,
        "footer_errors": list of footer validation errors,
        "row_count"    : int,
        "error_count"  : int,
        "filename"     : str,
        "encoding"     : str
    }
    Note: parse_errors does NOT fail silently.
    Caller should check error_count > 0 and fail test case.
    """
    file_config = file_config or {}
    fmt = (file_config.get("Format") or "AUTO").upper()
    # Auto-detect format from content
    if fmt == "AUTO":
        fmt = _detect_format(content, filename)
        logger.info(f"File_parser: Format auto-detected — '{fmt}'")
    # Detect encoding for text formats only — binary formats (xlsx, hl7 etc.)
    # produce meaningless chardet noise and don't need encoding detection
    _BINARY_FORMATS = {"EXCEL", "HL7", "NCPDP_D0", "NCPDP_SCRIPT", "PDF"}
    if fmt not in _BINARY_FORMATS:
        encoding = detect_encoding(
            content[:65536], file_config.get("Encoding")
        )
    else:
        encoding = "utf-8"
    # Dispatch to format parser
    parser_map = {
        "CSV"          : _parse_delimited,
        "PIPE"         : _parse_delimited,
        "TSV"          : _parse_delimited,
        "FIXED_WIDTH"  : _parse_fixed_width,
        "EXCEL"        : _parse_excel,
        "JSON"         : _parse_json,
        "XML"          : _parse_xml,
        "HL7"          : _parse_hl7,
        "NCPDP_D0"     : _parse_ncpdp_d0,
        "NCPDP_SCRIPT" : _parse_ncpdp_script,
        "PDF"          : _parse_pdf, 
    }
    parser = parser_map.get(fmt)
    if not parser:
        raise ValueError(
            f"Unsupported FileConfig.Format '{fmt}'. "
            f"Supported: {sorted(parser_map.keys())}"
        )
    df, parse_errors, schema_errors, footer_errors = parser(
        content, file_config, filename
    )
    # Log summary
    total_errors = len(parse_errors) + len(schema_errors) + len(footer_errors)
    if total_errors > 0:
        logger.issue(
            f"File_parser: '{filename}' — "
            f"{len(parse_errors)} parse error(s), "
            f"{len(schema_errors)} schema error(s), "
            f"{len(footer_errors)} footer error(s)."
        )
    else:
        logger.info(
            f"File_parser: '{filename}' — "
            f"parse complete, {len(df)} row(s), no errors."
        )
    return {
        "dataframe"    : df,
        "parse_errors" : parse_errors,
        "schema_errors": schema_errors,
        "footer_errors": footer_errors,
        "row_count"    : len(df),
        "error_count"  : total_errors,
        "filename"     : filename,
        "encoding"     : encoding,
        "format"       : fmt
    }

def _detect_format(content: bytes, filename: str = "") -> str:
    """
    Auto-detects file format from content and filename extension.
    """
    # Try filename extension first
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    ext_map = {
        "csv"  : "CSV",
        "txt"  : "CSV",
        "tsv"  : "TSV",
        "tab"  : "TSV",
        "xlsx" : "EXCEL",
        "xls"  : "EXCEL",
        "xlsm" : "EXCEL",
        "json" : "JSON",
        "xml"  : "XML",
        "hl7"  : "HL7",
        "dat"  : "FIXED_WIDTH",
        "pdf"  : "PDF",
    }
    if ext in ext_map:
        return ext_map[ext]
    # Try content sniffing
    try:
        sample = content[:500].decode("utf-8", errors="ignore").strip()
        if sample.startswith("<"):
            if "ncpdp" in sample.lower():
                return "NCPDP_SCRIPT"
            return "XML"
        if sample.startswith(("{", "[")):
            return "JSON"
        if sample.startswith("MSH|"):
            return "HL7"
        if "|" in sample.split("\n")[0]:
            return "PIPE"
        if "\t" in sample.split("\n")[0]:
            return "TSV"
        if "," in sample.split("\n")[0]:
            return "CSV"
    except Exception:
        pass
    return "CSV"

def fetch_and_parse(source: dict,
                    retry_attempts: int = 2,
                    retry_wait_minutes: float = 5,
                    add_source_file_column: bool = False) -> dict:
    """
    Convenience function — fetches and parses in one call.
    Handles multiple files (PickStrategy=ALL) by combining DataFrames.
    Returns same structure as parse_file() but with combined DataFrame.
    """
    from .File_fetcher import fetch_file
    file_config = source.get("FileConfig") or {}
    add_col     = add_source_file_column or source.get("AddSourceFileColumn", False)
    raw_files = fetch_file(
        source,
        retry_attempts=retry_attempts,
        retry_wait_minutes=retry_wait_minutes
    )
    if not raw_files:
        raise ValueError("No files retrieved from source.")
    all_dfs         = []
    all_parse_errors = []
    all_schema_errors = []
    all_footer_errors = []
    for filename, content in raw_files:
        result = parse_file(filename, content, file_config)
        df = result["dataframe"]
        if add_col and not df.empty:
            df["SourceFile"] = filename
        all_dfs.append(df)
        all_parse_errors.extend(result["parse_errors"])
        all_schema_errors.extend(result["schema_errors"])
        all_footer_errors.extend(result["footer_errors"])
    # Combine all DataFrames
    if all_dfs:
        # Schema consistency check for multiple files
        if len(all_dfs) > 1:
            first_cols = set(all_dfs[0].columns)
            for i, df in enumerate(all_dfs[1:], 2):
                if set(df.columns) != first_cols:
                    raise ValueError(
                        f"Schema inconsistency across files — "
                        f"file {i} has different columns: "
                        f"expected {sorted(first_cols)}, "
                        f"got {sorted(df.columns)}"
                    )
        combined_df = pd.concat(all_dfs, ignore_index=True)
    else:
        combined_df = pd.DataFrame()
    total_errors = (
        len(all_parse_errors) +
        len(all_schema_errors) +
        len(all_footer_errors)
    )
    logger.info(
        f"File_parser: Combined {len(raw_files)} file(s) — "
        f"{len(combined_df)} total rows, "
        f"{total_errors} total error(s)."
    )
    return {
        "dataframe"    : combined_df,
        "parse_errors" : all_parse_errors,
        "schema_errors": all_schema_errors,
        "footer_errors": all_footer_errors,
        "row_count"    : len(combined_df),
        "error_count"  : total_errors,
        "files"        : [f[0] for f in raw_files],
        "file_count"   : len(raw_files)
    }       
 
# ═══════════════════════════════════════════════════════════════════════════
# Generic Dynamic File-Location Pipeline
# ═══════════════════════════════════════════════════════════════════════════
#
# Distinct from the EDI835 pipeline (Edi835_fetcher.py), this handles the
# "dynamic file location" test case pattern:
#
#   Step 1 (ORACLE or MONGODB) — looks up a file path/location, stores it
#                                 in context under OutputKey. May return
#                                 ONE row (single dynamic file) or MANY rows
#                                 (multiple dynamic files to combine).
#   Step 2 (Type: "AUTO")      — terminal step. Reads InputKey from context,
#                                 builds a per-location Source dict, and
#                                 fetches + parses the file(s) via the normal
#                                 AUTO file-type detection (SFTP/UNC/S3/etc).
#
# Example Source array in test case document:
#
#   "Source": [
#       {
#           "Step"       : 1,
#           "Type"       : "ORACLE",
#           "Environment": "ORACLE_HTA_PROD",
#           "DB"         : "FIN",
#           "QueryType"  : "QUERY",
#           "Query"      : "SELECT FILE_LOCATION FROM FIN.FILE_REGISTRY WHERE EVENT_ID = :EventID",
#           "OutputKey"  : "FileLocation"
#       },
#       {
#           "Step"               : 2,
#           "Type"               : "AUTO",
#           "InputKey"           : "FileLocation",
#           "CredentialKey"      : "HTA_PREPROD",
#           "AddSourceFileColumn": true,
#           "FileConfig"         : { ... }
#       }
#   ]
# ═══════════════════════════════════════════════════════════════════════════
 
 
def _execute_pipeline_lookup_oracle(step: dict, context: dict, filter_values: dict) -> dict:
    """
    Executes an ORACLE lookup step for the dynamic file pipeline.
    Substitutes :field placeholders in the step's Query using filter_values
    (the test case's resolved DataFilters), executes the query, and stores
    ALL matching OutputKey values into context — a single value if one row
    matched (single-file pattern), or a list if multiple rows matched
    (multi-file combine pattern).
    """
    from .OracleDB import OracleConnection
 
    query       = step.get("Query", "")
    environment = (step.get("Environment") or "").upper()
    output_key  = step.get("OutputKey", "")
 
    if not query or not output_key:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [ORACLE]: "
            f"Requires 'Query' and 'OutputKey'."
        )
 
    resolved_query = query
    for field, value in filter_values.items():
        placeholder = f":{field}"
        if placeholder in resolved_query:
            resolved_query = resolved_query.replace(
                placeholder, _to_oracle_value(str(value))
            )
 
    logger.info(f"Pipeline Step {step.get('Step')} [ORACLE]: {resolved_query}")
 
    connection = OracleConnection(environment)
    result     = connection.fetchAllDict(resolved_query)
 
    if not result:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [ORACLE]: No records returned."
        )
 
    values = [row.get(output_key) for row in result if row.get(output_key) is not None]
    if not values:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [ORACLE]: "
            f"Field '{output_key}' not found in any result row."
        )
 
    logger.info(
        f"Pipeline Step {step.get('Step')} [ORACLE]: "
        f"{output_key} = {values if len(values) > 1 else values[0]} "
        f"({len(values)} value(s))"
    )
 
    context[output_key] = values if len(values) > 1 else values[0]
    return context
 
 
def _execute_pipeline_lookup_mongodb(step: dict, context: dict, filter_values: dict) -> dict:
    """
    Executes a MONGODB lookup step for the dynamic file pipeline —
    same role as the Oracle variant above but for MongoDB collections/views.
    Returns all matching OutputKey values (single value or list).
    """
    from .MongoDB import Collection
 
    db          = step.get("DB", "")
    query       = step.get("Query", "")
    environment = (step.get("Environment") or "").upper()
    output_key  = step.get("OutputKey", "")
 
    if not query or not output_key:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [MONGODB]: "
            f"Requires 'Query' and 'OutputKey'."
        )
 
    mongo_filter = {
        field: _to_mongo_value(str(value))
        for field, value in filter_values.items()
    }
 
    logger.info(
        f"Pipeline Step {step.get('Step')} [MONGODB]: "
        f"db.{query}.find({_json.dumps(mongo_filter, default=str)})"
    )
 
    collection = Collection(db, query, environment)
    documents  = list(collection.getData(mongo_filter))
 
    if not documents:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [MONGODB]: "
            f"No documents found in '{query}' with filter {mongo_filter}."
        )
 
    values = [doc.get(output_key) for doc in documents if doc.get(output_key) is not None]
    if not values:
        raise ValueError(
            f"Pipeline Step {step.get('Step')} [MONGODB]: "
            f"Field '{output_key}' not found in any document."
        )
 
    logger.info(
        f"Pipeline Step {step.get('Step')} [MONGODB]: "
        f"{output_key} = {values if len(values) > 1 else values[0]} "
        f"({len(values)} value(s))"
    )
 
    context[output_key] = values if len(values) > 1 else values[0]
    return context
 
 
def fetch_dynamic_pipeline(source_steps: list, filter_values: dict,
                           retry_attempts: int = 2,
                           retry_wait_minutes: float = 5) -> dict:
    """
    Generic two-phase dynamic file-location pipeline.
 
    Phase 1 — lookup steps (Type: MONGODB / ORACLE): resolve one or more
              dynamic location value(s) into context under the step's
              OutputKey. :field placeholders in the lookup query are
              substituted from `filter_values` (resolved DataFilters).
 
    Phase 2 — fetch step (Type: AUTO, terminal): reads InputKey from
              context, builds a Source dict per resolved location, fetches
              + parses each file via fetch_and_parse(), and combines them
              into one DataFrame — same combining behaviour as
              PickStrategy=ALL. Supports both single-file and
              multi-file dynamic-location patterns transparently.
 
    Args:
        source_steps  : List of step dicts from test case Source array
        filter_values : {field_name: resolved_value} from the test case's
                        DataFilters (SourceField side) — used to substitute
                        :placeholders in lookup-step queries
        retry_attempts, retry_wait_minutes : passed through to fetch_file()
 
    Returns:
        Same structure as parse_file() / fetch_and_parse():
        { "dataframe", "parse_errors", "schema_errors", "footer_errors",
          "row_count", "error_count", "files", "file_count" }
 
    Raises:
        ValueError if any lookup step fails, no terminal AUTO step is
        found, or InputKey is not resolved in context.
    """
    if not source_steps:
        raise ValueError("Pipeline: Source steps are empty — nothing to execute.")
 
    steps      = sorted(source_steps, key=lambda s: s.get("Step", 0))
    context    = {}
    fetch_step = None
 
    for step in steps:
        step_type = (step.get("Type") or "").upper()
 
        if step_type == "ORACLE":
            context = _execute_pipeline_lookup_oracle(step, context, filter_values)
        elif step_type == "MONGODB":
            context = _execute_pipeline_lookup_mongodb(step, context, filter_values)
        elif step_type == "AUTO":
            fetch_step = step          # terminal step — fetched after lookups complete
        else:
            raise ValueError(
                f"Pipeline Step {step.get('Step')}: Unknown Type '{step_type}'. "
                f"Supported lookup types: MONGODB, ORACLE. "
                f"Terminal fetch type: AUTO."
            )
 
    if fetch_step is None:
        raise ValueError(
            "Pipeline: No terminal fetch step found. "
            "Ensure one step has Type='AUTO'."
        )
 
    input_key = fetch_step.get("InputKey", "")
    locations = context.get(input_key)
    if locations is None:
        raise ValueError(
            f"Pipeline Step {fetch_step.get('Step')} [AUTO]: "
            f"InputKey '{input_key}' not found in context. "
            f"Check previous step OutputKey."
        )

    # Normalise to a list — transparently supports single-file (TC4-style)
    # and multi-file (TC5-style) dynamic-location patterns
    location_list = locations if isinstance(locations, list) else [locations]
 
    credential_key = fetch_step.get("CredentialKey", "")
    file_config    = fetch_step.get("FileConfig") or {}
    add_col        = fetch_step.get("AddSourceFileColumn", False)
 
    all_dfs           = []
    all_parse_errors  = []
    all_schema_errors = []
    all_footer_errors = []
    fetched_files     = []
 
    for location in location_list:
        per_file_source = {
            "Type"         : "AUTO",
            "Path"         : location,
            "CredentialKey": credential_key,
            "FileConfig"   : file_config
        }
        result = fetch_and_parse(
            per_file_source,
            retry_attempts=retry_attempts,
            retry_wait_minutes=retry_wait_minutes
        )
        df = result["dataframe"]
        if add_col and not df.empty:
            df["SourceFile"] = location
 
        all_dfs.append(df)
        all_parse_errors.extend(result["parse_errors"])
        all_schema_errors.extend(result["schema_errors"])
        all_footer_errors.extend(result["footer_errors"])
        fetched_files.extend(result.get("files", [location]))
 
    if all_dfs:
        if len(all_dfs) > 1:
            first_cols = set(all_dfs[0].columns)
            for i, df in enumerate(all_dfs[1:], 2):
                if set(df.columns) != first_cols:
                    raise ValueError(
                        f"Pipeline: Schema inconsistency across dynamically "
                        f"located files — file {i} has different columns: "
                        f"expected {sorted(first_cols, key=str)}, "
                        f"got {sorted(df.columns, key=str)}"
                    )
        combined_df = pd.concat(all_dfs, ignore_index=True)
    else:
        combined_df = pd.DataFrame()
 
    total_errors = (
        len(all_parse_errors) + len(all_schema_errors) + len(all_footer_errors)
    )
 
    logger.info(
        f"Pipeline: Combined {len(all_dfs)} dynamically located file(s) — "
        f"{len(combined_df)} total rows, {total_errors} total error(s)."
    )
 
    return {
        "dataframe"    : combined_df,
        "parse_errors" : all_parse_errors,
        "schema_errors": all_schema_errors,
        "footer_errors": all_footer_errors,
        "row_count"    : len(combined_df),
        "error_count"  : total_errors,
        "files"        : [f[0] for f in raw_files],
        "file_count"   : len(raw_files)		
		}


# ═══════════════════════════════════════════════════════════════════════════
# PDF extraction — used by PDF_Data validation type
# ═══════════════════════════════════════════════════════════════════════════
#
# All functions here are anchor-driven: the test case's CompareFields and
# TableExtraction config tell the extractor exactly where each value lives
# in the PDF, using surrounding text as anchors rather than coordinates.
#
# Anchor types (set per CompareField via AnchorType):
#   LEFT         — value immediately follows LeftAnchor text on same line
#   RIGHT        — value immediately precedes RightAnchor text on same line
#   BOTH         — value is sandwiched between LeftAnchor and RightAnchor
#
# Table extraction (via TableExtraction config block):
#   KeyColumn    — one column whose value identifies each row (the join key)
#   TableColumns — per-row columns: each data row produces one record
#   TotalColumns — bottom-row columns: same value appended to every record
# ═══════════════════════════════════════════════════════════════════════════

def _safe_numeric(value) -> float:
    """Converts a value to float for numeric DataFilter comparisons."""
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value or 0)))
    except (ValueError, TypeError):
        return 0.0


def _apply_record_filters(records: list, data_filters: list) -> list:
    """
    Applies DataFilters as Python-side predicates on a list of record dicts.
    Used for PDF (and other file sources) that have no SQL query interface.

    Field is looked up via TargetField first, then SourceField.
    Filter is skipped when DefaultValue is empty/None — consistent with
    how DB-side DataFilters behave when no runtime value is provided.

    Supported Operators:
      =  !=  <>  >  <  >=  <=  IN  NOT IN  LIKE  IS NULL  IS NOT NULL
    """
    filtered = list(records)
    for f in (data_filters or []):
        field    = (f.get("TargetField") or f.get("SourceField") or "").strip()
        operator = (f.get("Operator") or "=").upper().strip()
        value    = f.get("DefaultValue") or f.get("Value") or ""
        str_val  = str(value).strip()

        # Skip when no field or value is empty (pass-through behaviour)
        if not field or (str_val == "" and operator not in ("IS NULL", "IS NOT NULL")):
            continue

        before = len(filtered)
        try:
            if operator == "=":
                filtered = [r for r in filtered
                            if str(r.get(field, "")).strip() == str_val]
            elif operator in ("!=", "<>"):
                filtered = [r for r in filtered
                            if str(r.get(field, "")).strip() != str_val]
            elif operator == ">":
                filtered = [r for r in filtered
                            if _safe_numeric(r.get(field)) > _safe_numeric(value)]
            elif operator == "<":
                filtered = [r for r in filtered
                            if _safe_numeric(r.get(field)) < _safe_numeric(value)]
            elif operator == ">=":
                filtered = [r for r in filtered
                            if _safe_numeric(r.get(field)) >= _safe_numeric(value)]
            elif operator == "<=":
                filtered = [r for r in filtered
                            if _safe_numeric(r.get(field)) <= _safe_numeric(value)]
            elif operator == "IN":
                vals = {v.strip() for v in str_val.split(",")}
                filtered = [r for r in filtered
                            if str(r.get(field, "")).strip() in vals]
            elif operator == "NOT IN":
                vals = {v.strip() for v in str_val.split(",")}
                filtered = [r for r in filtered
                            if str(r.get(field, "")).strip() not in vals]
            elif operator == "LIKE":
                pat = re.escape(str_val).replace(r"\%", ".*").replace(r"\_", ".")
                filtered = [r for r in filtered
                            if re.fullmatch(pat, str(r.get(field, "")), re.IGNORECASE)]
            elif operator == "IS NULL":
                filtered = [r for r in filtered
                            if not str(r.get(field, "")).strip()]
            elif operator == "IS NOT NULL":
                filtered = [r for r in filtered
                            if str(r.get(field, "")).strip()]
            else:
                logger.warning(
                    f"PDF_parser: unknown DataFilter operator '{operator}' for "
                    f"'{field}' — skipped."
                )
        except Exception as e:
            logger.warning(f"PDF_parser: DataFilter on '{field}': {e} — skipped.")

        if len(filtered) != before:
            logger.info(
                f"PDF_parser: DataFilter '{field} {operator} {str_val!r}' — "
                f"{before} → {len(filtered)} record(s)."
            )

    return filtered


def _parse_pdf(content: bytes, file_config: dict,
               filename: str = "") -> tuple:
    """
    Full PDF extraction for the existing parse_file() / fetch_and_parse()
    pipeline. Once this returns a proper DataFrame, all standard validation
    types (SRC_TGT, Record_Count, Null_Check, Duplicate_Check etc.) work
    for PDF sources/targets without any special-casing in Data_Validation.py.

    Reads Target.FileConfig.Columns for extraction config:
      - Anchor columns (LEFT / RIGHT / BOTH) → header-level fields
      - "Table Column" entries              → per-row table values
      - "Table Column Total" entries        → bottom-row totals (same on all rows)

    After raw extraction, runs each record through _process_row — the same
    pipeline used for CSV/Excel — so Regex, Transform, Type, Precision,
    NullValues etc. all work exactly as in other file formats.

    DataFilters (from FileConfig._data_filters injected by Data_Validation.py)
    are applied as Python-side predicates after extraction since PDF has no
    SQL query interface.
    """
    columns      = file_config.get("Columns", []) or []
    null_values  = file_config.get("NullValues", []) or []
    data_filters = file_config.get("_data_filters", []) or []

    parse_errors  = []
    schema_errors = []
    footer_errors = []

    # ── Guard: non-PDF extension ──────────────────────────────────────────
    # If the file has a non-PDF extension (e.g. .xlsx accidentally configured
    # with Format=PDF), Aspose will either error immediately or, worse,
    # silently hang trying to parse binary content.  Warn early and bail out.
    file_ext = Path(filename).suffix.lower() if filename else ""
    if file_ext and file_ext not in {".pdf"}:
        err = (
            f"File_parser: '{filename}' has extension '{file_ext}' but "
            f"FileConfig.Format is PDF. Correct the FileName or Format."
        )
        logger.issue(err)
        return pd.DataFrame(), [{"RowNumber": 0, "Error": err, "RawContent": ""}], [], []

    if not columns:
        logger.warning(
            f"File_parser: '{filename}' — PDF FileConfig.Columns not defined. "
            f"Add anchor/table column config to Target.FileConfig.Columns."
        )
        return pd.DataFrame(), parse_errors, schema_errors, footer_errors

    # Phase 1 & 2: raw extraction (anchors + table rows).
    # extract_pdf_anchor_values opens the PDF via Aspose.PDF (_open_pdf)
    # which enforces a 60-second timeout for corrupt/huge PDFs.
    extracted   = extract_pdf_anchor_values(content, columns)
    raw_records = extracted.get("records", [])
    for err in extracted.get("extraction_errors", []):
        parse_errors.append({"RowNumber": 0, "Error": err, "RawContent": ""})

    # Apply _process_row — transforms, type coercion, Regex, Precision,
    # null normalisation — exactly as CSV/Excel does, reusing the same code.
    processed_records = []
    for i, rec in enumerate(raw_records, 1):
        processed, row_errors = _process_row(rec, columns, null_values)
        for err in row_errors:
            err["RowNumber"] = i
            parse_errors.append(err)
        if processed:
            processed_records.append(processed)

    # Apply DataFilters (Python-side, injected from Data_Validation._run_single)
    if data_filters:
        processed_records = _apply_record_filters(processed_records, data_filters)

    if not processed_records:
        logger.info(f"File_parser: '{filename}' — PDF parsed 0 row(s).")
        return pd.DataFrame(), parse_errors, schema_errors, footer_errors

    df = pd.DataFrame(processed_records)
    total_errors = len(parse_errors) + len(schema_errors)
    logger.info(
        f"File_parser: '{filename}' — PDF parsed {len(df)} row(s), "
        f"{total_errors} error(s)."
    )
    return df, parse_errors, schema_errors, footer_errors


# ─────────────────────────────────────────────────────────────────────────────
# Low-level anchor extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _anchor_extract_left(full_text: str, left_anchor: str,
                         occurrence: int = 1,
                         case_sensitive: bool = False) -> str | None:
    """
    Finds `left_anchor` in full_text and returns the value that follows it
    on the same line, up to two or more consecutive spaces or end-of-line.

    occurrence: 1-based — use 2 if the anchor appears multiple times and
                you need the second match.
    case_sensitive: False by default since anchors are layout labels.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.escape(left_anchor) + r"\s*(.*?)(?=\s{2,}|\n|$)"
    matches = list(re.finditer(pattern, full_text, flags | re.MULTILINE))
    if not matches or occurrence > len(matches):
        return None
    return matches[occurrence - 1].group(1).strip() or None


def _anchor_extract_right(full_text: str, right_anchor: str,
                          occurrence: int = 1,
                          case_sensitive: bool = False) -> str | None:
    """
    Finds `right_anchor` in full_text and returns the value that precedes it
    on the same line — everything from the last large whitespace gap (or
    beginning of line) up to the anchor.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    # Capture: start of line or 2+ spaces, then non-space content, then anchor
    pattern = r"(?:^|\s{2,})([\S][^\n]*?)\s*" + re.escape(right_anchor)
    matches = list(re.finditer(pattern, full_text, flags | re.MULTILINE))
    if not matches or occurrence > len(matches):
        return None
    return matches[occurrence - 1].group(1).strip() or None


def _anchor_extract_both(full_text: str, left_anchor: str, right_anchor: str,
                         occurrence: int = 1,
                         case_sensitive: bool = False) -> str | None:
    """
    Extracts text sandwiched between left_anchor and right_anchor on the
    same line. Most precise method — use when both anchors are on the same
    line as the value (e.g. "Claim ID: 12345  Date: 01/01/2026").
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = (re.escape(left_anchor) + r"\s*(.*?)\s*" +
               re.escape(right_anchor))
    matches = list(re.finditer(pattern, full_text, flags | re.DOTALL))
    if not matches or occurrence > len(matches):
        return None
    return matches[occurrence - 1].group(1).strip() or None


# ─────────────────────────────────────────────────────────────────────────────
# Table extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_column_index(header_row: list, header_text: str,
                       case_sensitive: bool = False) -> int | None:
    """
    Returns the 0-based index of the column whose header matches header_text.
    Returns None if not found.
    """
    for idx, cell in enumerate(header_row):
        cell_str = str(cell or "").strip()
        match = (cell_str == header_text if case_sensitive
                 else cell_str.lower() == header_text.lower())
        if match:
            return idx
    return None


def _is_total_row(row: list, key_col_idx: int,
                  total_row_key_value: str = "") -> bool:
    """
    Identifies whether a table row is the total/summary row.

    Detection priority:
      1. Explicit TotalRowKeyValue configured in TableExtraction → exact match
      2. Key column cell contains "total" / "grand total" / "sub total"
      3. Key column cell is blank/empty (common for totals rows)
    """
    if key_col_idx >= len(row):
        return False
    key_val = str(row[key_col_idx] or "").strip()

    if total_row_key_value:
        return key_val.lower() == total_row_key_value.lower()

    lower = key_val.lower()
    if any(t in lower for t in ("total", "grand total", "subtotal", "sub total")):
        return True
    if not key_val:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# PDF backend — Aspose.PDF
# ═══════════════════════════════════════════════════════════════════════════
#
# Exposes the same interface the extraction code expects, so _get_chart_region,
# _get_cell_semantic_line, _extract_table_records and extract_pdf_anchor_values
# work unchanged:
#
#   _open_pdf(bytes)         → _PdfDoc
#   _PdfDoc.pages            → [_PdfPage, ...]      (0-indexed list)
#   _PdfPage.extract_text()  → str
#   _PdfPage.extract_words() → [{'text','x0','x1','top','bottom'}, ...]
#   _PdfPage.extract_tables()→ [[[cell, ...], ...], ...]
#   _PdfPage.find_tables()   → [_TableObj, ...]     (.cells for bbox lookup)
#   _PdfPage.crop(bbox)      → _PdfPage restricted to (x0, top, x1, bottom)
#   _PdfPage.width/height/page_number
#
# Coordinate convention: Aspose uses PDF-native bottom-left origin. All
# coordinates exposed here are converted to top-left origin (y increases
# downward) to match what the extraction logic expects.
#
# Licensing: without a licence file Aspose runs in evaluation mode, which
# limits the number of pages processed and may inject watermarks. Set the
# licence path in Config/environment.ini to remove those limits:
#
#   [ASPOSE]
#   license_path = C:\licences\Aspose.PDF.Python.lic
# ═══════════════════════════════════════════════════════════════════════════

_ASPOSE_LICENCE_APPLIED = False


def _apply_aspose_licence():
    """
    Applies the Aspose licence once per process, if configured.
    Missing licence is not fatal — Aspose falls back to evaluation mode,
    which is usable for small documents but page-limited and watermarked.
    """
    global _ASPOSE_LICENCE_APPLIED
    if _ASPOSE_LICENCE_APPLIED:
        return
    _ASPOSE_LICENCE_APPLIED = True
    try:
        import aspose.pdf as ap
        from .Read_config import config_read
        lic_path = config_read().aspose_license_path()
        if lic_path and os.path.exists(lic_path):
            lic = ap.License()
            lic.set_license(lic_path)
            logger.info(f"PDF_parser: Aspose licence applied from '{lic_path}'.")
        else:
            logger.warning(
                "PDF_parser: Aspose licence not found — running in evaluation "
                "mode (page-limited). Add [ASPOSE] license_path to "
                "Config/environment.ini for full functionality."
            )
    except Exception as e:
        logger.warning(
            f"PDF_parser: could not apply Aspose licence ({e}) — "
            f"continuing in evaluation mode."
        )


def _open_pdf(content: bytes) -> "_PdfDoc":
    """
    Opens a PDF from raw bytes using Aspose.PDF.

    Wrapped in a daemon thread with a 60-second timeout so a corrupt,
    encrypted or non-PDF file cannot block the worker indefinitely.
    """
    import threading as _threading
    import aspose.pdf as ap

    _apply_aspose_licence()

    _TIMEOUT = 60
    _result  = [None]
    _error   = [None]

    def _do_open():
        try:
            _result[0] = ap.Document(io.BytesIO(content))
        except Exception as e:
            _error[0] = e

    t = _threading.Thread(target=_do_open, daemon=True)
    t.start()
    t.join(timeout=_TIMEOUT)

    if t.is_alive():
        raise ValueError(
            f"PDF open timed out after {_TIMEOUT}s — "
            f"file may be corrupt, encrypted, or not a valid PDF."
        )
    if _error[0]:
        raise _error[0]

    return _PdfDoc(_result[0])


class _TableObj:
    """Holds a flat, row-major list of (x0, top, x1, bottom) cell bboxes."""
    def __init__(self, cells_bboxes):
        self.cells = cells_bboxes


class _PdfPage:
    """
    Wraps an Aspose Page and exposes the interface the extraction code uses.

    `clip` (x0, top, x1, bottom) in top-left coords restricts every operation
    to that region, which is how crop() is implemented.
    """

    def __init__(self, ap_page, page_number: int, clip=None):
        self._page       = ap_page
        self.page_number = page_number
        self._clip       = clip
        rect             = ap_page.get_page_rect(True)
        self.width       = rect.width
        self.height      = rect.height

    # ── Coordinate helper ─────────────────────────────────────────────────

    def _to_tl(self, rect):
        """Aspose Rectangle (bottom-left origin) → (x0, top, x1, bottom)."""
        return (rect.llx,
                self.height - rect.ury,
                rect.urx,
                self.height - rect.lly)

    def _in_clip(self, bbox) -> bool:
        if not self._clip:
            return True
        x0, top, x1, bottom = bbox
        cx0, ctop, cx1, cbottom = self._clip
        # Overlap test rather than strict containment
        return not (x1 < cx0 or x0 > cx1 or bottom < ctop or top > cbottom)

    # ── Public API ────────────────────────────────────────────────────────

    def extract_text(self) -> str:
        """
        Full page text. When cropped, fragments are filtered by the clip
        region and reassembled in reading order (top→bottom, left→right).
        """
        import aspose.pdf.text as apt

        if not self._clip:
            absorber = apt.TextAbsorber()
            self._page.accept(absorber)
            return absorber.text or ""

        # Cropped: gather fragments, filter, sort into lines
        frags = self._fragments()
        if not frags:
            return ""
        frags.sort(key=lambda f: (round(f["top"], 1), f["x0"]))

        lines, current, cur_top = [], [], None
        for f in frags:
            if cur_top is None or abs(f["top"] - cur_top) <= 3:
                current.append(f)
                cur_top = f["top"] if cur_top is None else cur_top
            else:
                lines.append(" ".join(x["text"] for x in current))
                current, cur_top = [f], f["top"]
        if current:
            lines.append(" ".join(x["text"] for x in current))
        return "\n".join(lines)

    def _fragments(self) -> list:
        """All text fragments on the page as dicts, filtered by clip."""
        import aspose.pdf.text as apt

        absorber = apt.TextFragmentAbsorber()
        self._page.accept(absorber)

        out = []
        for frag in absorber.text_fragments:
            try:
                bbox = self._to_tl(frag.rectangle)
            except Exception:
                continue
            if not self._in_clip(bbox):
                continue
            text = (frag.text or "").strip()
            if not text:
                continue
            out.append({
                "text": text,
                "x0": bbox[0], "top": bbox[1],
                "x1": bbox[2], "bottom": bbox[3],
            })
        return out

    def extract_words(self) -> list:
        """
        Word-level boxes. Aspose fragments are usually phrase-level, so each
        fragment is split on whitespace and its width apportioned across the
        words by character count — accurate enough for the semantic-line
        grouping in _get_cell_semantic_line, which only relies on y-positions.
        """
        words = []
        for frag in self._fragments():
            parts = frag["text"].split()
            if not parts:
                continue
            if len(parts) == 1:
                words.append(dict(frag))
                continue
            total_chars = sum(len(p) for p in parts)
            span        = frag["x1"] - frag["x0"]
            cursor      = frag["x0"]
            for p in parts:
                w = span * (len(p) / total_chars) if total_chars else 0
                words.append({
                    "text": p,
                    "x0": cursor,
                    "x1": cursor + w,
                    "top": frag["top"],
                    "bottom": frag["bottom"],
                })
                cursor += w
        return words

    def _absorbed_tables(self) -> list:
        """Runs TableAbsorber and returns tables overlapping the clip region."""
        import aspose.pdf.text as apt

        absorber = apt.TableAbsorber()
        absorber.visit(self._page)

        tables = []
        for tbl in absorber.table_list:
            try:
                if self._clip and not self._in_clip(self._to_tl(tbl.rectangle)):
                    continue
            except Exception:
                pass
            tables.append(tbl)
        return tables

    @staticmethod
    def _cell_text(cell) -> str:
        """Concatenates all text fragments in an AbsorbedCell."""
        parts = []
        for frag in cell.text_fragments:
            t = (frag.text or "").strip()
            if t:
                parts.append(t)
        return "\n".join(parts)

    def extract_tables(self) -> list:
        """Returns [[row, ...], ...] — one 2D list per detected table."""
        result = []
        for tbl in self._absorbed_tables():
            rows = []
            for row in tbl.row_list:
                rows.append([self._cell_text(c) for c in row.cell_list])
            if rows:
                result.append(rows)
        return result

    def find_tables(self) -> list:
        """
        Returns _TableObj instances whose .cells is a flat, row-major list of
        (x0, top, x1, bottom) — consumed by the LineNumber cell-crop logic.
        """
        out = []
        for tbl in self._absorbed_tables():
            cells = []
            for row in tbl.row_list:
                for cell in row.cell_list:
                    try:
                        cells.append(self._to_tl(cell.rectangle))
                    except Exception:
                        cells.append((0.0, 0.0, 0.0, 0.0))
            out.append(_TableObj(cells))
        return out

    def crop(self, bbox) -> "_PdfPage":
        """Returns a new _PdfPage restricted to (x0, top, x1, bottom)."""
        return _PdfPage(self._page, self.page_number, clip=bbox)


class _PdfDoc:
    """Wraps an Aspose Document; .pages is a 0-indexed list of _PdfPage."""

    def __init__(self, ap_doc):
        self._doc  = ap_doc
        # Aspose page collection is 1-based
        self.pages = [
            _PdfPage(ap_doc.pages[i], i)
            for i in range(1, len(ap_doc.pages) + 1)
        ]

    def close(self):
        try:
            self._doc.dispose()
        except Exception:
            pass


def _get_chart_region(pdf, chart_number: str):


    """
    Locates the named chart section within the PDF and returns a cropped
    PDF page object restricted to that section's region, plus the
    text extracted from that region.

    Searches for a label matching "CHART <number>" or "CHART<number>"
    (case-insensitive). The region extends from that label down to the next
    "CHART" heading or end of page — whichever comes first.

    Args:
        pdf          : open PDF document object
        chart_number : value from the column's ChartNumber field, e.g. "1A", "2"

    Returns:
        (cropped_page, chart_text) — cropped PDF page and its text
        (None, "")                 — if chart label not found in any page
    """
    chart_label   = str(chart_number).upper()
    # Matches "CHART 1A", "CHART1A", "CHART 2", "CHART2" etc.
    this_pattern  = re.compile(
        r"CHART\s*" + re.escape(chart_label) + r"", re.IGNORECASE
    )
    other_pattern = re.compile(r"CHART\s*\w+", re.IGNORECASE)

    for page in pdf.pages:
        words = page.extract_words()

        # ── Locate the chart label on this page ───────────────────────────
        chart_top = None
        for i, w in enumerate(words):
            # Single token: "CHART1A"
            if this_pattern.search(w["text"]):
                chart_top = w["top"]
                break
            # Two tokens: "CHART" + "1A"
            if w["text"].upper() == "CHART" and i + 1 < len(words):
                combined = w["text"] + words[i + 1]["text"]
                if this_pattern.search(combined):
                    chart_top = w["top"]
                    break

        if chart_top is None:
            continue   # not on this page

        # ── Find where the NEXT chart section begins ───────────────────────
        next_chart_top = page.height   # default: end of page
        for i, w in enumerate(words):
            if w["top"] <= chart_top:
                continue   # still above or at current chart label
            if w["text"].upper() == "CHART" and i + 1 < len(words):
                next_combined = w["text"] + words[i + 1]["text"]
                # Different chart from the one we found
                if not this_pattern.search(next_combined):
                    next_chart_top = w["top"]
                    break

        # ── Crop and return ───────────────────────────────────────────────
        cropped = page.crop((0, chart_top, page.width, next_chart_top))
        text    = cropped.extract_text() or ""
        logger.info(
            f"PDF_parser: CHART {chart_number} region found — "
            f"y=[{chart_top:.0f}, {next_chart_top:.0f}], "
            f"{len(text):,} char(s)."
        )
        return cropped, text

    logger.warning(
        f"PDF_parser: CHART {chart_number} label not found in any page. "
        f"Searching full PDF instead."
    )
    return None, ""


def _get_cell_semantic_line(cell_text: str, line_number: int,
                             cell_words: list = None) -> str:
    """
    Extracts the Nth SEMANTIC line (1-based) from a multi-line table cell.

    The problem this solves: a table cell may have several logical data items
    stacked by \n in the template (e.g. Drug_Name, Claim_ID, Qty, Rx#).
    When any of those items is too long, the PDF renderer word-wraps it onto
    additional visual lines — so "line 2" in the template can appear at
    visual line 3 or 4 in the rendered PDF.

    This function uses y-coordinate analysis to merge word-wrapped visual
    lines back into their parent semantic lines, then returns the value at
    the requested logical position.

    Args:
        cell_text  : raw text of the cell from the PDF backend (may contain \n
                     for both logical and word-wrapped lines)
        line_number: 1-based logical line number from the template
        cell_words : list of word dicts with 'top', 'bottom', 'x0', 'text'
                     from the PDF page.  When provided enables coordinate-based
                     semantic grouping.  When None, falls back to \n splitting.

    Returns:
        Extracted text for the requested logical line, or "" if out of range.
    """
    if not cell_text:
        return ""

    if cell_words and len(cell_words) > 1:
        # ── Sort words by y-position then x-position ───────────────────────
        sorted_words = sorted(cell_words, key=lambda w: (round(w["top"], 1), w["x0"]))

        # ── Group into visual lines (same y within 3 pt tolerance) ────────
        visual_lines = [[sorted_words[0]]]
        for w in sorted_words[1:]:
            if abs(w["top"] - visual_lines[-1][0]["top"]) <= 3:
                visual_lines[-1].append(w)
            else:
                visual_lines.append([w])

        if len(visual_lines) == 1:
            line_text = " ".join(w["text"] for w in visual_lines[0])
            return line_text if line_number == 1 else ""

        # ── Compute y-gaps between consecutive visual lines ────────────────
        gaps = [
            visual_lines[i][0]["top"] - visual_lines[i - 1][-1]["bottom"]
            for i in range(1, len(visual_lines))
        ]
        # Median gap ≈ normal inter-field spacing.
        # Word-wrapped continuation lines have a gap < 60% of that.
        median_gap    = sorted(gaps)[len(gaps) // 2]
        wrap_threshold = median_gap * 0.6

        # ── Merge word-wrapped visual lines into semantic lines ────────────
        semantic_groups = [[visual_lines[0]]]
        for i, gap in enumerate(gaps):
            if gap < wrap_threshold:
                # Continuation of the previous semantic line
                semantic_groups[-1].append(visual_lines[i + 1])
            else:
                semantic_groups.append([visual_lines[i + 1]])

        # ── Convert each semantic group to a string ────────────────────────
        text_lines = [
            " ".join(
                " ".join(w["text"] for w in vl) for vl in sem
            ).strip()
            for sem in semantic_groups
        ]
    else:
        # ── Fallback: split by newline, skip blank lines ───────────────────
        text_lines = [
            ln.strip() for ln in (cell_text or "").split("\n") if ln.strip()
        ]

    logger.debug(
        f"PDF_parser: cell has {len(text_lines)} semantic line(s); "
        f"requesting line {line_number}."
    )

    if 1 <= line_number <= len(text_lines):
        return text_lines[line_number - 1]
    if text_lines:
        logger.warning(
            f"PDF_parser: LineNumber={line_number} out of range "
            f"(cell has {len(text_lines)} semantic line(s)); "
            f"returning last line."
        )
        return text_lines[-1]
    return ""


def _extract_table_records(pdf, table_cols: list, total_cols: list,
                           anchor_values: dict,
                           page_num=None, table_index: int = 1,
                           total_row_key_value: str = "",
                           chart_number: str = "") -> list:
    """
    Extracts one dict per detail row from a PDF table, using the column
    config from Target.FileConfig.Columns (AnchorType == "Table Column" and
    "Table Column Total").

    Args:
        pdf               : open PDF document object
        table_cols        : Columns entries with AnchorType == "Table Column"
                            Keys used: SourceName, ColumnName, Required,
                                       LineNumber (optional), ChartNumber (optional)
        total_cols        : Columns entries with AnchorType == "Table Column Total"
                            Keys used: SourceName, ColumnName, Required, ChartNumber
        anchor_values     : header-level fields already extracted (copied into
                            every output record unchanged)
        page_num          : 1-based page to search; None = all pages
        table_index       : 1-based index of the table on the page (default 1)
        total_row_key_value: explicit cell value marking the total row
        chart_number      : restrict table search to this named chart section
                            (e.g. "1A"). When set, crops the page to the chart
                            region before extracting tables.

    Returns list of record dicts — one per detail row (header rows and the
    total row are excluded).  Each record contains anchor_values + per-row
    table column values + total column values (same on every record).

    LineNumber support:
        When a Table Column entry has "LineNumber": N, the Nth logical/semantic
        line within that table cell is extracted. Uses coordinate-based word-wrap
        detection so the value is found even when earlier cell content wraps
        onto multiple visual lines (shifting later items down).
    """
    # ── Locate the table (optionally restricting to chart region) ─────────
    # Determine chart_number from the first column that declares one
    if not chart_number:
        for c in (table_cols + total_cols):
            if c.get("ChartNumber"):
                chart_number = str(c["ChartNumber"]).strip()
                break

    # If a chart region is requested, use the cropped page for table search
    chart_cropped = None
    if chart_number:
        chart_cropped, _ = _get_chart_region(pdf, chart_number)

    search_targets = []
    if chart_cropped is not None:
        search_targets = [chart_cropped]   # only within the chart's region
    elif page_num and 1 <= page_num <= len(pdf.pages):
        search_targets = [pdf.pages[page_num - 1]]
    else:
        search_targets = list(pdf.pages)

    raw_table = None
    table_page = None   # keep ref for word extraction (LineNumber support)
    for pg in search_targets:
        tables = pg.extract_tables()
        if tables:
            idx = min(table_index, len(tables)) - 1
            raw_table  = tables[idx]
            table_page = pg
            break

    if not raw_table or len(raw_table) < 2:
        logger.warning("PDF_parser: no table found or table has fewer than 2 rows.")
        return []

    header_row = raw_table[0]
    data_rows  = raw_table[1:]

    # ── Map ColumnName → column index (case-insensitive) ─────────────────
    def _resolve_col(col_name: str) -> int | None:
        idx = _find_column_index(header_row, col_name)
        if idx is None:
            logger.warning(
                f"PDF_parser: column '{col_name}' not found in table. "
                f"Headers: {[str(c or '').strip() for c in header_row]}"
            )
        return idx

    table_col_idxs = [(c, _resolve_col(c.get("ColumnName", ""))) for c in table_cols]
    total_col_idxs = [(c, _resolve_col(c.get("ColumnName", ""))) for c in total_cols]

    # Use the first column for total-row detection when no key column is defined
    first_col_idx = 0

    # ── Separate detail rows from the total row ───────────────────────────
    detail_rows   = []
    total_row_raw = None

    for row in data_rows:
        if _is_total_row(row, first_col_idx, total_row_key_value):
            total_row_raw = row
        else:
            # Skip blank rows (all cells empty)
            if all(not str(cell or "").strip() for cell in row):
                continue
            detail_rows.append(row)

    # ── Extract total column values (same on every output record) ─────────
    total_values = {}
    if total_row_raw is not None:
        for col_cfg, idx in total_col_idxs:
            sn = col_cfg.get("SourceName", "")
            if sn and idx is not None and idx < len(total_row_raw):
                total_values[sn] = str(total_row_raw[idx] or "").strip()
    elif total_cols:
        logger.warning("PDF_parser: 'Table Column Total' columns configured "
                       "but no total row detected in table.")

    # ── Build one record per detail row ───────────────────────────────────
    # Precompute per-column line numbers for LineNumber support
    table_col_lines = {c.get("SourceName", ""): c.get("LineNumber") for c in table_cols}

    records = []
    for row_idx_r, row in enumerate(detail_rows):
        record = dict(anchor_values)          # header fields same on every row

        for col_cfg, col_idx in table_col_idxs:
            sn          = col_cfg.get("SourceName", "")
            line_number = col_cfg.get("LineNumber")
            if not sn or col_idx is None or col_idx >= len(row):
                continue

            raw_cell = str(row[col_idx] or "").strip()

            if line_number and table_page is not None:
                # Semantic-line extraction using coordinate analysis.
                # We need words inside this specific cell's bounding box.
                # the backend does not directly expose per-cell word coords from
                # extract_tables(), so we find the cell bbox from the table object.
                try:
                    tbl_obj    = table_page.find_tables()
                    if tbl_obj:
                        tbl        = tbl_obj[min(table_index, len(tbl_obj)) - 1]
                        # tbl.cells is a list of (x0,top,x1,bottom) per cell, row-major
                        # row index: skip header row (row_idx_r+1 because detail_rows = data_rows[1:])
                        actual_row_i = row_idx_r + 1   # +1 for header
                        n_cols = len(header_row)
                        cell_bbox_idx = actual_row_i * n_cols + col_idx
                        if cell_bbox_idx < len(tbl.cells):
                            x0, top, x1, bottom = tbl.cells[cell_bbox_idx]
                            cell_region = table_page.crop((x0, top, x1, bottom))
                            cell_words  = cell_region.extract_words()
                            record[sn]  = _get_cell_semantic_line(
                                raw_cell, line_number, cell_words
                            )
                        else:
                            record[sn] = _get_cell_semantic_line(raw_cell, line_number)
                    else:
                        record[sn] = _get_cell_semantic_line(raw_cell, line_number)
                except Exception as _e:
                    logger.debug(f"PDF_parser: LineNumber fallback for '{sn}': {_e}")
                    record[sn] = _get_cell_semantic_line(raw_cell, line_number)
            else:
                record[sn] = raw_cell

        record.update(total_values)           # total fields same on every row
        records.append(record)

    logger.info(
        f"PDF_parser: extracted {len(records)} detail row(s), "
        f"{len(total_values)} total column value(s)."
    )
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_pdf_anchor_values(content: bytes,
                              columns: list) -> dict:
    """
    Main entry point for PDF_Data extraction. Called from Data_Validation.py.

    columns: list of dicts from Target.FileConfig.Columns. Each entry:
        SourceName      — output field name (used in JoinFields / CompareFields)
        AnchorType      — "LEFT" | "RIGHT" | "BOTH"
                          "Table Column" | "Table Column Total"
        LeftAnchor      — anchor text to the left of the value  (LEFT / BOTH)
        RightAnchor     — anchor text to the right of the value (RIGHT / BOTH)
        ColumnName      — table column header text (Table Column / Table Column Total)
        Required        — bool; missing required value is an extraction error
        Occurrence      — which match to use when anchor appears >1 time (default 1)
        CaseSensitive   — whether anchor search is case-sensitive (default false)

    Phase 1 — Anchor extraction:
        LEFT / RIGHT / BOTH columns → scans full PDF text, extracts header-level
        values that are the same across all table rows (member ID, month, etc.).

    Phase 2 — Table extraction (only when Table Column or Table Column Total
        columns are present):
        Locates the PDF table, extracts one record per detail row.  Every
        record contains the Phase-1 anchor values plus per-row table values
        plus total-row values.

    Returns:
        {
          "anchor_values"    : {SourceName: value, ...},   # Phase 1 output
          "records"          : [{SourceName: value, ...}], # one per detail row
          "extraction_errors": [str, ...]
        }
        When no table columns are configured, records = [anchor_values]
        (single-record comparison for documents without a detail table).
    """
    anchor_values     = {}
    extraction_errors = []

    # _open_pdf uses Aspose.PDF and enforces its own 60-second timeout
    try:
        pdf = _open_pdf(content)
    except Exception as e:
        logger.issue(f"PDF_parser: failed to open PDF — {e}")
        return {"anchor_values": {}, "records": [], "extraction_errors": [str(e)]}

    # Extract full text once — reused by anchor searches without ChartNumber
    pages_text = []
    for page in pdf.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception as e:
            pages_text.append("")
            extraction_errors.append(f"Page {page.page_number} text error: {e}")

    full_text = "\n".join(pages_text)
    logger.info(
        f"PDF_parser: {len(pdf.pages)} page(s), "
        f"{len(full_text):,} char(s) extracted for anchor search."
    )

    # Cache chart-region texts so each named chart is located only once
    # even when multiple columns reference the same ChartNumber.
    _chart_text_cache: dict = {}

    def _text_for_col(col: dict) -> str:
        """Returns chart-region text when ChartNumber is set, else full PDF text."""
        cn = str(col.get("ChartNumber") or "").strip()
        if not cn:
            return full_text
        if cn not in _chart_text_cache:
            _, chart_text = _get_chart_region(pdf, cn)
            _chart_text_cache[cn] = chart_text if chart_text else full_text
        return _chart_text_cache[cn]

    # Split columns by anchor type
    anchor_cols = []
    table_cols  = []
    total_cols  = []
    for col in (columns or []):
        at = (col.get("AnchorType") or "LEFT").strip().upper()
        if at in ("LEFT", "RIGHT", "BOTH"):
            anchor_cols.append(col)
        elif at == "TABLE COLUMN":
            table_cols.append(col)
        elif at == "TABLE COLUMN TOTAL":
            total_cols.append(col)
        else:
            extraction_errors.append(
                f"Unknown AnchorType '{col.get('AnchorType')}' "
                f"for SourceName '{col.get('SourceName', '')}'. "
                f"Supported: LEFT, RIGHT, BOTH, Table Column, Table Column Total."
            )

    # ── Phase 1: Anchor extraction ────────────────────────────────────────
    for col in anchor_cols:
        src_name     = col.get("SourceName", "")
        anchor_type  = (col.get("AnchorType") or "LEFT").strip().upper()
        left_anchor  = col.get("LeftAnchor",  "")
        right_anchor = col.get("RightAnchor", "")
        occurrence   = int(col.get("Occurrence", 1))
        case_sens    = bool(col.get("CaseSensitive", False))
        required     = col.get("Required", False)

        if not src_name:
            continue

        value = None
        try:
            search_text = _text_for_col(col)   # chart region or full PDF

            if anchor_type == "LEFT":
                if not left_anchor:
                    raise ValueError("AnchorType=LEFT requires LeftAnchor.")
                value = _anchor_extract_left(search_text, left_anchor,
                                             occurrence, case_sens)
            elif anchor_type == "RIGHT":
                if not right_anchor:
                    raise ValueError("AnchorType=RIGHT requires RightAnchor.")
                value = _anchor_extract_right(search_text, right_anchor,
                                              occurrence, case_sens)
            elif anchor_type == "BOTH":
                if not left_anchor or not right_anchor:
                    raise ValueError("AnchorType=BOTH requires LeftAnchor and RightAnchor.")
                value = _anchor_extract_both(search_text, left_anchor, right_anchor,
                                             occurrence, case_sens)
        except Exception as e:
            msg = f"'{src_name}' [{anchor_type}]: {e}"
            logger.issue(f"PDF_parser: {msg}")
            extraction_errors.append(msg)
            value = None

        anchor_values[src_name] = value

        if value is not None:
            logger.info(f"PDF_parser: '{src_name}' [{anchor_type}] → {value!r}")
        elif required:
            msg = f"Required field '{src_name}' [{anchor_type}] not found in PDF."
            logger.issue(f"PDF_parser: {msg}")
            extraction_errors.append(msg)
        else:
            logger.warning(f"PDF_parser: '{src_name}' [{anchor_type}] → not found.")

    # ── Phase 2: Table extraction ─────────────────────────────────────────
    records = []
    if table_cols or total_cols:
        try:
            # Infer chart_number from the first column that declares one
            _tbl_chart = ""
            for _c in (table_cols + total_cols):
                if _c.get("ChartNumber"):
                    _tbl_chart = str(_c["ChartNumber"]).strip()
                    break
            records = _extract_table_records(
                pdf, table_cols, total_cols, anchor_values,
                chart_number=_tbl_chart
            )
        except Exception as e:
            msg = f"Table extraction failed: {e}"
            logger.issue(f"PDF_parser: {msg}")
            extraction_errors.append(msg)
    else:
        # Document-level comparison only — one record per PDF
        records = [dict(anchor_values)]

    pdf.close()
    return {
        "anchor_values"    : anchor_values,
        "records"          : records,
        "extraction_errors": extraction_errors,
    }