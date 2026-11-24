"""
Mismatch_Report.py
==================
Writes every failed record of a SRC_TGT comparison to an Excel workbook, so
nothing is hidden behind the few sample lines shown in the log.

    <dir>\\Failed_Data_Log\\<Module>\\<Module>_TC<n>_<yyyymmdd_hhmmss>.xlsx

    Summary           test case, sides, run time; one line per field with the
                      number of mismatches and its most common value patterns
                      (each linked to its tab)
    <one tab / field> key columns · Source value · Target value · Issue
    Missing records   key columns · Issue (Missing in target / in source)

Settings, Config/environment.ini:

    [Mismatch_File]
    Enabled = Yes
    dir = \\\\server\\share\\...\\Log\\
    MaxRowsPerField = 100000        (optional; no limit when absent)

Keys and values are written as text: Excel keeps only 15 significant digits,
so a 16-digit claim id written as a number would lose its last digit, and
0.0 / 0 are shown exactly as they were compared.
"""

import logging
import os
import re
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

EXCEL_MAX_ROWS = 1_048_576          # per sheet, header included
TOP_PATTERNS = 5                    # patterns per field in the log / summary
NULL_TEXT = "(null)"


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def read_settings() -> dict:
    """{"enabled", "dir", "max_rows"} from [Mismatch_File]; disabled when absent."""
    from .Read_config import config_read
    cr = config_read()

    def get(section, key, default=None):
        try:
            return cr.get_conf_data(section, key)
        except KeyError:
            return default

    enabled = str(get("Mismatch_File", "Enabled", "No")).strip().lower() in ("yes", "y", "true", "1", "on")
    base = (get("Mismatch_File", "dir") or get("Log_Dir", "dir") or "").strip()
    max_rows = get("Mismatch_File", "MaxRowsPerField")
    try:
        max_rows = int(str(max_rows).replace(",", "").strip()) if max_rows not in (None, "") else None
        if max_rows is not None and max_rows <= 0:
            max_rows = None
    except ValueError:
        logger.warning(f"[Mismatch_File] MaxRowsPerField '{max_rows}' is not a number — no limit used.")
        max_rows = None
    return {"enabled": enabled and bool(base), "dir": base, "max_rows": max_rows}


# ─────────────────────────────────────────────────────────────────────────────
# Collecting
# ─────────────────────────────────────────────────────────────────────────────

def cell_text(value) -> str:
    """How a value is shown in the workbook and in pattern lines."""
    try:
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            return NULL_TEXT
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat(sep=" ")
    return str(value)


class MismatchCollector:
    """Gathers mismatches per field and missing records during compare_data."""

    def __init__(self, keep_rows=True):
        self.keep_rows = keep_rows      # False: patterns only, no rows kept (file disabled)
        self.key_labels = None          # forward-direction key column names
        self.fields = []                # [{"source", "target", "frame", "total", "patterns"}]
        self.missing = []               # DataFrames with key columns + Issue

    @property
    def has_data(self) -> bool:
        return self.keep_rows and (bool(self.fields) or any(len(m) for m in self.missing))

    def add_field(self, source_field, target_field, keys_df, s_values, t_values):
        """
        keys_df: DataFrame of the key columns for the mismatched rows;
        s_values / t_values: the compared values (same row order).
        Returns the field's patterns [(count, source_text, target_text)].
        """
        s_text = pd.Series(list(s_values)).map(cell_text)
        t_text = pd.Series(list(t_values)).map(cell_text)
        counts = (pd.DataFrame({"s": s_text, "t": t_text})
                  .value_counts(sort=True).reset_index(name="n"))
        patterns = [(int(r.n), r.s, r.t) for r in counts.itertuples(index=False)]
        if not self.keep_rows:
            return patterns
        frame = keys_df.reset_index(drop=True).copy()
        frame.columns = list(self.key_labels or frame.columns)
        for col in frame.columns:
            frame[col] = frame[col].map(cell_text)
        issue = pd.Series("Value differs", index=s_text.index)
        issue[(s_text == NULL_TEXT) & (t_text != NULL_TEXT)] = "Null in source"
        issue[(t_text == NULL_TEXT) & (s_text != NULL_TEXT)] = "Null in target"
        frame["Source value"] = s_text.values
        frame["Target value"] = t_text.values
        frame["Issue"] = issue.values
        self.fields.append({"source": source_field, "target": target_field,
                            "frame": frame, "total": len(frame), "patterns": patterns})
        return patterns

    def add_missing(self, keys_df, issue):
        if not self.keep_rows or keys_df is None or not len(keys_df):
            return
        frame = keys_df.reset_index(drop=True).copy()
        if self.key_labels and len(self.key_labels) == len(frame.columns):
            frame.columns = list(self.key_labels)
        for col in frame.columns:
            frame[col] = frame[col].map(cell_text)
        frame["Issue"] = issue
        self.missing.append(frame)


def pattern_lines(patterns, total, limit=TOP_PATTERNS) -> list:
    lines = [f"   {n:,} × {s} != {t}" for n, s, t in patterns[:limit]]
    if len(patterns) > limit:
        rest = total - sum(n for n, _, _ in patterns[:limit])
        lines.append(f"   … {len(patterns) - limit} more pattern(s), {rest:,} row(s)")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip()).strip("_") or "Module"


def _sheet_name(base: str, used: set) -> str:
    name = re.sub(r"[\[\]:*?/\\']", "_", str(base or "Field")).strip() or "Field"
    name = name[:31]
    candidate, i = name, 2
    while candidate.lower() in used or candidate.lower() in ("summary", "missing records"):
        suffix = f" ({i})"
        candidate = name[:31 - len(suffix)] + suffix
        i += 1
    used.add(candidate.lower())
    return candidate


def output_path(base_dir: str, module: str, test_case_number) -> str:
    folder = os.path.join(base_dir, "Failed_Data_Log", _safe_name(module))
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{_safe_name(module)}_TC{test_case_number}_{stamp}"
    path, i = os.path.join(folder, stem + ".xlsx"), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{i}.xlsx")
        i += 1
    return path


def write_workbook(collector: MismatchCollector, path: str, info: dict, max_rows=None) -> str:
    """Writes the workbook; returns its path."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_numbers": False,
                                    "strings_to_formulas": False, "strings_to_urls": False})
    bold = wb.add_format({"bold": True})
    head = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    link = wb.add_format({"font_color": "#0563C1", "underline": 1})
    text = wb.add_format({"num_format": "@"})
    per_sheet = EXCEL_MAX_ROWS - 1

    # Plan the tabs first (the summary links to them and comes first).
    used, plan = set(), []
    for f in collector.fields:
        written = f["total"] if max_rows is None else min(f["total"], max_rows)
        chunks = max(1, -(-written // per_sheet))
        base = f["target"] if f["target"] else str(f["source"]).split(".")[-1]
        names = [_sheet_name(base if c == 0 else f"{base} ({c + 1})", used) for c in range(chunks)]
        plan.append((f, written, names))
    missing = pd.concat(collector.missing, ignore_index=True) if collector.missing else pd.DataFrame()
    missing_written = len(missing) if max_rows is None else min(len(missing), max_rows)

    # Summary
    ws = wb.add_worksheet("Summary")
    rows = [("Test case", f"{info.get('test_case_number')} — {info.get('description', '')}"),
            ("Module", info.get("module", "")),
            ("Source", info.get("source", "")),
            ("Target", info.get("target", "")),
            ("Run time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("Rows per field", f"up to {max_rows:,}" if max_rows else "all")]
    for r, (k, v) in enumerate(rows):
        ws.write_string(r, 0, k, bold)
        ws.write_string(r, 1, str(v))
    r = len(rows) + 1
    for c, h in enumerate(["Field (source)", "Field (target)", "Mismatches", "Rows in file",
                           "Most common differences (count × source != target)", "Tab"]):
        ws.write_string(r, c, h, head)
    for f, written, names in plan:
        r += 1
        ws.write_string(r, 0, str(f["source"]))
        ws.write_string(r, 1, str(f["target"]))
        ws.write_number(r, 2, f["total"])
        ws.write_number(r, 3, written)
        ws.write_string(r, 4, "; ".join(f"{n:,} × {s} != {t}" for n, s, t in f["patterns"][:TOP_PATTERNS])
                        + (f"; … {len(f['patterns']) - TOP_PATTERNS} more" if len(f["patterns"]) > TOP_PATTERNS else ""))
        ws.write_url(r, 5, f"internal:'{names[0]}'!A1", link, string=names[0])
    if len(missing):
        r += 1
        ws.write_string(r, 0, "(whole record)")
        ws.write_string(r, 1, "")
        ws.write_number(r, 2, len(missing))
        ws.write_number(r, 3, missing_written)
        by_issue = missing["Issue"].value_counts()
        ws.write_string(r, 4, "; ".join(f"{n:,} × {k}" for k, n in by_issue.items()))
        ws.write_url(r, 5, "internal:'Missing records'!A1", link, string="Missing records")
    ws.set_column(0, 1, 28)
    ws.set_column(2, 3, 12)
    ws.set_column(4, 4, 70)
    ws.set_column(5, 5, 24)

    def write_frame(frame, names):
        cols = list(frame.columns)
        values = frame.values.tolist()
        for c_idx, name in enumerate(names):
            sheet = wb.add_worksheet(name)
            for c, h in enumerate(cols):
                sheet.write_string(0, c, str(h), head)
            part = values[c_idx * per_sheet:(c_idx + 1) * per_sheet]
            for i, row in enumerate(part, start=1):
                for c, v in enumerate(row):
                    sheet.write_string(i, c, str(v), text)
            sheet.set_column(0, len(cols) - 1, 22)
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(len(part), 1), len(cols) - 1)

    for f, written, names in plan:
        write_frame(f["frame"].head(written), names)
    if len(missing):
        chunks = max(1, -(-missing_written // per_sheet))
        write_frame(missing.head(missing_written),
                    ["Missing records"] + [f"Missing records ({i})" for i in range(2, chunks + 1)])
    wb.close()
    return path


def write_report(collector: MismatchCollector, info: dict, settings: dict = None):
    """Writes the workbook when enabled and there is something to report. Returns the path or None."""
    settings = settings or read_settings()
    if not settings["enabled"] or not collector.has_data:
        return None
    path = output_path(settings["dir"], info.get("module") or "Module", info.get("test_case_number"))
    return write_workbook(collector, path, info, settings["max_rows"])