import logging
import pandas as pd
from decimal import Decimal, InvalidOperation
 
logger = logging.getLogger()
 
 
# ─────────────────────────────────────────────────────────────────────────────
# X12 835 Parser
#
# Handles:
#   - Single BPR per ST/SE (standard)
#   - Multiple BPR per ST/SE (auto-detected)
#   - PLB with multiple reason/amount pairs — summed per NPI+TIN+CheckNumber
#   - CAS at claim and service line level
#   - Member paid (CAS*PR) accumulated per service line
#   - Reversal claims (CLP02=22) — all amounts negative by design
#   - NPI from N1*PE**XX segment
#   - TIN from REF*TJ segment
#   - CheckNumber from TRN segment
# ─────────────────────────────────────────────────────────────────────────────
 
 
def _to_decimal(value) -> Decimal:
    """Safely converts a value to Decimal. Returns Decimal(0) on failure."""
    try:
        if value is None or str(value).strip() == "":
            return Decimal("0")
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        # Fix #7 — log bad values instead of silently zeroing
        logger.warning(
            f"EDI835 Parser: Could not convert '{value}' to Decimal — defaulting to 0."
        )
        return Decimal("0")
 
 
def _parse_segments(raw_835: str) -> list:
    """
    Splits raw X12 835 into list of segment element lists.
    Auto-detects element separator (position 3) and
    segment terminator (position 105) from ISA header.
    """
    raw = raw_835.strip()
 
    if len(raw) < 106:
        raise ValueError(
            f"EDI835 Parser: File too short ({len(raw)} chars). "
            f"ISA requires at least 106 characters."
        )
 
    element_sep  = raw[3]
    segment_term = raw[105]
 
    raw_segments = [
        s.strip() for s in raw.split(segment_term) if s.strip()
    ]
 
    segments = [seg.split(element_sep) for seg in raw_segments]
 
    logger.info(
        f"EDI835 Parser: element_sep='{element_sep}' "
        f"segment_term='{segment_term}' "
        f"total_segments={len(segments)}"
    )
 
    return segments
 
 
def parse_835(raw_835: str) -> dict:
    """
    Parses raw X12 835 into structured dict.
    Auto-detects single vs multiple BPR per ST/SE.
    Groups claims under their nearest preceding BPR.
    Captures NPI, TIN, CheckNumber at BPR group level.
 
    Structure:
    {
        "ISA": {...},
        "GS" : {...},
        "transactions": [
            {
                "ST"        : {...},
                "SE"        : {...},
                "bpr_groups": [
                    {
                        "BPR"        : { "BPR02": Decimal, ... },
                        "NPI"        : "1215914288",
                        "TIN"        : "133757370",
                        "CheckNumber": "404245",
                        "PLB"        : [
                            {
                                "PLB01"      : "1215914288",
                                "PLB02"      : "20251231",
                                "adjustments": [
                                    { "reason": "FB:420053", "amount": Decimal }
                                ]
                            }
                        ],
                        "claims": [
                            {
                                "CLP": {
                                    "CLP01": "PatientControlNum",
                                    "CLP02": "22",
                                    "CLP03": Decimal,
                                    "CLP04": Decimal,
                                    "CLP07": "PayerClaimControlNum"
                                },
                                "adjustments"  : [...],
                                "service_lines": [
                                    {
                                        "SVC"        : { "SVC01": ..., "SVC02": Decimal, "SVC03": Decimal },
                                        "adjustments": [...],
                                        "member_paid": Decimal
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
    """
    segments = _parse_segments(raw_835)
 
    result = {
        "ISA"         : {},
        "GS"          : {},
        "transactions": []
    }
 
    current_transaction = None
    current_bpr_group   = None
    current_claim       = None
    current_svc         = None
 
    for seg in segments:
        if not seg or not seg[0]:
            continue
 
        seg_id = seg[0].strip().upper()
 
        # ── Interchange envelope ──────────────────────────────────────────────
        if seg_id == "ISA":
            result["ISA"] = {
                f"ISA{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }
 
        elif seg_id == "GS":
            result["GS"] = {
                f"GS{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }
 
        # ── Transaction set ───────────────────────────────────────────────────
        elif seg_id == "ST":
            current_transaction = {
                "ST"        : {
                    f"ST{str(i).zfill(2)}": seg[i].strip()
                    for i in range(1, len(seg))
                },
                "SE"        : {},
                "bpr_groups": []
            }
            result["transactions"].append(current_transaction)
            current_bpr_group = None
            current_claim     = None
            current_svc       = None
 
        elif seg_id == "SE":
            if current_transaction is not None:
                current_transaction["SE"] = {
                    f"SE{str(i).zfill(2)}": seg[i].strip()
                    for i in range(1, len(seg))
                }
            current_bpr_group = None
            current_claim     = None
            current_svc       = None

        elif seg_id == "GE":
            if current_transaction is not None:
                current_transaction["GE"] = {
                    f"GE{str(i).zfill(2)}": seg[i].strip()
                    for i in range(1, len(seg))
                }
        
        elif seg_id == "IEA":
            result["IEA"] = {
                f"IEA{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }

        # ── BPR — starts a new BPR group ──────────────────────────────────────
        elif seg_id == "BPR":
            if current_transaction is None:
                logger.warning("EDI835 Parser: BPR outside ST/SE — skipping.")
                continue
 
            bpr_data = {
                f"BPR{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }
            if "BPR02" in bpr_data:
                bpr_data["BPR02"] = _to_decimal(bpr_data["BPR02"])
 
            current_bpr_group = {
                "BPR"        : bpr_data,
                "NPI"        : "",    # populated from N1*PE**XX
                "TIN"        : "",    # populated from REF*TJ
                "CheckNumber": "",    # populated from TRN02
                "PLB"        : [],
                "claims"     : []
            }
            current_transaction["bpr_groups"].append(current_bpr_group)
            current_claim = None
            current_svc   = None
 
        # ── TRN — check/trace number ──────────────────────────────────────────
        elif seg_id == "TRN":
            if current_bpr_group is not None and not current_claim:
                # TRN02 = check/trace number
                current_bpr_group["CheckNumber"] = (
                    seg[2].strip() if len(seg) > 2 else ""
                )
 
        # ── N1 — party identification ─────────────────────────────────────────
        elif seg_id == "N1":
            if current_bpr_group is not None and not current_claim:
                n101 = seg[1].strip().upper() if len(seg) > 1 else ""
                n103 = seg[3].strip().upper() if len(seg) > 3 else ""
                n104 = seg[4].strip()          if len(seg) > 4 else ""
 
                # N1*PE**XX*{NPI} — Payee with NPI qualifier XX
                if n101 == "PE" and n103 == "XX":
                    current_bpr_group["NPI"] = n104
 
        # ── REF — reference identification ────────────────────────────────────
        elif seg_id == "REF":
            if current_bpr_group is not None and not current_claim:
                ref01 = seg[1].strip().upper() if len(seg) > 1 else ""
                ref02 = seg[2].strip()          if len(seg) > 2 else ""
 
                # REF*TJ = Federal TIN
                if ref01 == "TJ":
                    current_bpr_group["TIN"] = ref02
 
        # ── PLB — provider level adjustment ───────────────────────────────────
        elif seg_id == "PLB":
            if current_bpr_group is None:
                logger.warning("EDI835 Parser: PLB outside BPR group — skipping.")
                continue
 
            plb_entry = {
                "PLB01"      : seg[1].strip() if len(seg) > 1 else "",
                "PLB02"      : seg[2].strip() if len(seg) > 2 else "",
                "adjustments": []
            }
 
            # PLB03+ are reason/amount pairs
            i = 3
            while i < len(seg):
                reason = seg[i].strip() if i < len(seg) else ""
                amount = _to_decimal(seg[i + 1]) if (i + 1) < len(seg) else Decimal("0")
                if reason:
                    plb_entry["adjustments"].append({
                        "reason": reason,
                        "amount": amount
                    })
                i += 2
 
            current_bpr_group["PLB"].append(plb_entry)
 
        # ── CLP — claim level ─────────────────────────────────────────────────
        elif seg_id == "CLP":
            if current_bpr_group is None:
                logger.warning("EDI835 Parser: CLP outside BPR group — skipping.")
                continue
 
            clp_data = {
                f"CLP{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }
            for elem in ["CLP03", "CLP04", "CLP05"]:
                if elem in clp_data:
                    clp_data[elem] = _to_decimal(clp_data[elem])
 
            current_claim = {
                "CLP"          : clp_data,
                "adjustments"  : [],
                "service_lines": []
            }
            current_bpr_group["claims"].append(current_claim)
            current_svc = None
 
        # ── CAS — adjustment ──────────────────────────────────────────────────
        elif seg_id == "CAS":
            if current_claim is None:
                continue
 
            group_code = seg[1].strip() if len(seg) > 1 else ""
 
            i = 2
            while i < len(seg):
                reason_code = seg[i].strip()          if i       < len(seg) else ""
                amount      = _to_decimal(seg[i + 1]) if (i + 1) < len(seg) else Decimal("0")
                quantity    = seg[i + 2].strip()       if (i + 2) < len(seg) else ""
 
                if reason_code:
                    cas_entry = {
                        "CAS01": group_code,
                        "CAS02": reason_code,
                        "CAS03": amount,
                        "CAS04": quantity
                    }
 
                    if current_svc is not None:
                        current_svc["adjustments"].append(cas_entry)
                        if group_code.upper() == "PR":
                            current_svc["member_paid"] += amount
                    else:
                        current_claim["adjustments"].append(cas_entry)
 
                i += 3
 
        # ── SVC — service line level ──────────────────────────────────────────
        elif seg_id == "SVC":
            if current_claim is None:
                logger.warning("EDI835 Parser: SVC outside CLP — skipping.")
                continue
 
            svc_data = {
                f"SVC{str(i).zfill(2)}": seg[i].strip()
                for i in range(1, len(seg))
            }
            for elem in ["SVC02", "SVC03", "SVC05"]:
                if elem in svc_data:
                    svc_data[elem] = _to_decimal(svc_data[elem])
 
            current_svc = {
                "SVC"        : svc_data,
                "adjustments": [],
                "member_paid": Decimal("0")
            }
            current_claim["service_lines"].append(current_svc)
 
    total_bpr_groups = sum(len(t["bpr_groups"]) for t in result["transactions"])
    total_claims     = sum(
        len(g["claims"])
        for t in result["transactions"]
        for g in t["bpr_groups"]
    )
    total_svcs = sum(
        len(c["service_lines"])
        for t in result["transactions"]
        for g in t["bpr_groups"]
        for c in g["claims"]
    )
 
    logger.info(
        f"EDI835 Parser: "
        f"transactions={len(result['transactions'])}, "
        f"bpr_groups={total_bpr_groups}, "
        f"claims={total_claims}, "
        f"service_lines={total_svcs}"
    )
 
    return result
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DataFrame builders
# ─────────────────────────────────────────────────────────────────────────────
 
def get_bpr_summary(parsed: dict) -> list:
    summaries = []
    for t_idx, transaction in enumerate(parsed.get("transactions", [])):
        for g_idx, group in enumerate(transaction.get("bpr_groups", [])):
 
            # Fix #5 — safe .get() on BPR
            bpr02     = group.get("BPR", {}).get("BPR02", Decimal("0"))
            plb_total = Decimal("0")
 
            for plb in group.get("PLB", []):
                for adj in plb.get("adjustments", []):
                    plb_total += adj.get("amount", Decimal("0"))
 
            clp_paid_total      = Decimal("0")
            clp_submitted_total = Decimal("0")
 
            for claim in group.get("claims", []):
                # Fix #3 — safe .get() on CLP
                clp = claim.get("CLP", {})
                clp_paid_total      += clp.get("CLP04", Decimal("0"))
                clp_submitted_total += clp.get("CLP03", Decimal("0"))
 
            summaries.append({
                "transaction_idx"    : t_idx,
                "bpr_group_idx"      : g_idx,
                "BPR02"              : bpr02,
                "plb_total"          : plb_total,
                "clp_paid_total"     : clp_paid_total,
                "clp_submitted_total": clp_submitted_total
            })
 
    return summaries
 
 
def get_bpr_dataframe(parsed: dict) -> pd.DataFrame:
    rows = []
    for t_idx, transaction in enumerate(parsed.get("transactions", [])):
        for g_idx, group in enumerate(transaction.get("bpr_groups", [])):
 
            # Fix #5 — safe .get() on BPR
            bpr = group.get("BPR", {})
            if not bpr:
                logger.warning(
                    f"EDI835 Parser: BPR group {g_idx} in transaction {t_idx} "
                    f"has no BPR data — skipping."
                )
                continue
 
            bpr02     = bpr.get("BPR02", Decimal("0"))
            plb_total = Decimal("0")
 
            for plb in group.get("PLB", []):
                for adj in plb.get("adjustments", []):
                    plb_total += adj.get("amount", Decimal("0"))
 
            clp_paid_total      = Decimal("0")
            clp_submitted_total = Decimal("0")
 
            for claim in group.get("claims", []):
                # Fix #3 — safe .get() on CLP
                clp = claim.get("CLP", {})
                clp_paid_total      += clp.get("CLP04", Decimal("0"))
                clp_submitted_total += clp.get("CLP03", Decimal("0"))
 
            rows.append({
                "NPI"                : group.get("NPI", ""),
                "TIN"                : group.get("TIN", ""),
                "CheckNumber"        : group.get("CheckNumber", ""),
                "BPR02"              : float(bpr02),
                "plb_total"          : float(plb_total),
                "clp_paid_total"     : float(clp_paid_total),
                "clp_submitted_total": float(clp_submitted_total),
                "bpr_group_idx"      : g_idx,
                "transaction_idx"    : t_idx
            })
 
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    logger.info(f"EDI835 Parser: BPR DataFrame — {len(df)} BPR group(s).")
    return df
 
 
def get_clp_dataframe(parsed: dict) -> pd.DataFrame:
    rows = []
    for t_idx, transaction in enumerate(parsed.get("transactions", [])):
        for g_idx, group in enumerate(transaction.get("bpr_groups", [])):
            npi          = group.get("NPI", "")
            tin          = group.get("TIN", "")
            check_number = group.get("CheckNumber", "")
 
            for claim in group.get("claims", []):
                # Fix #3 — safe .get() on CLP
                clp = claim.get("CLP", {})
                if not clp:
                    logger.warning(
                        f"EDI835 Parser: Claim in BPR group {g_idx} "
                        f"transaction {t_idx} has no CLP data — skipping."
                    )
                    continue
 
                svc_paid_total      = Decimal("0")
                svc_submitted_total = Decimal("0")
                member_paid_total   = Decimal("0")
 
                for svc in claim.get("service_lines", []):
                    # Fix #4 — safe .get() on SVC
                    sv = svc.get("SVC", {})
                    svc_paid_total      += sv.get("SVC03", Decimal("0"))
                    svc_submitted_total += sv.get("SVC02", Decimal("0"))
                    member_paid_total   += svc.get("member_paid", Decimal("0"))
 
                rows.append({
                    "ClaimID"            : clp.get("CLP07", ""),
                    "PatientControlNum"  : clp.get("CLP01", ""),
                    "CLP02"              : clp.get("CLP02", ""),
                    "CLP03"              : float(clp.get("CLP03", Decimal("0"))),
                    "CLP04"              : float(clp.get("CLP04", Decimal("0"))),
                    "svc_paid_total"     : float(svc_paid_total),
                    "svc_submitted_total": float(svc_submitted_total),
                    "member_paid_total"  : float(member_paid_total),
                    "NPI"                : npi,
                    "TIN"                : tin,
                    "CheckNumber"        : check_number,
                    "bpr_group_idx"      : g_idx,
                    "transaction_idx"    : t_idx
                })
 
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    logger.info(f"EDI835 Parser: CLP DataFrame — {len(df)} claim(s).")
    return df

def get_svc_dataframe(parsed: dict) -> pd.DataFrame:
    rows = []
    for t_idx, transaction in enumerate(parsed.get("transactions", [])):
        for g_idx, group in enumerate(transaction.get("bpr_groups", [])):
            npi          = group.get("NPI", "")
            tin          = group.get("TIN", "")
            check_number = group.get("CheckNumber", "")
 
            for claim in group.get("claims", []):
                # Fix #3 — safe .get() on CLP
                clp = claim.get("CLP", {})
                if not clp:
                    logger.warning(
                        f"EDI835 Parser: Claim in BPR group {g_idx} "
                        f"transaction {t_idx} has no CLP data — skipping."
                    )
                    continue
 
                payer_claim_id   = clp.get("CLP07", "")
                patient_ctrl_num = clp.get("CLP01", "")
                clp02            = clp.get("CLP02", "")
 
                for svc_idx, svc in enumerate(claim.get("service_lines", []), start=1):
                    # Fix #4 — safe .get() on SVC
                    sv = svc.get("SVC", {})
                    if not sv:
                        logger.warning(
                            f"EDI835 Parser: Service line {svc_idx} in claim "
                            f"'{payer_claim_id}' has no SVC data — skipping."
                        )
                        continue
 
                    cas_total   = Decimal("0")
                    copay       = Decimal("0")
                    coinsurance = Decimal("0")
                    deductible  = Decimal("0")
 
                    for adj in svc.get("adjustments", []):
                        amt        = adj.get("CAS03", Decimal("0"))
                        cas_total += amt
                        group_code = (adj.get("CAS01", "") or "").upper()
                        reason     = (adj.get("CAS02", "") or "").strip()
                        if group_code == "PR":
                            if reason == "1":
                                deductible  += amt
                            elif reason == "2":
                                coinsurance += amt
                            elif reason == "3":
                                copay       += amt
 
                    rows.append({
                        "ClaimID"          : payer_claim_id,
                        "PatientControlNum": patient_ctrl_num,
                        "CLP02"            : clp02,
                        "line_seq"         : svc_idx,
                        "SVC01"            : sv.get("SVC01", ""),
                        "SVC02"            : float(sv.get("SVC02", Decimal("0"))),
                        "SVC03"            : float(sv.get("SVC03", Decimal("0"))),
                        "cas_total"        : float(cas_total),
                        "member_paid"      : float(svc.get("member_paid", Decimal("0"))),
                        "copay"            : float(copay),
                        "coinsurance"      : float(coinsurance),
                        "deductible"       : float(deductible),
                        "NPI"              : npi,
                        "TIN"              : tin,
                        "CheckNumber"      : check_number,
                        "bpr_group_idx"    : g_idx,
                        "transaction_idx"  : t_idx
                    })
 
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    logger.info(f"EDI835 Parser: SVC DataFrame — {len(df)} service line(s).")
    return df

 
def get_plb_dataframe(parsed: dict) -> pd.DataFrame:
    """
    Returns flat DataFrame — one row per NPI+TIN+CheckNumber combination.
    PLB adjustments summed per payment center.
 
    Columns:
        NPI, TIN, CheckNumber,
        plb_total (sum of all PLB adjustment amounts — signed),
        adjustment_count,
        bpr_group_idx, transaction_idx
    """
    rows = []
 
    for t_idx, transaction in enumerate(parsed.get("transactions", [])):
        for g_idx, group in enumerate(transaction.get("bpr_groups", [])):
 
            npi          = group.get("NPI", "")
            tin          = group.get("TIN", "")
            check_number = group.get("CheckNumber", "")
            plb_total    = Decimal("0")
            adj_count    = 0
 
            for plb in group.get("PLB", []):
                for adj in plb.get("adjustments", []):
                    plb_total += adj.get("amount", Decimal("0"))
                    adj_count += 1
 
            if adj_count > 0:
                rows.append({
                    "NPI"             : npi,
                    "TIN"             : tin,
                    "CheckNumber"     : check_number,
                    "plb_total"       : float(plb_total),
                    "adjustment_count": adj_count,
                    "bpr_group_idx"   : g_idx,
                    "transaction_idx" : t_idx
                })
 
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    logger.info(
        f"EDI835 Parser: PLB DataFrame — "
        f"{len(df)} payment center(s) with PLB adjustments."
    )
    return df
 
 
def parse_835_full(raw_835: str) -> dict:
    """
    Parses raw X12 835 and builds all DataFrames in a single pass.
    Use this instead of calling parse_835 + individual builders separately.
 
    Returns:
    {
        "parsed"       : full parsed dict,
        "bpr_summaries": list of BPR group summaries (for balance checks),
        "bpr_df"       : DataFrame — one row per BPR group (NPI+TIN+CheckNumber),
        "clp_df"       : DataFrame — one row per claim,
        "svc_df"       : DataFrame — one row per service line,
        "plb_df"       : DataFrame — one row per payment center PLB total
    }
    """
    parsed = parse_835(raw_835)
    return {
        "parsed"       : parsed,
        "bpr_summaries": get_bpr_summary(parsed),
        "bpr_df"       : get_bpr_dataframe(parsed),
        "clp_df"       : get_clp_dataframe(parsed),
        "svc_df"       : get_svc_dataframe(parsed),
        "plb_df"       : get_plb_dataframe(parsed)
    }