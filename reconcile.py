"""
reconcile.py (v2)
------------------
Stages (run once per direction: credit-side settlements vs debit-side refunds):
  1. Exact ID match       - order ID found in narration, amount+date within tight tolerance
  2. Fuzzy 1:1 match      - blocked by date-week bucket, then nearest match on amount+date
  3. Split match          - one settlement == sum of 2 unmatched bank rows
  4. Lifecycle match      - settlement adjustments are reconciled as an evidenced group
  5. LLM-assisted match   - batched, only for what's left, only if ANTHROPIC_API_KEY is set
  6. Exception            - everything still unresolved, with a classified reason

Every match also gets confidence-gated into:
  - auto_accepted.csv  (confidence >= AUTO_ACCEPT_THRESHOLD -> safe to post automatically)
  - needs_review.csv   (below threshold -> a human should eyeball it before posting)

Finally, reconcile.py scores itself against data/ground_truth.csv (precision/recall),
which is the honest version of "match rate" - it tells you not just how much you
matched, but how much of what you matched was actually correct.

Run: python reconcile.py
Output: output/auto_accepted.csv, output/needs_review.csv, output/exceptions.csv,
        output/unexplained_bank_credits.csv, output/summary.json
"""

import os
import json
import itertools
from decimal import Decimal
from datetime import datetime
import pandas as pd

AMOUNT_TOL_TIGHT = Decimal("3.0")
AMOUNT_TOL_FUZZY = Decimal("10.0")
AMOUNT_TOL_SPLIT = Decimal("5.0")
DATE_WINDOW_TIGHT = 3
DATE_WINDOW_FUZZY = 7
DATE_WINDOW_SPLIT = 5
AUTO_ACCEPT_THRESHOLD = 0.85
LLM_BATCH_SIZE = 5
LLM_CANDIDATE_WINDOW_DAYS = 15


def D(x):
    return Decimal(str(x)).quantize(Decimal("0.01"))


def parse_bank_ids(value):
    """Normalise one-to-many bank links for scoring and audit output."""
    if value is None or pd.isna(value):
        return set()
    return {item for item in str(value).replace("+", ";").split(";") if item}


def load_data():
    orders = pd.read_csv("data/order_ledger.csv", dtype=str).fillna("")
    settlements = pd.read_csv("data/razorpay_settlement.csv", dtype={"net_amount": str})
    bank = pd.read_csv("data/bank_statement.csv", dtype={"amount": str})
    settlements["settlement_date"] = pd.to_datetime(settlements["settlement_date"])
    bank["value_date"] = pd.to_datetime(bank["value_date"])
    settlements["net_amount_d"] = settlements["net_amount"].apply(D)
    # A foreign-currency processor amount cannot be compared directly to an INR
    # bank amount. `expected_bank_amount` is supplied only when an explicit FX
    # rate/evidence is present; otherwise the normal settlement net is used.
    if "expected_bank_amount" in settlements.columns:
        settlements["recon_amount_d"] = settlements["expected_bank_amount"].replace("", pd.NA).fillna(settlements["net_amount"]).apply(D)
    else:
        settlements["recon_amount_d"] = settlements["net_amount_d"]
    bank["amount_d"] = bank["amount"].apply(D)
    return orders, settlements, bank


def bucket_bank_by_week(bank_pool, window_days):
    """Blocking: group bank rows into buckets so fuzzy matching doesn't compare
    every settlement against every bank row (O(n*m) -> roughly O(n*k))."""
    bucket_size = max(window_days, 1)
    buckets = {}
    epoch = bank_pool["value_date"].min() if not bank_pool.empty else pd.Timestamp("2026-01-01")
    for _, b in bank_pool.iterrows():
        bucket_id = (b["value_date"] - epoch).days // bucket_size
        buckets.setdefault(bucket_id, []).append(b)
    return buckets, epoch, bucket_size


def candidates_from_buckets(buckets, epoch, bucket_size, target_date):
    bucket_id = (target_date - epoch).days // bucket_size
    out = []
    for bid in (bucket_id - 1, bucket_id, bucket_id + 1):
        out.extend(buckets.get(bid, []))
    return out


def stage1_exact_id(settlements, bank, matched_s, matched_b, comparisons):
    matches = []
    for _, s in settlements.iterrows():
        candidates = bank[bank["narration"].str.contains(s["order_id"], na=False)]
        for _, b in candidates.iterrows():
            if b["bank_txn_id"] in matched_b:
                continue
            comparisons[0] += 1
            amount_diff = abs(b["amount_d"] - s["recon_amount_d"])
            date_diff = abs((b["value_date"] - s["settlement_date"]).days)
            if amount_diff <= AMOUNT_TOL_TIGHT and date_diff <= DATE_WINDOW_TIGHT:
                is_fx = pd.notna(s.get("currency")) and str(s.get("currency")) != "INR"
                matches.append({
                    "settlement_id": s["settlement_id"], "order_id": s["order_id"],
                    "match_type": "fx_conversion" if is_fx else "exact_id", "matched_bank_txn_ids": b["bank_txn_id"],
                    "confidence": 0.99, "amount_diff": float(amount_diff),
                    "notes": (f"FX evidence: {s['source_amount']} {s['currency']} at documented rate {s['exchange_rate']} equals ₹{s['recon_amount_d']}; order ID found in narration."
                              if is_fx else "Order ID found in bank narration; amount and date within tolerance."),
                })
                matched_s.add(s["settlement_id"])
                matched_b.add(b["bank_txn_id"])
                break
    return matches


def stage2_fuzzy_blocked(settlements, bank, matched_s, matched_b, comparisons):
    """Same fuzzy logic as v1, but candidates come from date buckets instead of
    a full cross join - this is what keeps it usable at real merchant volume."""
    remaining_s = settlements[~settlements["settlement_id"].isin(matched_s)]
    remaining_b = bank[~bank["bank_txn_id"].isin(matched_b)]
    buckets, epoch, bucket_size = bucket_bank_by_week(remaining_b, DATE_WINDOW_FUZZY)

    pairs = []
    for _, s in remaining_s.iterrows():
        for b in candidates_from_buckets(buckets, epoch, bucket_size, s["settlement_date"]):
            if b["bank_txn_id"] in matched_b:
                continue
            comparisons[0] += 1
            amount_diff = abs(b["amount_d"] - s["recon_amount_d"])
            date_diff = abs((b["value_date"] - s["settlement_date"]).days)
            if amount_diff <= AMOUNT_TOL_FUZZY and date_diff <= DATE_WINDOW_FUZZY:
                cost = float(amount_diff) + date_diff * 2
                pairs.append((cost, s["settlement_id"], s["order_id"], b["bank_txn_id"], amount_diff, date_diff))

    pairs.sort(key=lambda x: x[0])
    matches = []
    for cost, sid, oid, bid, amount_diff, date_diff in pairs:
        if sid in matched_s or bid in matched_b:
            continue
        confidence = round(max(0.55, 1 - (cost / 30)), 2)
        matches.append({
            "settlement_id": sid, "order_id": oid, "match_type": "fuzzy_amount_date",
            "matched_bank_txn_ids": bid, "confidence": confidence, "amount_diff": float(amount_diff),
            "notes": f"No usable ID in narration; matched on amount (Δ₹{amount_diff}) and date (Δ{date_diff}d).",
        })
        matched_s.add(sid)
        matched_b.add(bid)
    return matches


def stage3_split(settlements, bank, matched_s, matched_b, comparisons):
    remaining_s = settlements[~settlements["settlement_id"].isin(matched_s)]
    remaining_b = bank[~bank["bank_txn_id"].isin(matched_b)]
    matches = []
    for _, s in remaining_s.iterrows():
        window = remaining_b[
            (~remaining_b["bank_txn_id"].isin(matched_b)) &
            (abs((remaining_b["value_date"] - s["settlement_date"]).dt.days) <= DATE_WINDOW_SPLIT)
        ]
        rows = window.to_dict("records")
        found = False
        for b1, b2 in itertools.combinations(rows, 2):
            comparisons[0] += 1
            total = b1["amount_d"] + b2["amount_d"]
            diff = abs(total - s["recon_amount_d"])
            if diff <= AMOUNT_TOL_SPLIT:
                matches.append({
                    "settlement_id": s["settlement_id"], "order_id": s["order_id"],
                    "match_type": "split_bank_entries",
                    "matched_bank_txn_ids": f'{b1["bank_txn_id"]}+{b2["bank_txn_id"]}',
                    "confidence": 0.8, "amount_diff": float(diff),
                    "notes": "Settlement amount matches the SUM of two unmatched bank entries within window.",
                })
                matched_s.add(s["settlement_id"])
                matched_b.add(b1["bank_txn_id"])
                matched_b.add(b2["bank_txn_id"])
                found = True
                break
        if found:
            continue
    return matches


def stage4_lifecycle_adjustments(settlements, bank, matched_s, matched_b, comparisons):
    """Resolve a settlement and its explicitly recorded adjustment as one bank
    movement. A difference is *never* treated as an adjustment unless an
    adjustment leg exists for the same order and the arithmetic balances."""
    matches = []
    adjustment_types = {"settlement_adjustment", "cashback_adjustment"}
    for order_id, legs in settlements.groupby("order_id"):
        pending = legs[~legs["settlement_id"].isin(matched_s)]
        adjustments = pending[pending["type"].isin(adjustment_types)]
        primaries = pending[pending["type"].isin(["settlement", "split_payment"])]
        if adjustments.empty or len(primaries) != 1:
            continue
        primary = primaries.iloc[0]
        # Do not aggregate unrelated effects (refunds/chargebacks are separate
        # bank debits and must have their own evidence).
        group = pd.concat([primaries, adjustments])
        expected = sum(group["net_amount_d"], Decimal("0.00"))
        candidates = bank[
            (~bank["bank_txn_id"].isin(matched_b)) &
            (bank["amount_d"] > 0) &
            (abs((bank["value_date"] - primary["settlement_date"]).dt.days) <= DATE_WINDOW_TIGHT)
        ]
        for _, b in candidates.iterrows():
            comparisons[0] += 1
            diff = abs(b["amount_d"] - expected)
            if diff > AMOUNT_TOL_TIGHT:
                continue
            linked_ids = "+".join(group["settlement_id"])
            for _, leg in group.iterrows():
                role = "base settlement" if leg["settlement_id"] == primary["settlement_id"] else leg["type"].replace("_", " ")
                matches.append({
                    "settlement_id": leg["settlement_id"], "order_id": order_id,
                    "match_type": "lifecycle_adjustment", "matched_bank_txn_ids": b["bank_txn_id"],
                    "confidence": 0.98, "amount_diff": float(diff),
                    "notes": f"Evidenced lifecycle group ({linked_ids}): {role}; ₹{primary['net_amount_d']} + adjustments = ₹{expected}, matching bank credit.",
                })
                matched_s.add(leg["settlement_id"])
            matched_b.add(b["bank_txn_id"])
            break
    return matches


def detect_ledger_duplicates(orders, settlements):
    """Flag repeated internal entries rather than inventing a missing payout."""
    exceptions = []
    for order_id, rows in orders.groupby("order_id"):
        if len(rows) < 2:
            continue
        processor_legs = settlements[(settlements["order_id"] == order_id) &
                                      (settlements["type"].isin(["settlement", "split_payment"]))]
        if len(processor_legs) >= 1:
            extras = len(rows) - 1
            exceptions.append({
                "entity_type": "ledger_record", "settlement_id": "", "order_id": order_id,
                "net_amount": rows.iloc[-1]["gross_amount"], "settlement_date": rows.iloc[-1]["order_date"],
                "reason": f"Duplicate ledger entry detected: {len(rows)} internal records but {len(processor_legs)} processor payment leg(s). The extra {extras} ledger record(s) were not treated as missing money.",
            })
    return exceptions


def stage4_llm_batched(settlements, bank, matched_s, matched_b):
    """Use the model only to surface a *review candidate*. It can never post a
    transaction automatically: output is constrained to supplied IDs, checked
    again locally, and deliberately capped below the auto-accept threshold."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    remaining_s = settlements[~settlements["settlement_id"].isin(matched_s)]
    if not api_key or remaining_s.empty:
        return []

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed - skipping stage 4 (pip install anthropic).")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    matches = []
    rows = list(remaining_s.iterrows())

    for i in range(0, len(rows), LLM_BATCH_SIZE):
        batch = rows[i:i + LLM_BATCH_SIZE]
        cases = []
        for _, s in batch:
            remaining_b = bank[~bank["bank_txn_id"].isin(matched_b)]
            window = remaining_b[
                abs((remaining_b["value_date"] - s["settlement_date"]).dt.days) <= LLM_CANDIDATE_WINDOW_DAYS
            ]
            if window.empty:
                continue
            cases.append({
                "settlement_id": s["settlement_id"],
                "order_id": s["order_id"],
                "reconciliation_amount": str(s["recon_amount_d"]),
                "settlement_date": s["settlement_date"].date().isoformat(),
                "candidates": [
                    {"bank_txn_id": b["bank_txn_id"], "amount": str(b["amount_d"]),
                     "value_date": b["value_date"].date().isoformat(), "narration": b["narration"]}
                    for _, b in window.iterrows()
                ],
            })
        if not cases:
            continue

        prompt = f"""You are a payments reconciliation analyst. Below is a batch of settlement
rows that could not be automatically matched to a bank statement line, each with its own
list of nearby candidate bank rows. Return a candidate ONLY where the supplied narration,
date, and arithmetic provide direct evidence. Do not infer facts or invent IDs. If evidence
is incomplete or ambiguous, return match: null.

Cases:
{json.dumps(cases, indent=2)}

Respond ONLY with a JSON array, one object per settlement_id, no other text:
[{{"settlement_id": "...", "match": "<bank_txn_id or null>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}]"""

        try:
            response = client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"), max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
        except Exception as e:
            print(f"LLM batch call failed: {e}")
            continue

        case_map = {case["settlement_id"]: case for case in cases}
        for item in parsed:
            case = case_map.get(item.get("settlement_id"))
            if not case or item.get("match") not in {c["bank_txn_id"] for c in case["candidates"]}:
                continue
            if item.get("match") and item.get("confidence", 0) >= 0.6 and item["match"] not in matched_b:
                sid = item["settlement_id"]
                oid = settlements.loc[settlements["settlement_id"] == sid, "order_id"].values[0]
                matches.append({
                    "settlement_id": sid, "order_id": oid, "match_type": "llm_review_candidate",
                    "matched_bank_txn_ids": item["match"], "confidence": min(float(item["confidence"]), 0.79),
                    "amount_diff": None, "notes": f"AI-proposed evidence candidate — requires human approval, never auto-posted: {item.get('reason', '')}",
                })
                matched_s.add(sid)
                matched_b.add(item["match"])

    return matches


def classify_exception(s, bank):
    explicit = {
        "duplicate_settlement": "Duplicate processor webhook has no independent bank credit. Left unresolved rather than matched to the original payout.",
        "settlement_adjustment": "Settlement adjustment has no balancing base-settlement and bank-credit evidence. Manual review required.",
        "chargeback": "Chargeback has no evidenced matching bank debit. Manual review required; it was not assumed to be a refund.",
        "cashback_adjustment": "Additional credit has no evidenced bank movement. Manual review required.",
    }
    if s["type"] in explicit:
        return explicit[s["type"]]
    nearby = bank[abs((bank["value_date"] - s["settlement_date"]).dt.days) <= 15]
    if nearby.empty:
        leg = "refund debit" if s["net_amount_d"] < 0 else "settlement credit"
        return f"No bank {('debit' if s['net_amount_d'] < 0 else 'credit')} found within 15 days - " \
               f"possible delayed or failed {leg}."
    return "Unresolved - closest bank candidates fall outside amount/date tolerance. Needs manual review."


def score_against_ground_truth(matches_df, exceptions_df):
    """The honest metric: precision & recall against the held-out answer key,
    not just 'how many settlements got some match'."""
    if not os.path.exists("data/ground_truth.csv"):
        return None
    gt = pd.read_csv("data/ground_truth.csv", dtype=str).fillna("")
    gt["should_match"] = gt["should_match"].astype(str).str.lower() == "true"
    gt_map = {row["settlement_id"]: row for _, row in gt.iterrows()}

    predicted = {}
    if not matches_df.empty:
        for _, m in matches_df.iterrows():
            predicted[m["settlement_id"]] = parse_bank_ids(m["matched_bank_txn_ids"])

    tp = fp = fn = tn = 0
    for sid, row in gt_map.items():
        true_ids = parse_bank_ids(row["true_bank_txn_ids"])
        should_match = row["should_match"]
        pred_ids = predicted.get(sid)

        if should_match:
            if pred_ids and pred_ids == true_ids:
                tp += 1
            elif pred_ids and pred_ids != true_ids:
                fp += 1  # matched, but to the wrong bank row(s)
                fn += 1  # ... which also means the true match was missed
            else:
                fn += 1  # left as an exception when it should have matched
        else:
            if pred_ids:
                fp += 1  # matched something that truly has no counterpart (duplicate/missing)
            else:
                tn += 1  # correctly left unmatched

    precision = round(tp / (tp + fp), 3) if (tp + fp) else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) else None

    # precision specifically within the auto-accepted subset - this is the number
    # that matters most, since those matches get posted without human review
    auto_df = matches_df[matches_df["confidence"] >= AUTO_ACCEPT_THRESHOLD] if not matches_df.empty else matches_df
    auto_tp = auto_fp = 0
    for _, m in auto_df.iterrows():
        sid = m["settlement_id"]
        row = gt_map.get(sid)
        if row is None:
            continue
        true_ids = parse_bank_ids(row["true_bank_txn_ids"])
        pred_ids = parse_bank_ids(m["matched_bank_txn_ids"])
        if row["should_match"] and pred_ids == true_ids:
            auto_tp += 1
        else:
            auto_fp += 1
    auto_precision = round(auto_tp / (auto_tp + auto_fp), 3) if (auto_tp + auto_fp) else None

    return {
        "true_positives": tp, "false_positives": fp, "false_negatives": fn, "true_negatives": tn,
        "precision": precision, "recall": recall,
        "auto_accept_precision": auto_precision, "auto_accept_count": len(auto_df),
    }


def main():
    orders, settlements, bank = load_data()
    matched_s, matched_b = set(), set()
    comparisons = [0]
    all_matches = []

    for direction, s_filter, b_filter in [
        ("credit", settlements["net_amount_d"] > 0, bank["amount_d"] > 0),
        ("debit", settlements["net_amount_d"] < 0, bank["amount_d"] < 0),
    ]:
        s_pool = settlements[s_filter]
        b_pool = bank[b_filter]
        all_matches += stage1_exact_id(s_pool, b_pool, matched_s, matched_b, comparisons)
        all_matches += stage2_fuzzy_blocked(s_pool, b_pool, matched_s, matched_b, comparisons)
        all_matches += stage3_split(s_pool, b_pool, matched_s, matched_b, comparisons)

    all_matches += stage4_lifecycle_adjustments(settlements, bank, matched_s, matched_b, comparisons)
    all_matches += stage4_llm_batched(settlements, bank, matched_s, matched_b)

    matched_df = pd.DataFrame(all_matches)
    auto_df = matched_df[matched_df["confidence"] >= AUTO_ACCEPT_THRESHOLD] if not matched_df.empty else matched_df
    review_df = matched_df[matched_df["confidence"] < AUTO_ACCEPT_THRESHOLD] if not matched_df.empty else matched_df

    unmatched = settlements[~settlements["settlement_id"].isin(matched_s)].copy()
    exceptions = [{
        "entity_type": "settlement", "settlement_id": s["settlement_id"], "order_id": s["order_id"],
        "net_amount": str(s["net_amount_d"]), "settlement_date": s["settlement_date"].date().isoformat(),
        "reason": classify_exception(s, bank),
    } for _, s in unmatched.iterrows()]
    exceptions.extend(detect_ledger_duplicates(orders, settlements))
    exceptions_df = pd.DataFrame(exceptions)

    unexplained_bank = bank[~bank["bank_txn_id"].isin(matched_b)].copy()

    os.makedirs("output", exist_ok=True)
    auto_df.to_csv("output/auto_accepted.csv", index=False)
    review_df.to_csv("output/needs_review.csv", index=False)
    exceptions_df.to_csv("output/exceptions.csv", index=False)
    unexplained_bank.drop(columns=["amount_d"], errors="ignore").to_csv(
        "output/unexplained_bank_credits.csv", index=False)

    total = len(settlements)
    matched_count = len(matched_df)
    match_rate = round(100 * matched_count / total, 1) if total else 0
    breakdown = matched_df["match_type"].value_counts().to_dict() if not matched_df.empty else {}
    scoring = score_against_ground_truth(matched_df, exceptions_df)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "total_settlements_considered": total,
        "matched_count": matched_count,
        "match_rate_pct": match_rate,
        "match_type_breakdown": breakdown,
        "auto_accepted_count": len(auto_df),
        "needs_review_count": len(review_df),
        "exception_count": len(exceptions_df),
        "unexplained_bank_credit_count": len(unexplained_bank),
        "llm_assisted_used": os.environ.get("ANTHROPIC_API_KEY") is not None,
        "pairwise_comparisons_made": comparisons[0],
        "ground_truth_scoring": scoring,
    }
    with open("output/summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print("\nWritten: output/auto_accepted.csv, output/needs_review.csv, output/exceptions.csv, "
          "output/unexplained_bank_credits.csv, output/summary.json")


if __name__ == "__main__":
    main()
