"""
generate_data.py (v2)
----------------------
Adds three things over v1:
  1. Ground truth: every bank row is tagged with the settlement it's REALLY
     tied to (true_settlement_id), and a separate ground_truth.csv records,
     for every settlement, whether it should match anything at all and to
     which bank row(s). This lets reconcile.py report actual precision/recall,
     not just "% matched".
  2. Debit-side rows: refunds and partial refunds now generate a matching
     bank DEBIT row (negative amount), so the engine has something real to
     match refund settlement legs against instead of dumping them all into
     exceptions.
  3. Decimal money math throughout, so any rounding mismatch you see in the
     output is one we deliberately injected — not an artifact of float math.

Run: python generate_data.py
Output: ./data/order_ledger.csv, ./data/razorpay_settlement.csv,
        ./data/bank_statement.csv, ./data/ground_truth.csv
"""

import random
import os
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
import pandas as pd

random.seed(42)

N_ORDERS = 70
START_DATE = datetime(2026, 7, 1)

CUSTOMERS = [
    "Ananya Rao", "Vikram Nair", "Priya Menon", "Rahul Iyer", "Sneha Pillai",
    "Arjun Kumar", "Divya Krishnan", "Karthik Subramanian", "Meera Pillai",
    "Rohan Varma", "Lakshmi Narayan", "Suresh Babu", "Anjali Gupta", "Nikhil Shah",
]

def D(x):
    """Convert to a 2-decimal-place Decimal. All money math goes through this."""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def rand_date(base, max_offset_days=45):
    return base + timedelta(days=random.randint(0, max_offset_days))

os.makedirs("data", exist_ok=True)
orders, settlements, bank_rows, ground_truth = [], [], [], []

order_counter, settlement_counter, bank_counter = 1000, 5000, 9000

for i in range(N_ORDERS):
    order_counter += 1
    order_id = f"ORD{order_counter}"
    customer = random.choice(CUSTOMERS)
    gross_amount = D(random.choice([249, 499, 999, 1499, 1999, 2999, 4999]) + random.uniform(-2, 2))
    order_date = rand_date(START_DATE)

    r = random.random()
    status = "refunded" if r < 0.08 else "partial_refund" if r < 0.14 else "paid"

    orders.append({
        "order_id": order_id, "customer": customer, "gross_amount": str(gross_amount),
        "order_date": order_date.strftime("%Y-%m-%d"), "status": status,
    })

    # ---- main settlement leg ----
    fee = D(gross_amount * Decimal("0.02"))
    tax_on_fee = D(fee * Decimal("0.18"))
    net_amount = D(gross_amount - fee - tax_on_fee)

    delay_days = random.choice([7, 8, 9]) if random.random() < 0.15 else random.choice([1, 1, 1, 2, 2])
    settlement_date = order_date + timedelta(days=delay_days)

    settlement_counter += 1
    settlement_id = f"SETL{settlement_counter}"
    settlements.append({
        "settlement_id": settlement_id, "order_id": order_id,
        "gross_amount": str(gross_amount), "fee": str(fee), "tax": str(tax_on_fee),
        "net_amount": str(net_amount), "settlement_date": settlement_date.strftime("%Y-%m-%d"),
        "type": "settlement",
    })

    # inject: duplicate webhook -> a second settlement row that NO bank credit will ever back
    is_duplicate_case = random.random() < 0.06
    if is_duplicate_case:
        settlement_counter += 1
        dup_id = f"SETL{settlement_counter}"
        settlements.append({
            "settlement_id": dup_id, "order_id": order_id,
            "gross_amount": str(gross_amount), "fee": str(fee), "tax": str(tax_on_fee),
            "net_amount": str(net_amount), "settlement_date": settlement_date.strftime("%Y-%m-%d"),
            "type": "duplicate_settlement",
        })
        ground_truth.append({"settlement_id": dup_id, "true_bank_txn_ids": "", "should_match": False,
                              "reason": "duplicate webhook - no bank credit exists for this row"})

    # ---- refund / partial refund legs (debit side) ----
    refund_amount = None
    if status == "refunded":
        refund_amount = gross_amount
    elif status == "partial_refund":
        refund_amount = D(gross_amount * Decimal(str(round(random.uniform(0.2, 0.5), 2))))

    if refund_amount is not None:
        settlement_counter += 1
        refund_settlement_id = f"SETL{settlement_counter}"
        refund_date = settlement_date + timedelta(days=random.randint(1, 4))
        settlements.append({
            "settlement_id": refund_settlement_id, "order_id": order_id,
            "gross_amount": str(-refund_amount), "fee": "0", "tax": "0",
            "net_amount": str(-refund_amount), "settlement_date": refund_date.strftime("%Y-%m-%d"),
            "type": "refund" if status == "refunded" else "partial_refund",
        })

        # does the refund debit actually clear the bank, or is it also stuck? (~10% missing)
        refund_missing = random.random() < 0.10
        if refund_missing:
            ground_truth.append({"settlement_id": refund_settlement_id, "true_bank_txn_ids": "",
                                  "should_match": False, "reason": "refund debit never cleared the bank"})
        else:
            bank_amount = -refund_amount
            if random.random() < 0.10:  # rounding noise on refunds too
                bank_amount = D(bank_amount + Decimal(random.choice(["-1.50", "1.25", "-0.75"])))
            bank_counter += 1
            debit_id = f"BANK{bank_counter}"
            include_id = random.random() < 0.6
            narration = (f"NEFT/RAZORPAY/REFUND/{order_id}" if include_id else "RZRPAY REFUND DR")
            bank_rows.append({
                "bank_txn_id": debit_id, "amount": str(bank_amount),
                "value_date": (refund_date + timedelta(days=random.randint(0, 2))).strftime("%Y-%m-%d"),
                "narration": narration, "true_settlement_id": refund_settlement_id,
            })
            ground_truth.append({"settlement_id": refund_settlement_id, "true_bank_txn_ids": debit_id,
                                  "should_match": True, "reason": ""})

    # ---- bank credit leg(s) for the MAIN settlement ----
    missing_case = random.random() < 0.07
    split_case = (not missing_case) and status == "partial_refund" and random.random() < 0.5
    rounding_noise_case = (not missing_case) and random.random() < 0.12

    if missing_case:
        ground_truth.append({"settlement_id": settlement_id, "true_bank_txn_ids": "",
                              "should_match": False, "reason": "settlement never credited to bank"})
    else:
        bank_amount = net_amount
        if rounding_noise_case:
            bank_amount = D(bank_amount + Decimal(random.choice(["-2.50", "-1.75", "1.10", "2.00", "-0.90"])))

        bank_date = settlement_date + timedelta(days=random.choice([0, 0, 1, 1, 2]))
        include_id = random.random() < 0.65
        narration = f"NEFT/RAZORPAY/{order_id}/SETL" if include_id else "RZRPAY SETTLEMENT CR"

        if split_case:
            part1 = D(bank_amount * Decimal(str(round(random.uniform(0.4, 0.6), 2))))
            part2 = D(bank_amount - part1)
            ids = []
            for part in (part1, part2):
                bank_counter += 1
                bid = f"BANK{bank_counter}"
                ids.append(bid)
                bank_rows.append({
                    "bank_txn_id": bid, "amount": str(part),
                    "value_date": (bank_date + timedelta(days=random.randint(0, 1))).strftime("%Y-%m-%d"),
                    "narration": narration + " (split)", "true_settlement_id": settlement_id,
                })
            ground_truth.append({"settlement_id": settlement_id, "true_bank_txn_ids": ";".join(ids),
                                  "should_match": True, "reason": ""})
        else:
            bank_counter += 1
            bid = f"BANK{bank_counter}"
            bank_rows.append({
                "bank_txn_id": bid, "amount": str(bank_amount),
                "value_date": bank_date.strftime("%Y-%m-%d"),
                "narration": narration, "true_settlement_id": settlement_id,
            })
            ground_truth.append({"settlement_id": settlement_id, "true_bank_txn_ids": bid,
                                  "should_match": True, "reason": ""})

# a few unexplained bank credits with no ground-truth settlement behind them at all
for _ in range(3):
    bank_counter += 1
    bank_rows.append({
        "bank_txn_id": f"BANK{bank_counter}", "amount": str(D(random.uniform(100, 800))),
        "value_date": rand_date(START_DATE).strftime("%Y-%m-%d"),
        "narration": "UNKNOWN CREDIT - BANK REF", "true_settlement_id": "",
    })

pd.DataFrame(orders).to_csv("data/order_ledger.csv", index=False)
pd.DataFrame(settlements).to_csv("data/razorpay_settlement.csv", index=False)
pd.DataFrame(bank_rows).to_csv("data/bank_statement.csv", index=False)
pd.DataFrame(ground_truth).to_csv("data/ground_truth.csv", index=False)

print(f"Generated {len(orders)} orders, {len(settlements)} settlement rows, "
      f"{len(bank_rows)} bank rows, {len(ground_truth)} ground-truth labels")
print("Files written to ./data/  (ground_truth.csv is the held-out answer key — "
      "reconcile.py never reads it while matching, only when scoring itself)")
