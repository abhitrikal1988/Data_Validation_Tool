import logging
import pandas as pd
from decimal import Decimal
 
from Libraries.Regression_Log_Helper import append_log_entry
from .Edi835_fetcher import fetch_835_file
from .Edi835_parser  import parse_835_full
 
logger = logging.getLogger()
 
# Exact match — system always generates 2 decimal places, no rounding
BALANCE_TOLERANCE = Decimal("0.00")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Shared helper — single fetch, all DataFrames built once
# ─────────────────────────────────────────────────────────────────────────────
 
def _fetch_and_parse(source_steps, data_filters, runtime_filters,
                     test_case_number, log_doc,
                     visible_row_count=0) -> tuple:       # ← add param
    try:
        raw_835 = fetch_835_file(source_steps, data_filters, runtime_filters)
        result  = parse_835_full(raw_835)
 
        # ── Sample log so user can verify fetch ───────────────────
        if visible_row_count > 0 and raw_835:
            # Determine segment terminator from ISA header (position 105)
            seg_term = raw_835[105] if len(raw_835) > 105 else "~"
            segments = [s.strip() for s in raw_835.split(seg_term) if s.strip()]
 
            logger.info(
                f"[TC {test_case_number}] EDI835 fetched — "
                f"{len(raw_835):,} chars, {len(segments)} segments."
            )
            logger.info(
                f"[TC {test_case_number}] EDI835 segment sample "
                f"(first {min(visible_row_count, len(segments))} of {len(segments)}):"
            )
            for i, seg in enumerate(segments[:visible_row_count], 1):
                logger.info(f"  {i}: {seg}{seg_term}")
 
            # Show parsed claim summaries if available
            claims = (result or {}).get("claims", [])
            if claims:
                logger.info(
                    f"[TC {test_case_number}] EDI835 parsed claims sample "
                    f"(first {min(visible_row_count, len(claims))} of {len(claims)}):"
                )
                for i, claim in enumerate(claims[:visible_row_count], 1):
                    logger.info(
                        f"  {i}: ClaimID={claim.get('ClaimID','?')}, "
                        f"Status={claim.get('ClaimStatus','?')}, "
                        f"Submitted={claim.get('SubmittedAmount','?')}, "
                        f"Paid={claim.get('PaidAmount','?')}"
                    )
 
        return raw_835, result
    except Exception as e:
        msg = f"Failed to fetch/parse 835 file: {e}"
        logger.issue(f"TestCase {test_case_number}: {msg}")
        append_log_entry(log_doc, "Error", "EDI835", msg)
        log_doc["Result"] = "Fail"
        return None, None
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EDI835_Format — terminator, structural and field-level format validation
# ─────────────────────────────────────────────────────────────────────────────
 
REQUIRED_SEGMENTS = ["ISA", "GS", "ST", "BPR", "CLP", "SVC", "SE", "GE", "IEA"]
 
NUMERIC_ELEMENTS = [
    ("BPR", "BPR02", "Total Payment Amount"),
    ("CLP", "CLP03", "Claim Submitted Amount"),
    ("CLP", "CLP04", "Claim Paid Amount"),
    ("SVC", "SVC02", "Service Line Submitted Amount"),
    ("SVC", "SVC03", "Service Line Paid Amount"),
]
 
 
def _check_terminators(raw_835: str, test_case_number: int,

                        log_doc: dict) -> bool:
 
    failed = False
 
    if len(raw_835) < 106:

        msg = (

            f"File too short for valid X12 835 ({len(raw_835)} chars). "

            f"ISA requires at least 106 characters."

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("ISA_Length")

        return True
 
    element_sep  = raw_835[3]

    segment_term = raw_835[105]

    comp_sep     = raw_835[104]
 
    # Single summary log for terminators detected

    term_msg = (

        f"Terminators detected — "

        f"element_sep='{element_sep}' "

        f"segment_term='{segment_term}' "

        f"component_sep='{comp_sep}'"

    )

    logger.info(f"TestCase {test_case_number}: {term_msg}")

    append_log_entry(log_doc, "Info", "EDI835_Format", term_msg)
 
    first_segment_end = raw_835.find(segment_term)

    if first_segment_end == -1:

        msg = (

            f"Segment terminator '{segment_term}' not found in file. "

            f"File may be corrupt or wrong terminator detected."

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("Segment_Terminator")

        return True
 
    isa_segment  = raw_835[:first_segment_end]

    isa_elements = isa_segment.split(element_sep)
 
    isa_pass  = []

    isa_fails = []
 
    if len(isa_elements) != 17:

        failed = True

        msg    = (

            f"ISA segment has {len(isa_elements)} elements — "

            f"expected 17. "

            f"Possible incorrect element separator '{element_sep}'."

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("ISA_Element_Count")

        isa_fails.append("ISA_Element_Count")

    else:

        isa_pass.append("ISA element count valid (17)")
 
    isa16 = isa_elements[16].strip() if len(isa_elements) > 16 else ""

    if isa16 and isa16 != comp_sep:

        failed = True

        msg    = (

            f"ISA16 component separator '{isa16}' does not match "

            f"position 104 value '{comp_sep}'."

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("ISA16_ComponentSep")

        isa_fails.append("ISA16_ComponentSep")

    else:

        isa_pass.append(f"ISA16 component separator '{comp_sep}' consistent")
 
    # Single summary for ISA checks that passed

    if isa_pass:

        logger.info(f"TestCase {test_case_number}: ISA checks passed — {isa_pass}")

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"ISA checks passed — {isa_pass}")
 
    raw_segments    = [s.strip() for s in raw_835.split(segment_term) if s.strip()]

    invalid_seg_ids = []
 
    for seg in raw_segments:

        seg_id = seg.split(element_sep)[0].strip().upper()

        if not seg_id or not seg_id.isalnum() or len(seg_id) > 3:

            invalid_seg_ids.append(seg_id[:20])
 
    if invalid_seg_ids:

        failed = True

        msg    = (

            f"Terminator check: {len(invalid_seg_ids)} segment(s) with "

            f"invalid segment ID. Sample: {invalid_seg_ids[:5]}"

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("Segment_ID_Invalid")

    else:

        # Single summary for segment ID check

        logger.info(

            f"TestCase {test_case_number}: "

            f"Segment ID check — all {len(raw_segments)} segment(s) valid."

        )

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"Segment ID check — all {len(raw_segments)} segment(s) valid.")
 
    pair_pass = []
 
    def _check_pair(open_seg, close_seg, label):

        nonlocal failed

        open_count  = sum(1 for s in raw_segments

                          if s.split(element_sep)[0].strip().upper() == open_seg)

        close_count = sum(1 for s in raw_segments

                          if s.split(element_sep)[0].strip().upper() == close_seg)

        if open_count != close_count:

            failed = True

            msg    = (

                f"{label} mismatch: "

                f"{open_count} {open_seg} vs {close_count} {close_seg}."

            )

            logger.issue(f"TestCase {test_case_number}: {msg}")

            append_log_entry(log_doc, "Error", "EDI835_Format", msg)

            log_doc.setdefault("MismatchFields", []).append(

                f"{open_seg}_{close_seg}_Mismatch"

            )

        else:

            pair_pass.append(f"{label} ({open_count} group(s))")
 
    _check_pair("ST",  "SE",  "ST/SE")

    _check_pair("GS",  "GE",  "GS/GE")

    _check_pair("ISA", "IEA", "ISA/IEA")
 
    # Single summary for all passing pairs

    if pair_pass:

        logger.info(

            f"TestCase {test_case_number}: "

            f"Envelope pairs balanced — {pair_pass}"

        )

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"Envelope pairs balanced — {pair_pass}")
 
    return failed
 
 
def _check_control_counts(raw_835: str, parsed: dict,

                           test_case_number: int, log_doc: dict) -> bool:
 
    failed      = False

    element_sep = raw_835[3]

    seg_term    = raw_835[105]
 
    raw_segments = [s.strip() for s in raw_835.split(seg_term) if s.strip()]
 
    def _get_elements(seg_str):

        return seg_str.split(element_sep)
 
    count_pass = []
 
    # ── Check 1: GS01 = HP ────────────────────────────────────────────────────

    gs01_fail = False

    for seg in raw_segments:

        elems = _get_elements(seg)

        if elems[0].strip().upper() == "GS":

            gs01 = elems[1].strip() if len(elems) > 1 else ""

            if gs01.upper() != "HP":

                gs01_fail = True

                failed    = True

                msg       = (

                    f"GS01 = '{gs01}' — expected 'HP' for 835 "

                    f"(Health Care Claim Payment/Advice)."

                )

                logger.issue(f"TestCase {test_case_number}: {msg}")

                append_log_entry(log_doc, "Error", "EDI835_Format", msg)

                log_doc.setdefault("MismatchFields", []).append("GS01")

    if not gs01_fail:

        count_pass.append("GS01='HP'")
 
    # ── Check 2: ST01 = 835 ───────────────────────────────────────────────────

    st01_fail  = False

    st01_count = 0

    for seg in raw_segments:

        elems = _get_elements(seg)

        if elems[0].strip().upper() == "ST":

            st01_count += 1

            st01 = elems[1].strip() if len(elems) > 1 else ""

            if st01 != "835":

                st01_fail = True

                failed    = True

                msg       = (

                    f"ST01 = '{st01}' — expected '835' for "

                    f"Health Care Claim Payment transaction set."

                )

                logger.issue(f"TestCase {test_case_number}: {msg}")

                append_log_entry(log_doc, "Error", "EDI835_Format", msg)

                log_doc.setdefault("MismatchFields", []).append("ST01")

    if not st01_fail:

        count_pass.append(f"ST01='835' (all {st01_count} transaction set(s))")
 
    # ── Check 3: SE01 — declared segment count vs actual ─────────────────────

    in_transaction = False

    st_seg_count   = 0

    se_mismatches  = []
 
    for seg in raw_segments:

        elems  = _get_elements(seg)

        seg_id = elems[0].strip().upper()
 
        if seg_id == "ST":

            in_transaction = True

            st_seg_count   = 1

            continue
 
        if in_transaction:

            st_seg_count += 1

            if seg_id == "SE":

                in_transaction = False

                declared       = elems[1].strip() if len(elems) > 1 else "0"

                try:

                    declared_int = int(declared)

                except ValueError:

                    declared_int = 0

                if declared_int != st_seg_count:

                    se_mismatches.append({

                        "SE01_declared": declared_int,

                        "actual_count" : st_seg_count

                    })

                st_seg_count = 0
 
    if se_mismatches:

        failed = True

        msg    = (

            f"SE01 count mismatch: {len(se_mismatches)} transaction set(s) "

            f"where declared segment count != actual. "

            f"Detail: {se_mismatches}"

        )

        logger.issue(f"TestCase {test_case_number}: {msg}")

        append_log_entry(log_doc, "Error", "EDI835_Format", msg)

        log_doc.setdefault("MismatchFields", []).append("SE01_Count")

    else:

        count_pass.append(f"SE01 segment count valid (all {st01_count} transaction set(s))")
 
    # ── Check 4: GE01 — declared transaction set count vs actual ─────────────

    ge01_fail       = False

    actual_st_count = sum(

        1 for s in raw_segments

        if _get_elements(s)[0].strip().upper() == "ST"

    )

    for seg in raw_segments:

        elems = _get_elements(seg)

        if elems[0].strip().upper() == "GE":

            ge01 = elems[1].strip() if len(elems) > 1 else "0"

            try:

                ge01_int = int(ge01)

            except ValueError:

                ge01_int = 0

            if ge01_int != actual_st_count:

                ge01_fail = True

                failed    = True

                msg       = (

                    f"GE01 = {ge01_int} — declared transaction set count "

                    f"does not match actual ST count = {actual_st_count}."

                )

                logger.issue(f"TestCase {test_case_number}: {msg}")

                append_log_entry(log_doc, "Error", "EDI835_Format", msg)

                log_doc.setdefault("MismatchFields", []).append("GE01_Count")

    if not ge01_fail:

        count_pass.append(f"GE01={actual_st_count} matches ST count")
 
    # ── Check 5: IEA01 — declared functional group count vs actual ────────────

    iea01_fail      = False

    actual_gs_count = sum(

        1 for s in raw_segments

        if _get_elements(s)[0].strip().upper() == "GS"

    )

    for seg in raw_segments:

        elems = _get_elements(seg)

        if elems[0].strip().upper() == "IEA":

            iea01 = elems[1].strip() if len(elems) > 1 else "0"

            try:

                iea01_int = int(iea01)

            except ValueError:

                iea01_int = 0

            if iea01_int != actual_gs_count:

                iea01_fail = True

                failed     = True

                msg        = (

                    f"IEA01 = {iea01_int} — declared functional group count "

                    f"does not match actual GS count = {actual_gs_count}."

                )

                logger.issue(f"TestCase {test_case_number}: {msg}")

                append_log_entry(log_doc, "Error", "EDI835_Format", msg)

                log_doc.setdefault("MismatchFields", []).append("IEA01_Count")

    if not iea01_fail:

        count_pass.append(f"IEA01={actual_gs_count} matches GS count")
 
    # Single summary for all passing control count checks

    if count_pass:

        logger.info(

            f"TestCase {test_case_number}: "

            f"Control count checks passed — {count_pass}"

        )

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"Control count checks passed — {count_pass}")
 
    return failed
 
 
def run_edi835_format_check(source_steps, data_filters, runtime_filters,
                             check_fields, test_case_number,
                             test_description, log_doc,
                             visible_row_count=0) -> bool:
    """Validates terminators, control counts, required segments and numeric elements."""
    logger.info(
        f"TestCase {test_case_number}: EDI835_Format started — "
        f"fetching and validating 835 file structure"
    )
    append_log_entry(log_doc, "Info", "EDI835_Format",
                     "Validating 835 file structure and field formats.")
    raw_835, result = _fetch_and_parse(
        source_steps, data_filters, runtime_filters,
        test_case_number, log_doc, visible_row_count=visible_row_count
    )

    if raw_835 is None or result is None:
        log_doc["Result"] = "Fail"          # Fix — set Result on early return
        return True

    parsed = result["parsed"]
    failed = False
 
    # ── Check group 1: Terminators ────────────────────────────────────────────

    if _check_terminators(raw_835, test_case_number, log_doc):
        failed = True
 
    # ── Check group 2: Control counts ────────────────────────────────────────

    if _check_control_counts(raw_835, parsed, test_case_number, log_doc):
        failed = True
 
    # ── Build segment presence map ────────────────────────────────────────────

    present_segments = set()
 
    if parsed.get("ISA"):
        present_segments.add("ISA")

    if parsed.get("GS"):
        present_segments.add("GS")

    if parsed.get("IEA"):                   # Fix — only add if actually parsed
        present_segments.add("IEA")
 
    for transaction in parsed.get("transactions", []):
        if transaction.get("ST"):
            present_segments.add("ST")
        if transaction.get("SE"):
            present_segments.add("SE")
        if transaction.get("GE"):           # Fix — only add if actually parsed
            present_segments.add("GE")
        for group in transaction.get("bpr_groups", []):
            if group.get("BPR"):
                present_segments.add("BPR")
            if group.get("PLB"):
                present_segments.add("PLB")
            for claim in group.get("claims", []):
                if claim.get("CLP"):
                    present_segments.add("CLP")
                if claim.get("adjustments"):
                    present_segments.add("CAS")
                for svc in claim.get("service_lines", []):
                    if svc.get("SVC"):
                        present_segments.add("SVC")
 
    required      = list(REQUIRED_SEGMENTS)

    extra_numeric = []
 
    for cf in (check_fields or []):

        segment = (cf.get("Segment") or "").upper()

        rule    = (cf.get("Rule")    or "").upper()

        element = cf.get("Element", "")
 
        if rule == "REQUIRED" and segment and segment not in required:

            required.append(segment)

        elif rule == "NUMERIC" and segment and element:

            extra_numeric.append((segment, element, f"{segment} {element}"))
 
    # ── Check group 3: Required segments — log FAIL only ─────────────────────

    seg_pass = []

    seg_fail = []
 
    for seg in required:

        if seg not in present_segments:

            failed = True

            seg_fail.append(seg)

            msg = f"Required segment '{seg}' not found in 835 file."

            logger.issue(f"TestCase {test_case_number}: {msg}")

            append_log_entry(log_doc, "Error", "EDI835_Format", msg)

            log_doc.setdefault("MismatchFields", []).append(seg)

        else:

            seg_pass.append(seg)
 
    # Single summary line for all passing segments

    if seg_pass:

        logger.info(

            f"TestCase {test_case_number}: "

            f"Required segments present: {seg_pass}"

        )

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"Required segments present: {seg_pass}")
 
    # ── Check group 4: Numeric elements — log FAIL only ──────────────────────

    all_numeric_checks = NUMERIC_ELEMENTS + extra_numeric

    non_numeric        = {label: [] for _, _, label in all_numeric_checks}
 
    for transaction in parsed.get("transactions", []):

        for group in transaction.get("bpr_groups", []):

            bpr = group.get("BPR", {})

            for seg_id, elem, label in all_numeric_checks:

                if seg_id == "BPR":

                    val = bpr.get(elem)

                    if val is not None and not isinstance(val, Decimal):

                        try:

                            Decimal(str(val))

                        except Exception:

                            non_numeric[label].append(f"BPR {elem}='{val}'")
 
            for claim in group.get("claims", []):

                clp = claim.get("CLP", {})          # Fix — safe .get()

                for seg_id, elem, label in all_numeric_checks:

                    if seg_id == "CLP":

                        val = clp.get(elem)

                        if val is not None and not isinstance(val, Decimal):

                            try:

                                Decimal(str(val))

                            except Exception:

                                non_numeric[label].append(

                                    f"CLP01={clp.get('CLP01', '')} {elem}='{val}'"

                                )
 
                for svc in claim.get("service_lines", []):

                    sv = svc.get("SVC", {})          # Fix — safe .get()

                    for seg_id, elem, label in all_numeric_checks:

                        if seg_id == "SVC":

                            val = sv.get(elem)

                            if val is not None and not isinstance(val, Decimal):

                                try:

                                    Decimal(str(val))

                                except Exception:

                                    non_numeric[label].append(

                                        f"CLP={claim.get('CLP', {}).get('CLP01', '')} "

                                        f"SVC01={sv.get('SVC01', '')} {elem}='{val}'"

                                    )
 
    num_pass = []

    num_fail = []
 
    for _, _, label in all_numeric_checks:

        issues = non_numeric[label]

        if issues:

            failed = True

            num_fail.append(label)

            msg = (

                f"Element '{label}': {len(issues)} non-numeric value(s). "

                f"Sample: {issues[:10]}"

            )

            logger.issue(f"TestCase {test_case_number}: {msg}")

            append_log_entry(log_doc, "Error", "EDI835_Format", msg)

            log_doc.setdefault("MismatchFields", []).append(label)

        else:

            num_pass.append(label)
 
    # Single summary line for all passing numeric elements

    if num_pass:

        logger.info(

            f"TestCase {test_case_number}: "

            f"Numeric elements all valid: {num_pass}"

        )

        append_log_entry(log_doc, "Info", "EDI835_Format",

                         f"Numeric elements all valid: {num_pass}")
 
    # ── Final summary ─────────────────────────────────────────────────────────

    if not failed:

        logger.info(

            f"TestCase {test_case_number}: EDI835_Format — PASS. "

            f"All {len(required)} required segments present, "

            f"all {len(all_numeric_checks)} numeric elements valid."

        )

    else:

        logger.issue(

            f"TestCase {test_case_number}: EDI835_Format — FAIL. "

            f"Segment failures: {seg_fail}. "

            f"Numeric failures: {num_fail}."

        )
 
    log_doc["Result"] = "Fail" if failed else "Pass"

    return failed
 
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EDI835_Balance — internal amount balancing
# ─────────────────────────────────────────────────────────────────────────────
 
def run_edi835_balance_check(source_steps, data_filters, runtime_filters,
                              check_fields, test_case_number,
                              test_description, log_doc,
                              visible_row_count=0) -> bool:
    """Validates BPR, claim, service line and member paid balancing. True=fail."""
    logger.info(
        f"TestCase {test_case_number}: EDI835_Balance started — "
        f"validating internal amount balancing"
    )
    append_log_entry(log_doc, "Info", "EDI835_Balance",
                     "Validating internal 835 amount balancing.")
 
    _, result = _fetch_and_parse(
        source_steps, data_filters, runtime_filters,
        test_case_number, log_doc, visible_row_count=visible_row_count
    )
    if result is None:
        return True
 
    bpr_summaries = result["bpr_summaries"]
    clp_df        = result["clp_df"]
    svc_df        = result["svc_df"]
 
    if check_fields:
        rules_to_run = {
            (cf.get("Rule") or "").upper()
            for cf in check_fields
            if cf.get("Rule")
        }
    else:
        rules_to_run = {
            "BPR_BALANCE",
            "CLAIM_BALANCE",
            "CLAIM_SUBMITTED_BALANCE",
            "SERVICE_LINE_BALANCE",
            "MEMBER_PAID_BALANCE"
        }
 
    failed = False
 
    # ── BPR_BALANCE ───────────────────────────────────────────────────────────
    if "BPR_BALANCE" in rules_to_run:
        for summary in bpr_summaries:
            bpr02        = summary["BPR02"]
            plb_total    = summary["plb_total"]
            clp_paid     = summary["clp_paid_total"]
            expected_bpr = clp_paid - plb_total
            diff         = abs(bpr02 - expected_bpr)
            g_label      = (
                f"transaction={summary['transaction_idx']} "
                f"bpr_group={summary['bpr_group_idx']}"
            )
 
            if diff > BALANCE_TOLERANCE:
                failed = True
                msg    = (
                    f"BPR_BALANCE FAIL [{g_label}]: "
                    f"BPR02={bpr02} != "
                    f"sum(CLP04)={clp_paid} - sum(PLB)={plb_total} "
                    f"[expected={expected_bpr}, diff={diff}]"
                )
                logger.issue(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Error", "EDI835_Balance", msg)
                log_doc.setdefault("MismatchFields", []).append("BPR02")
            else:
                msg = (
                    f"BPR_BALANCE PASS [{g_label}]: "
                    f"BPR02={bpr02} = "
                    f"sum(CLP04)={clp_paid} - sum(PLB)={plb_total}"
                )
                #logger.info(f"TestCase {test_case_number}: {msg}")
                append_log_entry(log_doc, "Info", "EDI835_Balance", msg)
 
    # ── CLAIM_BALANCE ─────────────────────────────────────────────────────────
    if "CLAIM_BALANCE" in rules_to_run:
        claim_fails = []
        for _, row in clp_df.iterrows():
            diff = abs(
                Decimal(str(row["CLP04"])) -
                Decimal(str(row["svc_paid_total"]))
            )
            if diff > BALANCE_TOLERANCE:
                claim_fails.append({
                    "ClaimID"       : row["ClaimID"],
                    "CLP04"         : row["CLP04"],
                    "svc_paid_total": row["svc_paid_total"],
                    "diff"          : float(diff)
                })
 
        if claim_fails:
            failed = True
            msg    = (
                f"CLAIM_BALANCE FAIL: "
                f"{len(claim_fails)} claim(s) where CLP04 != sum(SVC03). "
                f"First {min(len(claim_fails), 10)}: {claim_fails[:10]}"
            )
            logger.issue(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Error", "EDI835_Balance", msg)
            log_doc.setdefault("MismatchFields", []).append("CLP04_SVC03_Balance")
        else:
            msg = (
                f"CLAIM_BALANCE PASS: "
                f"All {len(clp_df)} claim(s) — CLP04 = sum(SVC03)."
            )
            logger.info(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Info", "EDI835_Balance", msg)
 
    # ── CLAIM_SUBMITTED_BALANCE ───────────────────────────────────────────────
    if "CLAIM_SUBMITTED_BALANCE" in rules_to_run:
        submitted_fails = []
        for _, row in clp_df.iterrows():
            diff = abs(
                Decimal(str(row["CLP03"])) -
                Decimal(str(row["svc_submitted_total"]))
            )
            if diff > BALANCE_TOLERANCE:
                submitted_fails.append({
                    "ClaimID"            : row["ClaimID"],
                    "CLP03"              : row["CLP03"],
                    "svc_submitted_total": row["svc_submitted_total"],
                    "diff"               : float(diff)
                })
 
        if submitted_fails:
            failed = True
            msg    = (
                f"CLAIM_SUBMITTED_BALANCE FAIL: "
                f"{len(submitted_fails)} claim(s) where CLP03 != sum(SVC02). "
                f"First {min(len(submitted_fails), 10)}: {submitted_fails[:10]}"
            )
            logger.issue(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Error", "EDI835_Balance", msg)
            log_doc.setdefault("MismatchFields", []).append("CLP03_SVC02_Balance")
        else:
            msg = (
                f"CLAIM_SUBMITTED_BALANCE PASS: "
                f"All {len(clp_df)} claim(s) — CLP03 = sum(SVC02)."
            )
            logger.info(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Info", "EDI835_Balance", msg)
 
    # ── SERVICE_LINE_BALANCE ──────────────────────────────────────────────────
    if "SERVICE_LINE_BALANCE" in rules_to_run:
        svc_line_fails = []
 
        for _, row in svc_df.iterrows():
            if str(row.get("CLP02", "")).strip() == "22":
                continue
 
            svc02          = Decimal(str(row["SVC02"]))
            svc03          = Decimal(str(row["SVC03"]))
            cas_total      = Decimal(str(row["cas_total"]))
            expected_svc02 = svc03 + cas_total
            diff           = abs(svc02 - expected_svc02)
 
            if diff > BALANCE_TOLERANCE:
                svc_line_fails.append({
                    "ClaimID"  : row["ClaimID"],
                    "SVC01"    : row["SVC01"],
                    "SVC02"    : float(svc02),
                    "SVC03"    : float(svc03),
                    "cas_total": float(cas_total),
                    "expected" : float(expected_svc02),
                    "diff"     : float(diff)
                })
 
        if svc_line_fails:
            failed = True
            msg    = (
                f"SERVICE_LINE_BALANCE FAIL: "
                f"{len(svc_line_fails)} service line(s) where "
                f"SVC02 != SVC03 + sum(CAS). "
                f"First {min(len(svc_line_fails), 10)}: {svc_line_fails[:10]}"
            )
            logger.issue(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Error", "EDI835_Balance", msg)
            log_doc.setdefault("MismatchFields", []).append("SVC02_Balance")
        else:
            non_reversal_count = len(
                svc_df[svc_df["CLP02"].astype(str).str.strip() != "22"]
            )
            msg = (
                f"SERVICE_LINE_BALANCE PASS: "
                f"{non_reversal_count} service line(s) — "
                f"SVC02 = SVC03 + sum(CAS)."
            )
            logger.info(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Info", "EDI835_Balance", msg)
 
    # ── MEMBER_PAID_BALANCE ───────────────────────────────────────────────────
    if "MEMBER_PAID_BALANCE" in rules_to_run:
        member_paid_fails = [
            {
                "ClaimID"    : row["ClaimID"],
                "SVC01"      : row["SVC01"],
                "member_paid": row["member_paid"]
            }
            for _, row in svc_df.iterrows()
            if Decimal(str(row["member_paid"])) < Decimal("0")
            and str(row.get("CLP02", "")).strip() != "22"
        ]
 
        if member_paid_fails:
            failed = True
            msg    = (
                f"MEMBER_PAID_BALANCE FAIL: "
                f"{len(member_paid_fails)} service line(s) with negative "
                f"member paid (CAS*PR). "
                f"First {min(len(member_paid_fails), 10)}: "
                f"{member_paid_fails[:10]}"
            )
            logger.issue(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Error", "EDI835_Balance", msg)
            log_doc.setdefault("MismatchFields", []).append("MemberPaid_CAS_PR")
        else:
            non_reversal_count = len(
                svc_df[svc_df["CLP02"].astype(str).str.strip() != "22"]
            )
            msg = (
                f"MEMBER_PAID_BALANCE PASS: "
                f"{non_reversal_count} service line(s) — member paid >= 0."
            )
            logger.info(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Info", "EDI835_Balance", msg)
 
    log_doc["Result"] = "Fail" if failed else "Pass"
    return failed
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EDI835_Data — cross-validate 835 amounts against Oracle/MongoDB
# Reuses existing compare_data() — same pattern as SRC_TGT
# ─────────────────────────────────────────────────────────────────────────────
 
def run_edi835_data_check(source_steps, data_filters,
                           target_mongo_filter, target_oracle_filter,
                           runtime_filters, target, targets,
                           test_case_number, test_description,
                           log_doc, viceversa=False,
                           visible_row_count=0) -> bool:
    """
    Cross-validates 835 amounts against Oracle or MongoDB targets.
    Single 835 fetch — all DataFrames shared across all targets.
 
    Target filter resolution:
        MongoDB targets — filter built from TargetField in DataFilters
        Oracle targets  — filter built from TargetField in DataFilters
        Each target can also define its own DataFilters array for
        target-specific filtering.
 
    Level auto-detected from JoinFields/CompareFields:
        BPR02/plb_total  → BPR level    → bpr_df
        adjustment_count → PLB level    → plb_df
        SVC01/SVC02/SVC03/
        member_paid/
        cas_total/line_seq → SERVICE_LINE → svc_df
        Otherwise          → CLAIM level  → clp_df
    """
    from Libraries.data_fetcher import get_data
    from Libraries.parameter_reading import resolve_data_filters
    from Libraries.Comparison_vice_versa_with_log_info import (
        compare_data, is_cursor_empty
    )
    from pymongo.cursor import Cursor
    import json
 
    logger.info(
        f"TestCase {test_case_number}: EDI835_Data started — "
        f"fetching 835 file"
    )
    append_log_entry(log_doc, "Info", "EDI835_Data",
                     "Cross-validating 835 amounts against target(s).")
 
    # ── Single fetch — all DataFrames built once ──────────────────────────────
    _, result = _fetch_and_parse(
        source_steps, data_filters, runtime_filters,
        test_case_number, log_doc, visible_row_count=visible_row_count
    )
    if result is None:
        return True
 
    clp_df = result["clp_df"]
    svc_df = result["svc_df"]
    bpr_df = result["bpr_df"]
    plb_df = result["plb_df"]
 
    # ── Build targets list ────────────────────────────────────────────────────
    target_list = []
    if targets:
        target_list.extend(targets)
    elif target:
        entry = dict(target)
        entry.setdefault("Viceversa", viceversa)
        target_list.append(entry)
 
    if not target_list:
        msg = "No Target or Targets defined in test case."
        logger.issue(f"TestCase {test_case_number}: {msg}")
        append_log_entry(log_doc, "Error", "EDI835_Data", msg)
        log_doc["Result"] = "Fail"
        return True
 
    overall_failed = False
 
    # ── Run comparison per target ─────────────────────────────────────────────
    for idx, tgt in enumerate(target_list):
        description    = tgt.get("Description", f"Target {idx + 1}")
        target_type    = (tgt.get("Type", "") or "").upper()
        target_env     = (tgt.get("Environment", "") or "").upper()
        target_db      = tgt.get("DB", "")
        target_query   = tgt.get("Query", "")
        target_qtype   = tgt.get("QueryType", "")
        join_fields    = tgt.get("JoinFields", [])
        compare_fields = tgt.get("CompareFields", [])
        tgt_viceversa  = tgt.get("Viceversa", viceversa)
 
      
        logger.info(
            f"TestCase {test_case_number}: EDI835_Data — "
            f"running '{description}' against {target_type}"
        )
        append_log_entry(log_doc, "Info", "EDI835_Data",
                         f"Comparing: {description}")
 
        # ── Auto-detect level ─────────────────────────────────────────────────
        bpr_columns = {"BPR02", "plb_total", "clp_paid_total", "clp_submitted_total"}
        plb_columns = {"adjustment_count"}
        svc_columns = {"SVC01", "SVC02", "SVC03", "member_paid",
                       "cas_total", "line_seq", "copay",
                       "coinsurance", "deductible"}
 
        all_source_fields = set(
            f.get("SourceField", "")
            for f in (join_fields or []) + (compare_fields or [])
        )
 
        if all_source_fields & bpr_columns:
            source_df = bpr_df
            level     = "BPR"
        elif all_source_fields & plb_columns:
            source_df = plb_df
            level     = "PLB"
        elif all_source_fields & svc_columns:
            source_df = svc_df
            level     = "SERVICE_LINE"
        else:
            source_df = clp_df
            level     = "CLAIM"
 
        logger.info(
            f"TestCase {test_case_number}: '{description}' — "
            f"{level} level ({len(source_df)} rows)"
        )
 
        if source_df.empty:
            msg = f"'{description}': 835 has no {level} data — skipping."
            logger.warning(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Warning", "EDI835_Data", msg)
            overall_failed = True
            continue
 
       
        # Use pre-resolved filters — same pattern as existing keywords
        if target_type == "MONGODB":
            tgt_mongo_filter = target_mongo_filter or {}
            tgt_oracle_query = target_query
        else:
            tgt_mongo_filter = {}
            if target_oracle_filter and "select " in target_query.lower():
                tgt_oracle_query = (
                    f"SELECT * FROM ({target_query}) "
                    f"WHERE {target_oracle_filter}"
                )
            else:
                tgt_oracle_query = target_query
 
        # ── Fetch target data ─────────────────────────────────────────────────
        # ── Log exact target query ────────────────────────────────────────────
        if target_type == "MONGODB":
            logger.info(
                f"TestCase {test_case_number}: "
                f"Target [MONGODB]: db.{target_query}.find("
                f"{json.dumps(tgt_mongo_filter, indent=2, default=str)})"
            )
        else:
            logger.info(
                f"TestCase {test_case_number}: "
                f"Target [ORACLE]: {tgt_oracle_query}"
            )
 
        # ── Fetch target data ─────────────────────────────────────────────────
        _, target_data = get_data(
            None, None, None, "",
            target_type, target_db,
            tgt_oracle_query, target_env,
            None, target_qtype,
            {}, tgt_mongo_filter
        )
 
        if is_cursor_empty(target_data, f"target_{description}"):
            msg = (
                f"'{description}': Target {target_type} returned no data — "
                f"skipping comparison."
            )
            logger.warning(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Warning", "EDI835_Data", msg)
            overall_failed = True
            continue
 
        if isinstance(target_data, Cursor):
            target_data = list(target_data)

        if visible_row_count >0 and target_data:
            sample = target_data[:visible_row_count]
            for i, record in enumerate(sample,1):
                logger.info(f" {i}:{record}")



        target_df = pd.DataFrame(target_data)
 
        append_log_entry(
            log_doc, "Info", "EDI835_Data",
            f"'{description}': "
            f"835 source={len(source_df)} rows, "
            f"target={len(target_df)} rows"
        )
 
        # ── Compare ───────────────────────────────────────────────────────────
        target_failed = compare_data(
            source_df.to_dict(orient="records"),
            target_df.to_dict(orient="records"),
            "EDI835", target_type,
            join_fields, compare_fields,
            test_case_number,
            f"{test_description} — {description}",
            viceversa=tgt_viceversa,
            log_doc=log_doc
        )
 
        if target_failed:
            overall_failed = True
            msg = f"'{description}': FAIL"
            logger.issue(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Error", "EDI835_Data", msg)
        else:
            msg = f"'{description}': PASS"
            logger.info(f"TestCase {test_case_number}: {msg}")
            append_log_entry(log_doc, "Info", "EDI835_Data", msg)
 
    log_doc["Result"] = "Fail" if overall_failed else "Pass"
    return overall_failed