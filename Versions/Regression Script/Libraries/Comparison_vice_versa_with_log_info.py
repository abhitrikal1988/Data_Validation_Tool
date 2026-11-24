import pdb
 
import re
import os
import pandas as pd
from datetime import datetime
from bson.codec_options import CodecOptions
from bson import DatetimeMS
import logging
from pymongo.cursor import Cursor
from Libraries.Regression_Log_Helper import append_log_entry
from collections import defaultdict
from .Read_config import config_read , Timezone
from pandas.api.types import (is_datetime64_any_dtype, is_datetime64tz_dtype, is_numeric_dtype)
from decimal import Decimal, ROUND_HALF_UP

cr= config_read()
 
logger = logging.getLogger()
 
CHUNK_SIZE = 100000
 
# ── Custom ISSUE level ──────────────────────────────────────────────────────
# Register here so it exists in subprocess workers (which don't run Log_Config)
_ISSUE_LEVEL = 35
if not hasattr(logging, "ISSUE"):
    logging.addLevelName(_ISSUE_LEVEL, "ISSUE")
    logging.ISSUE = _ISSUE_LEVEL
if not hasattr(logging.Logger, "issue"):
    def _issue(self, message, *args, **kwargs):
        if self.isEnabledFor(_ISSUE_LEVEL):
            self._log(_ISSUE_LEVEL, message, args, **kwargs)
    logging.Logger.issue = _issue
mismatch=cr.mismatch_count("No_Of_Records_Display")
missing=cr.missing_count("No_Of_Records_Display")
 
def is_cursor_empty(data,name=""):
    try:
        # logger.debug(f"Checking if data '{name}' is empty.")
        if data is None:                        
            return True
 
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
        logger.issue(f"Error while checking if cursor '{name}' is empty: {e}")
        return True  # Treat as empty on failure
 
 
def safe_to_datetime(series: pd.Series) -> pd.Series:
    """Try specific formats first, then fall back to flexible parsing.
    Drop timezone info so both sides compare apples-to-apples."""
    if series.empty:
        return series
    # Try common formats first (fast path, no warnings)
    known_formats = ["%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"]
    for fmt in known_formats:
        try:
            dt = pd.to_datetime(series, format=fmt, errors="coerce", utc=False)
            # If this format worked for a meaningful portion, accept it
            if dt.notna().sum() >= max(1, int(0.5 * len(series))):
                return _strip_tz(dt)
        except Exception:
            continue
    # Fall back to flexible parsing (coerce errors); convert to str first to avoid dtype surprises
    dt = pd.to_datetime(series.astype(str), errors="coerce", utc=False)
    return _strip_tz(dt)
 
 
def _strip_tz(dt_series: pd.Series) -> pd.Series:
    """Remove timezone info when present (works for tz-aware results)."""
    try:
        # If tz-aware, this will work:
        return dt_series.dt.tz_localize(None)
    except Exception:
        # If tz-naive already, return as-is
        return dt_series
 
 
def looks_like_datetime_col(name: str) -> bool:
    n = (name or "").lower()
    return "date" in n or "time" in n or "timestamp" in n or "dt" in n
 
 
def _coerce_key_pair_to_common_dtype(src_col: pd.Series, tgt_col: pd.Series) -> tuple[pd.Series, pd.Series]:
    """
    Given two join columns, coerce both sides to a comparable dtype:
      - datetime if either column is datetime-like or the names look like dates
      - numeric if either side is numeric
      - otherwise normalized strings
    """
    # Detect name hints
    src_name = (src_col.name or "")
    tgt_name = (tgt_col.name or "")
    # Datetime path
    if (is_datetime64_any_dtype(src_col) or
            is_datetime64_any_dtype(tgt_col) or
            looks_like_datetime_col(src_name) or
            looks_like_datetime_col(tgt_name)):
        return safe_to_datetime(src_col), safe_to_datetime(tgt_col)
    # Numeric path
 
    if is_numeric_dtype(src_col) or is_numeric_dtype(tgt_col):
        return (pd.to_numeric(src_col, errors="coerce"), pd.to_numeric(tgt_col, errors="coerce"))
    # Fallback: normalized strings (strip; keep case as-is to avoid unexpected folding)
    return (src_col.astype("string").str.strip(), tgt_col.astype("string").str.strip())
 
 
def normalize_join_keys(src_df: pd.DataFrame, tgt_df: pd.DataFrame, src_keys, tgt_keys):
    """Normalize join keys to a common comparable dtype.
    Strategy: try datetime on any key that looks like a date/time; otherwise cast to string."""
    for s_key, t_key in zip(src_keys, tgt_keys):
        if s_key in src_df.columns and t_key in tgt_df.columns:
            if looks_like_datetime_col(s_key) or looks_like_datetime_col(t_key):
                src_df[s_key] = safe_to_datetime(src_df[s_key])
                tgt_df[t_key] = safe_to_datetime(tgt_df[t_key])
                # Convert both to string ISO date to make merge deterministic
                src_df[s_key] = src_df[s_key].dt.strftime("%Y-%m-%d %H:%M:%S").fillna(pd.NA)
                tgt_df[t_key] = tgt_df[t_key].dt.strftime("%Y-%m-%d %H:%M:%S").fillna(pd.NA)
            else:
                # Cast to pandas 'string' dtype for robust merge across sources
                src_df[s_key] = src_df[s_key].astype("string")
                tgt_df[t_key] = tgt_df[t_key].astype("string")
 
 
def normalize_compare_columns(src_df: pd.DataFrame, tgt_df_renamed: pd.DataFrame, field_map):
    """Align compare columns types across frames BEFORE merge (so row-wise compare is clean)."""
    for f in field_map:
        s_field = f["SourceField"]
        t_field = f["TargetField"]
        if s_field in src_df.columns and s_field in tgt_df_renamed.columns:
            # Heuristic: if the column name looks like datetime OR if either side is datetime-like, normalize as datetime
            if looks_like_datetime_col(s_field) or _either_datetime_like(src_df[s_field], tgt_df_renamed[s_field]):
                s_dt = safe_to_datetime(src_df[s_field])
                t_dt = safe_to_datetime(tgt_df_renamed[s_field])
                src_df[s_field] = s_dt
                tgt_df_renamed[s_field] = t_dt
            else:
                # Try numeric alignment; if both sides mostly parse as numbers, keep them numeric
                s_num = pd.to_numeric(src_df[s_field], errors="coerce")
                t_num = pd.to_numeric(tgt_df_renamed[s_field], errors="coerce")
                if s_num.notna().sum() + t_num.notna().sum() >= max(1, int(0.6 * (len(s_num) + len(t_num)))):
                    src_df[s_field] = s_num
                    tgt_df_renamed[s_field] = t_num
                else:
                    # Fallback to string
                    src_df[s_field] = src_df[s_field].astype("string")
                    tgt_df_renamed[s_field] = tgt_df_renamed[s_field].astype("string")
 
 
def _either_datetime_like(s: pd.Series, t: pd.Series) -> bool:
    return pd.api.types.is_datetime64_any_dtype(s) or pd.api.types.is_datetime64_any_dtype(t)
 
def normalize_join_key_types(src_df: pd.DataFrame, tgt_df: pd.DataFrame, key_names: list[str]) -> None:
    """
    Mutates src_df and tgt_df in-place so that each join key in key_names
    has the same dtype on both sides (datetime, numeric, or normalized string).
    """
    for k in key_names:
        if k not in src_df.columns or k not in tgt_df.columns:
            # Let your existing missing-key logging handle this earlier
            continue
        src_df[k], tgt_df[k] = _coerce_key_pair_to_common_dtype(src_df[k], tgt_df[k])
 
def normalize_join_keys_like(ref_df: pd.DataFrame, df_to_fix: pd.DataFrame, key_names: list[str]):
    """
    Normalize df_to_fix join keys to EXACTLY match ref_df join key dtypes.
    Handles datetime (including tz), numeric, and string.
    """
    for k in key_names:
        if k not in ref_df.columns or k not in df_to_fix.columns:
            continue
 
        ref_col = ref_df[k]
 
        # Datetime (tz-aware or naive)
        if pd.api.types.is_datetime64_any_dtype(ref_col):
            df_to_fix[k] = safe_to_datetime(df_to_fix[k])
 
            # If ref is tz-aware, localize missing_df to same tz
            if hasattr(ref_col.dt, "tz") and ref_col.dt.tz is not None:
                df_to_fix[k] = df_to_fix[k].dt.tz_localize(
                    ref_col.dt.tz, nonexistent="NaT", ambiguous="NaT"
                )
 
        # Numeric
        elif pd.api.types.is_numeric_dtype(ref_col):
            df_to_fix[k] = pd.to_numeric(df_to_fix[k], errors="coerce")
 
        # String fallback
        else:
            df_to_fix[k] = df_to_fix[k].astype("string").str.strip()
 
 
# ── Subprocess helpers ─────────────────────────────────────────────────────
 
class _LogAccumulator(logging.Handler):
    """Captures log records in a subprocess for pickling back to the main process."""
    def __init__(self):
        super().__init__()
        self.records = []
 
    def emit(self, record):
        self.records.append({
            "levelno": record.levelno,
            "msg":     record.getMessage(),
        })
 
def _apply_field_options_mask(s_series: "pd.Series",
                               t_series: "pd.Series",
                               field_def: dict) -> "pd.Series":
    """
    Builds a boolean mismatch mask for a single compare field,
    applying per-field options: Precision, Tolerance, DateOnly, CaseSensitive.

    Called only when at least one option is set — otherwise the existing
    fast vectorized path runs instead (zero performance impact on standard fields).

    Returns:
        pd.Series of bool — True where values differ (mismatch).
    """
    precision = field_def.get("Precision")
    tolerance = float(field_def.get("Tolerance") or 0.0)
    date_only = field_def.get("DateOnly", False)
    case_sens = field_def.get("CaseSensitive", True)

    def _compare_single(s_val, t_val) -> bool:
        """Returns True if values are DIFFERENT (mismatch)."""
        # Both null → match
        s_none = s_val is None or (isinstance(s_val, float) and pd.isna(s_val))
        t_none = t_val is None or (isinstance(t_val, float) and pd.isna(t_val))

        if s_none and t_none:
            return False   # both null — match
        if s_none != t_none:
            return True    # one null — mismatch

        # Date-only — strip time component
        if date_only:
            try:
                from datetime import datetime as _dt
                if hasattr(s_val, 'date'):
                    s_val = s_val.date()
                elif isinstance(s_val, str):
                    s_val = _dt.fromisoformat(str(s_val).split("T")[0]).date()
                if hasattr(t_val, 'date'):
                    t_val = t_val.date()
                elif isinstance(t_val, str):
                    t_val = _dt.fromisoformat(str(t_val).split("T")[0]).date()
                return s_val != t_val
            except Exception:
                pass  # fall through to string comparison

        # Numeric with precision/tolerance
        if precision is not None or tolerance != 0.0:
            try:
                s_dec = Decimal(str(s_val).strip().replace(",", ""))
                t_dec = Decimal(str(t_val).strip().replace(",", ""))
                if precision is not None:
                    factor = Decimal(10) ** -int(precision)
                    s_dec  = s_dec.quantize(factor, rounding=ROUND_HALF_UP)
                    t_dec  = t_dec.quantize(factor, rounding=ROUND_HALF_UP)
                diff = abs(s_dec - t_dec)
                return diff > Decimal(str(tolerance))
            except Exception:
                pass  # fall through to string comparison

        # Case-insensitive string comparison
        if not case_sens:
            return str(s_val).strip().upper() != str(t_val).strip().upper()

        # Default exact match
        return str(s_val).strip() != str(t_val).strip()

    # Apply row-level comparison — returns boolean Series
    return pd.Series(
        [_compare_single(s, t) for s, t in zip(s_series, t_series)],
        index=s_series.index
    )


def compare_data_in_process(source_data, target_data, sourceType, targetType,
                            join_fields, compare_fields, testCaseNumber,
                            testDescription, viceversa=False,
                            case_sensitive_join=True):
    """
    Module-level picklable wrapper for ProcessPoolExecutor.
 
    Runs compare_data in an isolated subprocess so comparisons use separate
    CPU cores rather than competing for the GIL.  All arguments must be
    picklable (plain lists / dicts — no cursors, no threading objects).

    case_sensitive_join : applies when JoinFields="ALL" (full-row compare) —
                          read from the test case's FileConfig.CaseSensitiveJoin.
                          Has no effect on explicit JoinFields entries, which
                          use their own per-field "CaseSensitive" key instead.
 
    Returns (status, log_doc_dict, log_records):
        status       — bool (True = differences found)
        log_doc_dict — {"Log", "Result", "MismatchFields", "MissingFields"}
        log_records  — list of {"levelno", "msg"} dicts to replay in main process
    """
    accumulator = _LogAccumulator()
    root = logging.getLogger()
    root.handlers = []
    root.addHandler(accumulator)
    root.setLevel(logging.DEBUG)
 
    log_doc = {
        "Log": [], "Result": "Pass",
        "MismatchFields": [], "MissingFields": []
    }
    try:
        status = compare_data(
            source_data, target_data, sourceType, targetType,
            join_fields, compare_fields, testCaseNumber, testDescription,
            viceversa=viceversa, log_doc=log_doc,
            case_sensitive_join=case_sensitive_join
        )
    except Exception as e:
        status = True
        log_doc["Result"] = "Fail"
        accumulator.records.append({
            "levelno": logging.ERROR,
            "msg":     f"[TC{testCaseNumber}] compare_data_in_process error: {e}"
        })
    finally:
        root.removeHandler(accumulator)
 
    return status, log_doc, accumulator.records
 
 
def compare_data(source_data, target_data, sourceType, targetType,join_fields, compare_fields,testCaseNumber,testDescription,viceversa=False,log_doc=None,case_sensitive_join=True):
    # logger.info("Starting data comparison between source and target.")
    try:
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
 
        def values_differ(a, b):
            """Safe != that never raises on pd.NA."""
            if pd.isna(a) and pd.isna(b):
                return False
            if pd.isna(a) or pd.isna(b):
                return True
            return a != b
 
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
 
            def _resolver(df, requested_names):
                """
                For each requested name, find the actual column in df.
                Matching is: exact -> case-insensitive -> ignore spaces/underscores.
                Returns (resolved_names, missing_name_or_None)
                """
                cols = list(df.columns)
                if not cols:
                    return [], requested_names[0] if requested_names else None
                exact = {c: c for c in cols}
                lower = {c.lower(): c for c in cols}
                loose = {c.lower().replace("_", "").replace(" ", ""): c for c in cols}
                resolved = []
                for name in requested_names:
                    if name in exact:
                        resolved.append(name)
                        continue
                    ln = name.lower()
                    if ln in lower:
                        resolved.append(lower[ln])
                        continue
 
                    ln_loose = ln.replace("_", "").replace(" ", "")
                    if ln_loose in loose:
                        resolved.append(loose[ln_loose])
                        continue
                    return None, name  # missing
                return resolved, None
 
            def _get_merged_column(row, base_name, side):
                """
                side = 'src' or 'tgt'
                Returns correct value whether suffix exists or not
                """
                suffixed = f"{base_name}_{side}"
                if suffixed in row:
                    return row[suffixed]
                if base_name in row:
                    return row[base_name]
                return pd.NA
            # resolve key names on each side
            src_keys_resolved, miss_src = _resolver(src_df, src_keys)
            if miss_src:
                msg = f"[{direction}] Join key '{miss_src}' not found in SOURCE columns: {list(src_df.columns)}"
                logger.issue(msg)
                if log_doc:
                    append_log_entry(log_doc, "Error", direction, msg)
                return True  # treat as failure
            tgt_keys_resolved, miss_tgt = _resolver(tgt_df, tgt_keys)
            if miss_tgt:
                msg = f"[{direction}] Join key '{miss_tgt}' not found in TARGET columns: {list(tgt_df.columns)}"
                logger.issue(msg)
                if log_doc:
                    append_log_entry(log_doc, "Error", direction, msg)
                return True
 
            # Rename target join columns to match the *resolved source* names
            # so both sides share the same key names for the merge.
            rename_map = {tgt: src for src, tgt in zip(src_keys_resolved, tgt_keys_resolved)}
            # .copy() is mandatory: pandas .rename() is a shallow copy, so
            # in-place mutations applied to tgt_df_renamed (dtype coercion in
            # normalize_join_key_types, normalize_compare_columns) bleed back
            # into the outer tgt_df. Without this the TGT DataFrame is corrupted
            # by the forward pass and arrives empty in the reverse pass.
            tgt_df_renamed = tgt_df.rename(columns=rename_map).copy()
 
            # Coerce join-key dtypes to be comparable (use RESOLVED names)
            normalize_join_key_types(src_df, tgt_df_renamed, src_keys_resolved)

            # Case-insensitive join key normalisation
            for jf, src_key in zip(join_fields, src_keys_resolved):
                if not jf.get("CaseSensitive", True):
                    if src_key in src_df.columns:
                        src_df[src_key] = src_df[src_key].astype(str).str.upper()
                    if src_key in tgt_df_renamed.columns:
                        tgt_df_renamed[src_key] = tgt_df_renamed[src_key].astype(str).str.upper()
 
            # Normalize compare columns BEFORE merge
            normalize_compare_columns(src_df, tgt_df_renamed, field_map)
 
            # Sort & merge using RESOLVED key list
            src_df = sort_dataframe(src_df, src_keys_resolved)
            tgt_df_renamed = sort_dataframe(tgt_df_renamed, src_keys_resolved)
 
            merged = pd.merge(src_df, tgt_df_renamed, on=src_keys_resolved, how='outer',
                              suffixes=('_src', '_tgt'), indicator=True)
 
            # ── Vectorized split — no full-DataFrame Python loop ──────
            both_df      = merged[merged["_merge"] == "both"].reset_index(drop=True)
            left_only_df = merged[merged["_merge"] == "left_only"].reset_index(drop=True)
 
            differences_found  = False
            difference_count   = 0
            mismatch_log_count = 0
            missing_keys_in_tgt = set()
 
            # ── Missing records ───────────────────────────────────────
            missing_count = len(left_only_df)
            if missing_count > 0:
                differences_found = True
 
                # Build viceversa set in one C-level operation (no Python loop)
                if direction == "SRC->TGT" and not left_only_df.empty:
                    missing_keys_in_tgt = set(
                        map(tuple, left_only_df[src_keys_resolved].values.tolist())
                    )
 
                # Log up to configured limit only
                log_label = "Missing in Source" if direction == "TGT->SRC" else "Missing in Target"
                for _, row in left_only_df.head(int(missing)).iterrows():
                    join_key_vals = {k: row.get(k) for k in src_keys_resolved}
                    msg = f"[{direction}] {log_label}: {join_key_vals}"
                    logger.issue(msg)
                    if log_doc:
                        append_log_entry(log_doc, "Error", direction, msg)
                        if "MissingFields" not in log_doc:
                            log_doc["MissingFields"] = []
                        for k in join_key_vals:
                            if k not in log_doc["MissingFields"]:
                                log_doc["MissingFields"].append(k)
 
            # ── Mismatch detection — vectorized per field ─────────────
            # normalize_compare_columns() already normalised dtypes before
            # the merge, so per-value try_parse_date / _normalize_pair_values
            # are redundant here.  A column-level boolean mask replaces the
            # 16k-row Python loop entirely.
            if not both_df.empty:
                for f in field_map:
                    s_field    = f["SourceField"]
                    t_field    = f["TargetField"]
                    precision  = f.get("Precision")
                    tolerance  = f.get("Tolerance", 0.0)
                    date_only  = f.get("DateOnly", False)
                    case_sens  = f.get("CaseSensitive", True)

                    # Resolve actual column names after merge suffixing
                    s_col = f"{s_field}_src" if f"{s_field}_src" in both_df.columns else s_field
                    t_col = f"{t_field}_tgt" if f"{t_field}_tgt" in both_df.columns else t_field

                    if s_col not in both_df.columns or t_col not in both_df.columns:
                        continue

                    s_series = both_df[s_col]
                    t_series = both_df[t_col]

                    # ── Per-field options path ────────────────────────────────
                    has_options = (
                        precision  is not None or
                        float(tolerance or 0) != 0.0 or
                        date_only or
                        not case_sens
                    )

                    if has_options:
                        # Row-level comparison needed for per-field options
                        mismatch_mask = _apply_field_options_mask(
                            s_series, t_series, f
                        )
                    else:
                        # Existing fast vectorized path — unchanged
                        both_na       = s_series.isna() & t_series.isna()
                        mismatch_mask = (
                            (s_series != t_series) |
                            (s_series.isna() ^ t_series.isna())
                        ) & ~both_na

                    field_diff_count = int(mismatch_mask.sum())
                    if field_diff_count == 0:
                        continue

                    difference_count += field_diff_count
                    differences_found = True

                    # Iterate only the mismatch rows, up to the configured limit
                    for _, row in both_df[mismatch_mask].iterrows():
                        if mismatch_log_count >= int(mismatch):
                            break
                        join_key_vals = {k: row.get(k) for k in src_keys_resolved}
                        s_val = row.get(s_col, pd.NA)
                        t_val = row.get(t_col, pd.NA)
                        msg   = f"Mismatch at {join_key_vals}: {s_field}={s_val} != {t_field}={t_val}"
                        logger.issue(f"[{direction}] {msg}")
                        if log_doc:
                            append_log_entry(log_doc, "Error", direction, msg)
                            if "MismatchFields" not in log_doc:
                                log_doc["MismatchFields"] = []
                            if s_field not in log_doc["MismatchFields"]:
                                log_doc["MismatchFields"].append(s_field)
                        mismatch_log_count += 1
 
            append_log_entry(log_doc, "Info", direction, f"Total Mismatches: {difference_count}")
            if difference_count > 0: 
                logger.issue(f"Total Mismatches: {difference_count}")
            append_log_entry(log_doc, "Info", direction, f"Total Missing Records: {missing_count}")
            if missing_count > 0:
                logger.issue(f"Total Missing Records: {missing_count}")
            return differences_found, missing_keys_in_tgt
 
        # Main forward comparison
        src_df = pd.DataFrame(source_data)
        tgt_df = pd.DataFrame(target_data)

        # Expand JoinFields="ALL" — use all source columns as composite key.
        # CaseSensitive carried from FileConfig.CaseSensitiveJoin so the
        # composite-key comparison honours case sensitivity the same way
        # an explicit JoinFields entry would via its own "CaseSensitive" key.
        if join_fields == "ALL" or join_fields == ["ALL"]:
            _exclude = {"SourceFile", "_chunk_seq", "_row_seq"}
            join_fields = [
                {"SourceField": col, "TargetField": col, "CaseSensitive": case_sensitive_join}
                for col in src_df.columns
                if col not in _exclude
            ]
            compare_fields = []  # ALL mode — join covers everything, no separate compare

        result_forward,missing_records_tgt =run_comparison(src_df, tgt_df,sourceType, targetType, join_fields, compare_fields, direction="SRC->TGT")
 
        if viceversa and missing_records_tgt:
            src_keys = [j["SourceField"] for j in join_fields]
            if all(k in src_df.columns for k in src_keys):
                missing_df = pd.DataFrame(list(missing_records_tgt), columns=src_keys)
                normalize_join_keys_like(src_df, missing_df, src_keys)

                # Correct anti-join: keep only SRC rows whose composite join
                # key is NOT in the missing set. The original code used
                # how='outer' which added NaN-padded ghost rows for any key in
                # missing_df not present in src_df — those survived the
                # '_merge != both' filter and corrupted the reverse-pass target,
                # causing UNC_PATH to report 0 records.
                try:
                    if len(src_keys) == 1:
                        key = src_keys[0]
                        missing_vals = set(missing_df[key].astype(str))
                        mask = ~src_df[key].astype(str).isin(missing_vals)
                    else:
                        missing_tuples = set(
                            map(tuple, missing_df[src_keys].astype(str).values.tolist())
                        )
                        mask = ~src_df[src_keys].astype(str).apply(
                            lambda r: tuple(r) in missing_tuples, axis=1
                        )
                    src_df = src_df[mask].reset_index(drop=True)
                except Exception as _exc:
                    logger.warning(
                        f"Vice-versa pre-filter failed ({_exc}); "
                        f"falling back to outer-merge method."
                    )
                    src_df = src_df.merge(
                        missing_df, on=src_keys, how="outer", indicator=True
                    )
                    src_df = src_df[src_df["_merge"] != "both"].drop(columns=["_merge"])

                logger.info(f"Filtered {len(missing_records_tgt)} records missing in TGT before reverse check.")
 
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
            result_reverse,_ =run_comparison(tgt_df, src_df, targetType,sourceType,reversed_joins, reversed_fields, direction="TGT->SRC")
        logger.info("Data comparison process completed.")
        final_result = result_forward or result_reverse
        if not final_result:
            logger.info(f"[TestCase {testCaseNumber}] Test Result: PASS")
        else:
            logger.issue(f"[TestCase {testCaseNumber}] Test Result: FAIL")
 
 
        return final_result
 
    except Exception as e:
        logger.exception(f"[Testcase{testCaseNumber}] Error during compare data:{e}")
        if log_doc:
            append_log_entry(log_doc,"Error","Compare Data",f"Exception:{str(e)}")
        return True
 
 
# ---------- Value-level normalization helpers ----------
 
def _normalize_pair_values(s_val, t_val, colname):
    """Normalize two scalar values to a comparable form (datetime → iso string, number → float, else → string)."""
    # If looks like datetime by name OR either is Timestamp-like, treat as datetime
    if looks_like_datetime_col(colname) or _value_is_datetime_like(s_val) or _value_is_datetime_like(t_val):
        s = _to_datetime_scalar(s_val)
        t = _to_datetime_scalar(t_val)
        # Compare as ISO string (second precision) so tz and dtype differences vanish
        s_iso = s.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(s) else pd.NA
        t_iso = t.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(t) else pd.NA
        return s_iso, t_iso
    # Try numeric
    s_num = _to_numeric_scalar(s_val)
    t_num = _to_numeric_scalar(t_val)
    if s_num is not None or t_num is not None:
        return s_num, t_num
    # Fallback to string (including None)
    s_str = None if s_val is None else str(s_val)
    t_str = None if t_val is None else str(t_val)
    return s_str, t_str
 
 
def _value_is_datetime_like(v):
    return isinstance(v, (pd.Timestamp, datetime))
 
 
def _to_datetime_scalar(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return pd.NaT
 
    if isinstance(v, pd.Timestamp):
        # strip tz for consistent comparison/printing
        try:
            return v.tz_localize(None)
 
        except Exception:
            return v
    try:
        dt = pd.to_datetime(v, errors="coerce", utc=False)
        return _strip_tz(dt) if isinstance(dt, pd.Timestamp) else dt
    except Exception:
        return pd.NaT
 
def _to_numeric_scalar(v):
    try:
 
        # Treat empty strings/None as None (so pd.isna works upstream)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        n = pd.to_numeric([v], errors="coerce")[0]
        return None if pd.isna(n) else float(n)
    except Exception:
        return None
 
 
def inapproved_validation(source_data, approved_values, check_field, testCaseNumber, testDescription, log_doc=None):
    try:
        logger.info(f"[TC {testCaseNumber}] {testDescription}")
        # Normalise to DataFrame regardless of input type
        if isinstance(source_data, Cursor):
            src_data = pd.DataFrame(list(source_data))
        elif isinstance(source_data, list):
            src_data = pd.DataFrame(source_data)
        elif isinstance(source_data, pd.DataFrame):
            src_data = source_data
        else:
            logger.warning(f"[TC {testCaseNumber}] Unknown data type: {type(source_data)}. Marking as FAIL.")
            return True
        # Empty = nothing unapproved = PASS
        if src_data.empty:
            logger.info(f"[TC {testCaseNumber}] PASS: Source returned 0 records — nothing unapproved found.")
            if log_doc is not None:
                log_doc["Result"] = "Pass"
            return False
        filed_name = check_field[0]['SourceField']
        if filed_name not in src_data.columns:
            logger.warning(f"[TC {testCaseNumber}] Column '{filed_name}' not found. Available: {list(src_data.columns)}")
            if log_doc is not None:
                log_doc["Result"] = "Fail"
            return True
        values = src_data[filed_name].dropna()
        converted_approved = pd.Series(approved_values).astype(values.dtype)
        invalid_values = values[~values.isin(converted_approved)]
        if invalid_values.empty:
            logger.info(f"[TC {testCaseNumber}] PASS: All values are within approved list.")
            append_log_entry(log_doc, "Info", "InApprovedValue",
                             f"Approved Values: '{approved_values}' — all source values are approved.")
            return False
        else:
            logger.issue(f"[TC {testCaseNumber}] FAIL: Unapproved values found: '{invalid_values.tolist()}'")
            append_log_entry(log_doc, "Error", "InApprovedValue",
                             f"Unapproved values found: '{invalid_values.tolist()}'")
            return True
    except Exception as e:
        logger.exception(f"Error in approved values validation for TC {testCaseNumber}: {e}")
        return False
 
 
def run_row_count_validation(source_data, target_data,approvedValues,source_data1, target_data1, testCaseNumber, testDescription,log_doc=None):
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
 
        source_count= 0 if source_count is None else source_count
        target_count =0 if target_count is None else target_count
 
        # --- Log Sample DataFrames if provided ---
        def _log_sample_data(label, df_like):
            try:
                if df_like is not None:
                    df = pd.DataFrame(df_like)
                    if not df.empty:
                        # sample = df.head(10).to_dict(orient="records")
                        # logger.info(f"Sample {label} Data (showing up to 10 rows):")
                        # for row in sample:
                        #     logger.info(f"{label} Row: {row}")
                        # if log_doc:
                        #     append_log_entry(
                        #         log_doc, "Info", f"{label}Sample",
                        #         f"{label} Data (up to 10 rows): {sample}"
                        #     )
                        pass
                    else:
                        logger.info(f"{label} DataFrame is empty.")
            except Exception as e:
                logger.warning(f"Failed to log sample {label} data: {e}")
 
 
        if not target_count and source_count == approvedValues:
            logger.info(f"Source Count: {source_count}| Approved Count: {approvedValues}")
            logger.info("Records Count Validation is completed.")
            logger.info(f"[TC {testCaseNumber}] PASS: Row counts match.")
            if log_doc:
                append_log_entry(log_doc, "Info", "RowCount",
                                 f"Source Count: {source_count}, Approved Count: {approvedValues}")
            return False
        elif not target_count and source_count != approvedValues:
            logger.info(f"Source Count: {source_count}| Approved Count: {approvedValues}")
            logger.info("Records Count Validation is completed.")
            logger.issue(f"[TC {testCaseNumber}] Fail: Row count Not matched.")
            if log_doc:
                append_log_entry(log_doc, "Error", "RowCount",
                                 f"Source Count: {source_count},  Approved Count: {approvedValues}")
                _log_sample_data("Source", source_data1)
                _log_sample_data("Target",target_data1)
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
 
 

# ─────────────────────────────────────────────────────────────────────────────
 
# Shared helpers — reused by Null_Check and future keywords
 
# ─────────────────────────────────────────────────────────────────────────────
 
def _safe_cursor_raw(source_data, test_case_number) -> pd.DataFrame:
    """
    Last resort fallback — iterates the cursor document by document.
    Any document that causes a date decode error is skipped and counted.
    """
    docs    = []
    skipped = 0
    try:
        cursor = source_data.clone()
        while True:
            try:
                doc = next(cursor)
                docs.append(doc)
            except StopIteration:
                break
            except Exception:
                skipped += 1
                continue
    except Exception as e:
        logger.warning(f"TestCase {test_case_number}: Raw fallback cursor error — {e}.")
    if skipped:
        logger.warning(
            f"TestCase {test_case_number}: Skipped {skipped} document(s) "
            f"with invalid dates during raw fallback."
        )
    df = pd.DataFrame(docs)
    logger.info(f"TestCase {test_case_number}: Raw fallback loaded {len(df)} records.")
    return df

def _to_dataframe(source_data, test_case_number) -> pd.DataFrame:
    """
    Converts incoming source data to a pandas DataFrame.
    Simple version without early exit.
    Used by Duplicate_Check and other keywords.
    For Null_Check use _to_dataframe_chunked which supports early exit.
    """
    if isinstance(source_data, Cursor):
        chunks = []
        chunk  = []
        try:
            for doc in source_data:
                chunk.append(doc)
                if len(chunk) == CHUNK_SIZE:
                    chunks.append(pd.DataFrame(chunk))
                    chunk = []
            if chunk:
                chunks.append(pd.DataFrame(chunk))
        except Exception as e:
            if "date value out of range" in str(e):
                logger.warning(
                    f"TestCase {test_case_number}: Invalid BSON date detected — "
                    f"switching to safe record-by-record iteration."
                )
                return _safe_cursor_raw(source_data, test_case_number)
            logger.warning(f"TestCase {test_case_number}: Cursor error — {e}.")
        if not chunks:
            return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True)
        logger.info(
            f"TestCase {test_case_number}: Loaded {len(df)} records "
            f"in {len(chunks)} chunk(s)."
        )
        return df
    elif isinstance(source_data, list):
        return pd.DataFrame(source_data)
    elif isinstance(source_data, pd.DataFrame):
        return source_data.reset_index(drop=True)
    else:
        logger.warning(
            f"TestCase {test_case_number}: Unknown source data type — {type(source_data)}."
        )
        return pd.DataFrame()
		
def _null_mask(df: pd.DataFrame, col: str) -> pd.Series:
    """
    Returns a boolean mask of null/empty/null-string values in a column.
    Handles:
      - Python None / NaN
      - Empty string ""
      - String "null", "NULL", "Null", "none", "None" (case insensitive)
      - String "na", "n/a", "nan" (case insensitive)
    """
    str_col = df[col].astype(str).str.strip().str.lower()
    return (
        df[col].isnull() |
        (str_col == "") |
        (str_col == "null") |
        (str_col == "none") |
        (str_col == "na") |
        (str_col == "n/a") |
        (str_col == "nan")
    )

def _resolve_result_fields(result_fields, df, fallback_field) -> list:
    """
    Resolves ResultFields to columns that exist in the DataFrame.
    Case-insensitive matching — handles Oracle UPPERCASE columns.
    Falls back to first available column if nothing matches.
    """
    if isinstance(result_fields, str):
        result_fields = [result_fields]
    result_fields = [f for f in (result_fields or []) if f]
    # Case-insensitive column map
    col_map = {c.lower(): c for c in df.columns}
    cols = [col_map[f.lower()] for f in result_fields if f.lower() in col_map]
    if not cols:
        if fallback_field and fallback_field in df.columns:
            return [fallback_field]
        # Try case-insensitive fallback
        if fallback_field and fallback_field.lower() in col_map:
            return [col_map[fallback_field.lower()]]
        # Last resort — first available column
        return [df.columns[0]] if len(df.columns) > 0 else []
    return cols

def _format_sample(df, cols_to_show, max_records) -> tuple:
    """
    Builds two formatted strings from a filtered DataFrame:
      - sample_lines : newline separated — for process logger
      - sample_flat  : comma separated single line — for MongoDB log detail
    """
    sample_records = (
        df[cols_to_show]
        .head(int(max_records))
        .to_dict(orient="records")
    )
    sample_lines = "\n".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    sample_flat = ", ".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    return sample_lines, sample_flat

# ─────────────────────────────────────────────────────────────────────────────
# Null_Check keyword
# ─────────────────────────────────────────────────────────────────────────────

 
def run_null_check(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                   sourceQueryType, check_fields,
                   test_case_number, test_description, log_doc,
                   data_filters=None) -> bool:
    from Libraries.Regression_Log_Helper import append_log_entry
    from Libraries.data_fetcher import get_null_data
    try:
        # ── Flatten SourceField list into individual field entries ────────────
        all_check_fields = []
        for field_def in check_fields:
            src           = field_def.get("SourceField", "")
            result_fields = field_def.get("ResultFields", [])
            if isinstance(src, list):
                for s in src:
                    if s:
                        all_check_fields.append({
                            "SourceField" : s,
                            "ResultFields": result_fields
                        })
            elif src:
                all_check_fields.append({
                    "SourceField" : src,
                    "ResultFields": result_fields
                })
        append_log_entry(log_doc, "Info", "Null_Check",
                         f"Checking fields: "
                         f"{[f.get('SourceField') for f in all_check_fields]}")
        logger.info(
            f"TestCase {test_case_number}: Null_Check started — "
            f"pushing null filter to {sourceType} per field"
        )
        # ── Fetch per field — each query independently capped ─────────────────
        field_results = get_null_data(
            sourceType, sourceDB, sourceQuery,
            sourceEnvKey, sourceQueryType,
            all_check_fields, data_filters
        )
        failed = False
        # ── Process each field result independently ───────────────────────────
        for field_def in all_check_fields:
            field         = field_def.get("SourceField", "")
            result_fields = field_def.get("ResultFields", [])
            if isinstance(result_fields, str):
                result_fields = [result_fields]
            field_data = field_results.get(field)
            if field_data is None:
                msg = f"Field '{field}': Failed to fetch data — skipping."
                logger.warning(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Warning", "Null_Check", msg)
                continue
            # ── Convert to DataFrame ──────────────────────────────────────────
            df = _to_dataframe(field_data, test_case_number)
            if df.empty:
                msg = f"Field '{field}': No nulls found."
                logger.info(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Info", "Null_Check", msg)
                continue
            # ── Flatten nested field if needed ────────────────────────────────
            if "." in field and field not in df.columns:
                top_key = field.split(".")[0]
                if top_key in df.columns:
                    try:
                        df[field] = df[top_key].apply(
                            lambda val, f=field, tk=top_key: (
                                _extract_nested_value({tk: val}, f)
                                if val is not None else None
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            f"TestCase {test_case_number}: "
                            f"Could not flatten '{field}' — {e}."
                        )
            if field not in df.columns:
                msg = f"Field '{field}' not found in returned data — skipping."
                logger.warning(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Warning", "Null_Check", msg)
                continue
            # ── Null mask and report ──────────────────────────────────────────
            null_mask_series = _null_mask(df, field)
            null_count       = int(null_mask_series.sum())
            if null_count == 0:
                msg = f"Field '{field}': No nulls found."
                logger.info(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Info", "Null_Check", msg)
            else:
                failed       = True
                null_df      = df[null_mask_series].reset_index(drop=True)
                cols_to_show = _resolve_result_fields(result_fields, null_df, field)
                sample_lines, sample_flat = _format_sample(
                    null_df, cols_to_show, missing
                )
                logger.issue(
                    f"TestCase {test_case_number}: Field '{field}': "
                    f"{null_count} null(s) found. "
                    f"First {min(null_count, int(missing))} records:\n"
                    f"{sample_lines}\n"
                )
                append_log_entry(
                    log_doc, "Error", "Null_Check",
                    f"Field '{field}': {null_count} null(s) found. "
                    f"First {min(null_count, int(missing))} records: {sample_flat}"
                )
                log_doc.setdefault("MismatchFields", []).append(field)
        log_doc["Result"] = "Fail" if failed else "Pass"
        return failed
    except Exception as e:
        logger.exception(
            f"TestCase {test_case_number}: Exception in run_null_check: {e}"
        )
        append_log_entry(log_doc, "Error", "Null_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
        return True

 
def _extract_nested_value(row: dict, field_path: str):
    """
    Extracts a display value from a row dict using dot notation.
    Handles flat, nested, and array fields.
    Returns scalar, or first element of list if single-item array.
    """
    parts = field_path.split(".")
    val   = row
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list):
            val = [
                item.get(part) if isinstance(item, dict) else item
                for item in val
            ]
        else:
            return None
    if isinstance(val, list):
        return val[0] if len(val) == 1 else val
    return val

def _format_duplicate_sample(records, result_fields, max_groups) -> tuple:
    """
    Formats sample records for logging and MongoDB storage.
    For Oracle  : records is a list of dicts — already flat rows.
    For MongoDB : records is a list of aggregation group dicts
                  each with { _id, count, docs: [...] }.
    Returns:
        (sample_lines, sample_flat)
        sample_lines : newline separated — for process logger
        sample_flat  : comma separated single line — for MongoDB log detail
    """
    sample_records = []
    for rec in records[:int(max_groups)]:
        row = {}
        for rf in result_fields:
            display_key  = rf.split(".")[-1]
            row[display_key] = _extract_nested_value(rec, rf)
        sample_records.append(row)
    sample_lines = "\n".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    sample_flat = ", ".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    return sample_lines, sample_flat

def _format_mongo_duplicate_sample(dup_groups, result_fields, max_groups) -> tuple:
    """
    Formats MongoDB aggregation pipeline results.
    Each group has { _id, count, docs: [...] }.
    Shows all occurrences within each group together.
    Returns:
        (sample_lines, sample_flat, total_dup_vals, total_dup_recs)
    """
    sample_records  = []
    total_dup_vals  = len(dup_groups)
    total_dup_recs  = sum(g.get("count", 0) for g in dup_groups)
    groups_shown    = 0
    for group in dup_groups:
        if groups_shown >= int(max_groups):
            break
        docs  = group.get("docs", [])
        count = group.get("count", 0)
        for doc in docs:
            row = {}
            for rf in result_fields:
                top_key      = rf.split(".")[0]
                display_key  = rf.split(".")[-1]
                # doc has top-level keys — extract nested value from there
                row[display_key] = _extract_nested_value(doc, rf) if "." in rf else doc.get(rf)
            row["_dup_count"] = count
            sample_records.append(row)
        groups_shown += 1
    sample_lines = "\n".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    sample_flat = ", ".join(
        ", ".join(f"{k}: {v}" for k, v in rec.items())
        for rec in sample_records
    )
    return sample_lines, sample_flat, total_dup_vals, total_dup_recs

 

def run_duplicate_check(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                        sourceQueryType, check_fields,
                        test_case_number, test_description, log_doc,
                        data_filters=None) -> bool:

 
    from Libraries.Regression_Log_Helper import append_log_entry
    from Libraries.data_fetcher import get_duplicate_data
    try:
 
        append_log_entry(log_doc, "Info", "Duplicate_Check",
                         f"Checking fields: "
                         f"{[f.get('SourceField') for f in check_fields]}")
        logger.info(
            f"TestCase {test_case_number}: Duplicate_Check started — "
            f"pushing duplicate detection to {sourceType} per field"
        )
        failed = False
 
        for field_def in check_fields:
            # ── Normalise SourceField and ResultFields ────────────────────────
 
            src_field     = field_def.get("SourceField", "")
 
            result_fields = field_def.get("ResultFields", [])
            if isinstance(src_field, str):
                src_fields = [src_field] if src_field else []
            else:
                src_fields = [f for f in src_field if f]
            if isinstance(result_fields, str):
                result_fields = [result_fields] if result_fields else []
 
            for sf in src_fields:
                if sf not in result_fields:
                    result_fields = [sf] + result_fields
            field_label = ", ".join(src_fields)
            if not src_fields:
                continue
            # ── Fetch sample + count for this field ───────────────────────────
 
            field_results = get_duplicate_data(
                sourceType, sourceDB, sourceQuery,
                sourceEnvKey, sourceQueryType,
                [field_def],
                data_filters
            )
            field_data = field_results.get(field_label)
            if field_data is None:
                msg = f"Field '{field_label}': Failed to fetch duplicate data — skipping."
                logger.warning(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Warning", "Duplicate_Check", msg)
                continue
            sample_data = field_data.get("sample", [])
            total_count = field_data.get("total_count", 0)
            total_recs  = field_data.get("total_recs", 0)
            capped      = field_data.get("capped", False)
            count_label = f"{total_count}+" if capped else str(total_count)
            if not sample_data or total_count == 0:
                msg = f"Field '{field_label}': No duplicates found."
                logger.info(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Info", "Duplicate_Check", msg)
                continue
            failed = True
            # ── Oracle — flat list of duplicate rows ──────────────────────────
 
            if sourceType and sourceType.upper() == "ORACLE":
                df = _to_dataframe(sample_data, test_case_number)
 
                sample_lines, sample_flat = _format_duplicate_sample(
                    df.to_dict(orient="records"),
                    result_fields,
                    missing
                )
                logger.issue(
                    f"TestCase {test_case_number}: Field '{field_label}': "
                    f"{count_label} duplicate value(s) found across "
                    f"{total_recs} record(s). "
                    f"First {min(total_recs, int(missing))} records:\n"
                    f"{sample_lines}\n"
                )
                append_log_entry(
                    log_doc, "Error", "Duplicate_Check",
                    f"Field '{field_label}': {count_label} duplicate value(s) "
                    f"across {total_recs} record(s). "
                    f"Sample: {sample_flat}"
                )
            # ── MongoDB — aggregation pipeline groups ─────────────────────────
 
            elif sourceType and sourceType.upper() == "MONGODB":
                sample_lines, sample_flat, _, _ = _format_mongo_duplicate_sample(
                    sample_data, result_fields, missing
                )
                logger.issue(
                    f"TestCase {test_case_number}: Field '{field_label}': "
                    f"{count_label} duplicate value(s) found across "
                    f"{total_recs} record(s). "
                    f"First {min(total_count, int(missing))} duplicate group(s):\n"
                    f"{sample_lines}\n"
                )
 
                append_log_entry(
                    log_doc, "Error", "Duplicate_Check",
                    f"Field '{field_label}': {count_label} duplicate value(s) "
                    f"across {total_recs} record(s). "
                    f"Sample: {sample_flat}"
                )
            log_doc.setdefault("MismatchFields", []).append(field_label)
        log_doc["Result"] = "Fail" if failed else "Pass"
 
        return failed
    except Exception as e:
 
        logger.exception(
            f"TestCase {test_case_number}: Exception in run_duplicate_check: {e}"
        )
 
        append_log_entry(log_doc, "Error", "Duplicate_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
 
        return True

 
def run_format_check(sourceType, sourceDB, sourceQuery, sourceEnvKey,
                     sourceQueryType, check_fields,
                     test_case_number, test_description, log_doc,
                     data_filters=None) -> bool:
    """
    Validates field values against format rules — REGEX, DATE_FORMAT,
    NUMERIC_RANGE, LENGTH.
    Format validation pushed directly to Oracle/MongoDB via get_format_data().
    Only sample records (up to missing count) come across the wire.
    Exact total count fetched via separate COUNT query only when failures found.
    Log order — for each field:
      1. Field + format type being checked
      2. Query used (from get_format_data log)
      3. Pass/Fail result with sample records immediately after
    Args:
        sourceType       : ORACLE or MONGODB
        sourceDB         : Database / schema name
        sourceQuery      : Table, view, or collection name
        sourceEnvKey     : Environment key
        sourceQueryType  : VIEW, TABLE, Collection etc.
        check_fields     : List of field dicts. Each:
                           { SourceField, FormatType, Pattern, Value, ResultFields }
        test_case_number : Test case number — used for logging.
        test_description : Test case description — used for logging.
        log_doc          : Log document dict updated in place via append_log_entry().
        data_filters     : Resolved DataFilters — Oracle WHERE string or MongoDB dict.
    Returns:
        True  = FAIL
        False = PASS
        None = SKIP
    """
    from Libraries.Regression_Log_Helper import append_log_entry
    from Libraries.data_fetcher import get_format_data
    try:
        append_log_entry(log_doc, "Info", "Format_Check",
                         f"Checking fields: {[f.get('SourceField') for f in check_fields]}")
        logger.info(
            f"TestCase {test_case_number}: Format_Check started — "
            f"pushing format filter to {sourceType}"
        )
        # ── Push format validation to DB — one field at a time ────────────────
        # Process each field individually so query and result are logged together
        failed = False
        for field_def in check_fields:
            field         = field_def.get("SourceField", "")
            format_type   = field_def.get("FormatType", "")
            result_fields = field_def.get("ResultFields", [])
            if isinstance(result_fields, str):
                result_fields = [result_fields]
            # Always include source field — case-insensitive check
            if field and field.lower() not in [rf.lower() for rf in result_fields]:
                result_fields = [field] + result_fields
            # ── Log which field is being checked ──────────────────────────────
            logger.info(
                f"TestCase {test_case_number}: "
                f"Checking field '{field}' [{format_type}] ..."
            )
            # ── Fetch format failures for this field only ─────────────────────
            # Query is logged inside get_format_data — appears right here
            field_results = get_format_data(
                sourceType, sourceDB, sourceQuery,
                sourceEnvKey, sourceQueryType,
                [field_def],     # pass single field_def at a time
                data_filters
            )
            field_data = field_results.get(field)
            if field_data is None:
                msg = f"Field '{field}' [{format_type}]: Failed to fetch data — skipping."
                logger.warning(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Warning", "Format_Check", msg)
                continue
            # ── Extract sample and total count ────────────────────────────────
            sample_data = field_data.get("sample", [])
            total_count = field_data.get("total_count", 0)
            if not sample_data or total_count == 0:
                # ── PASS — log immediately after query ────────────────────────
                msg = f"Field '{field}' [{format_type}]: All records pass."
                logger.info(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Info", "Format_Check", msg)
                continue
            # ── FAIL — log results immediately after query ────────────────────
            failed = True
            # Convert sample to DataFrame
            df = _to_dataframe(sample_data, test_case_number)
            # Flatten nested fields
            if "." in field and field not in df.columns:
                top_key = field.split(".")[0]
                if top_key in df.columns:
                    try:
                        df[field] = df[top_key].apply(
                            lambda val: _extract_nested_value(
                                {top_key: val}, field
                            ) if val is not None else None
                        )
                    except Exception as e:
                        logger.warning(
                            f"TestCase {test_case_number}: "
                            f"Could not flatten '{field}' — {e}."
                        )
            # Case-insensitive column resolution
            cols_to_show = _resolve_result_fields(result_fields, df, field)
            sample_lines, sample_flat = _format_sample(df, cols_to_show, missing)
            # ── Log result immediately after query log ────────────────────────
            logger.issue(
                f"TestCase {test_case_number}: Field '{field}' [{format_type}]: "
                f"{total_count} record(s) fail format validation. "
                f"First {min(total_count, int(missing))} records:\n"
                f"{sample_lines}\n"
            )
            append_log_entry(
                log_doc, "Error", "Format_Check",
                f"Field '{field}' [{format_type}]: {total_count} record(s) fail. "
                f"First {min(total_count, int(missing))} records: {sample_flat}"
            )
            log_doc.setdefault("MismatchFields", []).append(field)
        log_doc["Result"] = "Fail" if failed else "Pass"
        return failed
    except Exception as e:
        logger.exception(
            f"TestCase {test_case_number}: Exception in run_format_check: {e}"
        )
        append_log_entry(log_doc, "Error", "Format_Check", f"Exception: {e}")
        log_doc["Result"] = "Fail"
        return True