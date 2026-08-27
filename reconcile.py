"""
reconcile.py
------------
Matches Razorpay settlement rows against bank statement rows in staged passes,
each pass more expensive / less certain than the last. Anything that clears
no stage honestly, with a reason, instead of being force-matched.

Stages:
  1. Exact ID match       - order ID found in narration, amount+date within tight tolerance
  2. Fuzzy 1:1 match      - greedy nearest match on amount + date, within tolerance window
  3. Split match          - one settlement == sum of 2 unmatched bank rows (refund splits etc.)
  4. LLM-assisted match   - only for what's left, only if ANTHROPIC_API_KEY is set
  5. Exception            - everything still unresolved, with a classified reason

Run: python reconcile.py
Output: ./output/reconciliation_report.csv, ./output/exceptions.csv, ./output/summary.json
"""

import os
import json
import itertools
from datetime import datetime
import pandas as pd

AMOUNT_TOL_TIGHT = 3.0      # rupees, for exact-ID stage
AMOUNT_TOL_FUZZY = 10.0     # rupees, for fuzzy stage
AMOUNT_TOL_SPLIT = 5.0      # rupees, for split-sum stage
DATE_WINDOW_TIGHT = 3       # days
DATE_WINDOW_FUZZY = 7       # days
DATE_WINDOW_SPLIT = 5       # days

def load_data():
    settlements = pd.read_csv("data/razorpay_settlement.csv")
    bank = pd.read_csv("data/bank_statement.csv")
    settlements["settlement_date"] = pd.to_datetime(settlements["settlement_date"])
    bank["value_date"] = pd.to_datetime(bank["value_date"])
    return settlements, bank


def stage1_exact_id(settlements, bank):
    """Match settlements whose order_id appears in a bank narration."""
    matches = []
    matched_settlement_ids = set()
    matched_bank_ids = set()

    for _, s in settlements.iterrows():
        if s["net_amount"] <= 0:
            continue  # refunds/negative legs aren't credited to the bank the same way; skip in v1
        candidates = bank[bank["narration"].str.contains(s["order_id"], na=False)]
        for _, b in candidates.iterrows():
            if b["bank_txn_id"] in matched_bank_ids:
                continue
            amount_diff = abs(b["amount"] - s["net_amount"])
            date_diff = abs((b["value_date"] - s["settlement_date"]).days)
            if amount_diff <= AMOUNT_TOL_TIGHT and date_diff <= DATE_WINDOW_TIGHT:
                matches.append({
                    "settlement_id": s["settlement_id"],
                    "order_id": s["order_id"],
                    "match_type": "exact_id",
                    "matched_bank_txn_ids": b["bank_txn_id"],
                    "confidence": 0.99,
                    "amount_diff": round(amount_diff, 2),
                    "notes": "Order ID found in bank narration; amount and date within tolerance.",
                })
                matched_settlement_ids.add(s["settlement_id"])
                matched_bank_ids.add(b["bank_txn_id"])
                break
    return matches, matched_settlement_ids, matched_bank_ids


def stage2_fuzzy(settlements, bank, matched_settlement_ids, matched_bank_ids):
    """Greedy nearest-match on amount + date for whatever stage 1 missed."""
    matches = []
    remaining_settlements = settlements[
        (~settlements["settlement_id"].isin(matched_settlement_ids)) & (settlements["net_amount"] > 0)
    ]
    remaining_bank = bank[~bank["bank_txn_id"].isin(matched_bank_ids)]

    candidate_pairs = []
    for _, s in remaining_settlements.iterrows():
        for _, b in remaining_bank.iterrows():
            amount_diff = abs(b["amount"] - s["net_amount"])
            date_diff = abs((b["value_date"] - s["settlement_date"]).days)
            if amount_diff <= AMOUNT_TOL_FUZZY and date_diff <= DATE_WINDOW_FUZZY:
                cost = amount_diff + date_diff * 2
                candidate_pairs.append((cost, s["settlement_id"], b["bank_txn_id"], amount_diff, date_diff))

    candidate_pairs.sort(key=lambda x: x[0])  # greedy: best (lowest cost) pairs first
    used_settlements, used_bank = set(), set()
    for cost, sid, bid, amount_diff, date_diff in candidate_pairs:
        if sid in used_settlements or bid in used_bank:
            continue
        confidence = max(0.55, round(1 - (cost / 30), 2))
        matches.append({
            "settlement_id": sid,
            "order_id": settlements.loc[settlements["settlement_id"] == sid, "order_id"].values[0],
            "match_type": "fuzzy_amount_date",
            "matched_bank_txn_ids": bid,
            "confidence": confidence,
            "amount_diff": round(amount_diff, 2),
            "notes": f"No order ID in narration; matched on amount (Δ₹{amount_diff:.2f}) and date (Δ{date_diff}d).",
        })
        used_settlements.add(sid)
        used_bank.add(bid)

    matched_settlement_ids |= used_settlements
    matched_bank_ids |= used_bank
    return matches, matched_settlement_ids, matched_bank_ids


def stage3_split(settlements, bank, matched_settlement_ids, matched_bank_ids):
    """Check if an unmatched settlement equals the sum of two unmatched bank rows."""
    matches = []
    remaining_settlements = settlements[
        (~settlements["settlement_id"].isin(matched_settlement_ids)) & (settlements["net_amount"] > 0)
    ]
    remaining_bank = bank[~bank["bank_txn_id"].isin(matched_bank_ids)]

    used_bank = set()
    for _, s in remaining_settlements.iterrows():
        window = remaining_bank[
            (~remaining_bank["bank_txn_id"].isin(used_bank)) &
            (abs((remaining_bank["value_date"] - s["settlement_date"]).dt.days) <= DATE_WINDOW_SPLIT)
        ]
        found = False
        for b1, b2 in itertools.combinations(window.to_dict("records"), 2):
            total = b1["amount"] + b2["amount"]
            if abs(total - s["net_amount"]) <= AMOUNT_TOL_SPLIT:
                matches.append({
                    "settlement_id": s["settlement_id"],
                    "order_id": s["order_id"],
                    "match_type": "split_bank_entries",
                    "matched_bank_txn_ids": f'{b1["bank_txn_id"]}+{b2["bank_txn_id"]}',
                    "confidence": 0.8,
                    "amount_diff": round(abs(total - s["net_amount"]), 2),
                    "notes": "Settlement amount matches the SUM of two unmatched bank entries within window.",
                })
                matched_settlement_ids.add(s["settlement_id"])
                used_bank.add(b1["bank_txn_id"])
                used_bank.add(b2["bank_txn_id"])
                found = True
                break
        if found:
            continue

    matched_bank_ids |= used_bank
    return matches, matched_settlement_ids, matched_bank_ids


def stage4_llm_assisted(settlements, bank, matched_settlement_ids, matched_bank_ids):
    """
    Optional: for anything still unmatched, ask an LLM to reason about plausible
    matches within a wider window. Skipped gracefully if no API key is set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    remaining_settlements = settlements[
        (~settlements["settlement_id"].isin(matched_settlement_ids)) & (settlements["net_amount"] > 0)
    ]
    remaining_bank = bank[~bank["bank_txn_id"].isin(matched_bank_ids)]

    if not api_key or remaining_settlements.empty:
        return [], matched_settlement_ids, matched_bank_ids

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed - skipping stage 4 (pip install anthropic).")
        return [], matched_settlement_ids, matched_bank_ids

    client = anthropic.Anthropic(api_key=api_key)
    matches = []

    for _, s in remaining_settlements.iterrows():
        window = remaining_bank[
            (abs((remaining_bank["value_date"] - s["settlement_date"]).dt.days) <= 15)
        ]
        if window.empty:
            continue

        prompt = f"""You are a payments reconciliation analyst. A merchant's Razorpay settlement row
could not be automatically matched to a bank statement line. Decide if any of the candidate
bank rows below plausibly correspond to this settlement, accounting for fee rounding, delayed
credit, or partial/split payments.

Settlement:
  settlement_id: {s['settlement_id']}
  order_id: {s['order_id']}
  net_amount: {s['net_amount']}
  settlement_date: {s['settlement_date'].date()}

Candidate bank rows:
{window[['bank_txn_id','amount','value_date','narration']].to_string(index=False)}

Respond ONLY with JSON, no other text:
{{"match": "<bank_txn_id or null>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            text = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
        except Exception as e:
            print(f"LLM call failed for {s['settlement_id']}: {e}")
            continue

        if parsed.get("match") and parsed.get("confidence", 0) >= 0.6:
            matches.append({
                "settlement_id": s["settlement_id"],
                "order_id": s["order_id"],
                "match_type": "llm_assisted",
                "matched_bank_txn_ids": parsed["match"],
                "confidence": parsed["confidence"],
                "amount_diff": None,
                "notes": f"LLM reasoning: {parsed.get('reason', '')}",
            })
            matched_settlement_ids.add(s["settlement_id"])
            matched_bank_ids.add(parsed["match"])

    return matches, matched_settlement_ids, matched_bank_ids


def classify_exception(s, bank, matched_bank_ids, all_settlements):
    """Give every unresolved settlement an honest, specific reason - never a generic 'unmatched'."""
    if s["net_amount"] <= 0:
        return "Refund/partial-refund leg - not reconciled against bank credits in v1 (needs debit-side matching)."

    siblings = all_settlements[
        (all_settlements["order_id"] == s["order_id"]) &
        (all_settlements["type"].isin(["settlement", "duplicate_settlement"]))
    ]
    if len(siblings) > 1:
        return "Likely duplicate settlement webhook for this order - only one bank credit expected, none left to assign."

    nearby_bank = bank[abs((bank["value_date"] - s["settlement_date"]).dt.days) <= 15]
    if nearby_bank.empty:
        return "No bank credit found within 15 days of settlement date - possible delayed or failed payout."

    return "Unresolved - closest bank candidates fall outside amount/date tolerance. Needs manual review."


def main():
    settlements, bank = load_data()

    all_matches = []
    matched_settlement_ids, matched_bank_ids = set(), set()

    m1, matched_settlement_ids, matched_bank_ids = stage1_exact_id(settlements, bank)
    all_matches += m1

    m2, matched_settlement_ids, matched_bank_ids = stage2_fuzzy(
        settlements, bank, matched_settlement_ids, matched_bank_ids)
    all_matches += m2

    m3, matched_settlement_ids, matched_bank_ids = stage3_split(
        settlements, bank, matched_settlement_ids, matched_bank_ids)
    all_matches += m3

    m4, matched_settlement_ids, matched_bank_ids = stage4_llm_assisted(
        settlements, bank, matched_settlement_ids, matched_bank_ids)
    all_matches += m4

    matched_df = pd.DataFrame(all_matches)

    unmatched = settlements[~settlements["settlement_id"].isin(matched_settlement_ids)].copy()
    exceptions = []
    for _, s in unmatched.iterrows():
        exceptions.append({
            "settlement_id": s["settlement_id"],
            "order_id": s["order_id"],
            "net_amount": s["net_amount"],
            "settlement_date": s["settlement_date"].date().isoformat(),
            "reason": classify_exception(s, bank, matched_bank_ids, settlements),
        })
    exceptions_df = pd.DataFrame(exceptions)

    unexplained_bank = bank[~bank["bank_txn_id"].isin(matched_bank_ids)].copy()
    unexplained_bank = unexplained_bank[~unexplained_bank["narration"].str.contains("UNKNOWN", na=False) | True]

    os.makedirs("output", exist_ok=True)
    matched_df.to_csv("output/reconciliation_report.csv", index=False)
    exceptions_df.to_csv("output/exceptions.csv", index=False)
    unexplained_bank.to_csv("output/unexplained_bank_credits.csv", index=False)

    total_settlements = len(settlements[settlements["net_amount"] > 0])
    matched_count = len(matched_df)
    match_rate = round(100 * matched_count / total_settlements, 1) if total_settlements else 0

    breakdown = matched_df["match_type"].value_counts().to_dict() if not matched_df.empty else {}

    summary = {
        "generated_at": datetime.now().isoformat(),
        "total_settlements_considered": total_settlements,
        "matched_count": matched_count,
        "match_rate_pct": match_rate,
        "match_type_breakdown": breakdown,
        "exception_count": len(exceptions_df),
        "unexplained_bank_credit_count": len(unexplained_bank),
        "llm_assisted_used": os.environ.get("ANTHROPIC_API_KEY") is not None,
    }
    with open("output/summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print("\nWritten: output/reconciliation_report.csv, output/exceptions.csv, "
          "output/unexplained_bank_credits.csv, output/summary.json")


if __name__ == "__main__":
    main()