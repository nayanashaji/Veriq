"""
generate_data.py
-----------------
Creates 3 synthetic CSV sources that a real merchant would have to reconcile:

  1. order_ledger.csv       - the merchant's internal source of truth
  2. razorpay_settlement.csv - what Razorpay SAYS it settled
  3. bank_statement.csv      - what actually landed in the bank

Deliberately injects the mismatch patterns that make reconciliation hard:
  - delayed settlements (bank credit arrives days later)
  - rounding / fee-recalculation differences (bank amount != stated net amount)
  - duplicate settlement rows (double webhook, only paid once)
  - split bank credits (one settlement split across two bank lines)
  - missing bank entries (settlement recorded, money never arrived)
  - narration noise (order ID sometimes present in bank narration, sometimes not)

Run: python generate_data.py
Output: ./data/order_ledger.csv, ./data/razorpay_settlement.csv, ./data/bank_statement.csv
"""

import random
import pandas as pd
from datetime import datetime, timedelta

random.seed(42)

N_ORDERS = 70
START_DATE = datetime(2026, 7, 1)

CUSTOMERS = [
    "Ananya Rao", "Vikram Nair", "Priya Menon", "Rahul Iyer", "Sneha Pillai",
    "Arjun Kumar", "Divya Krishnan", "Karthik Subramanian", "Meera Pillai",
    "Rohan Varma", "Lakshmi Narayan", "Suresh Babu", "Anjali Gupta", "Nikhil Shah",
]

def rand_date(base, max_offset_days=45):
    return base + timedelta(days=random.randint(0, max_offset_days))

def money(x):
    return round(x, 2)

orders = []
settlements = []
bank_rows = []

order_counter = 1000
settlement_counter = 5000
bank_counter = 9000

for i in range(N_ORDERS):
    order_counter += 1
    order_id = f"ORD{order_counter}"
    customer = random.choice(CUSTOMERS)
    gross_amount = money(random.choice([249, 499, 999, 1499, 1999, 2999, 4999]) + random.uniform(-2, 2))
    order_date = rand_date(START_DATE)

    r = random.random()
    if r < 0.08:
        status = "refunded"
    elif r < 0.14:
        status = "partial_refund"
    else:
        status = "paid"

    orders.append({
        "order_id": order_id,
        "customer": customer,
        "gross_amount": gross_amount,
        "order_date": order_date.strftime("%Y-%m-%d"),
        "status": status,
    })

    # ---- Razorpay settlement leg(s) for this order ----
    fee = money(gross_amount * 0.02)
    tax_on_fee = money(fee * 0.18)
    net_amount = money(gross_amount - fee - tax_on_fee)

    delay_days = random.choice([1, 1, 1, 2, 2] + [7, 8, 9] if random.random() < 0.15 else [1, 1, 2])
    settlement_date = order_date + timedelta(days=delay_days)

    settlement_counter += 1
    settlement_id = f"SETL{settlement_counter}"
    settlements.append({
        "settlement_id": settlement_id,
        "order_id": order_id,
        "gross_amount": gross_amount,
        "fee": fee,
        "tax": tax_on_fee,
        "net_amount": net_amount,
        "settlement_date": settlement_date.strftime("%Y-%m-%d"),
        "type": "settlement",
    })

    # inject: ~5% duplicate webhook -> extra settlement row, but bank pays only once
    is_duplicate_case = random.random() < 0.06
    if is_duplicate_case:
        settlement_counter += 1
        settlements.append({
            "settlement_id": f"SETL{settlement_counter}",
            "order_id": order_id,
            "gross_amount": gross_amount,
            "fee": fee,
            "tax": tax_on_fee,
            "net_amount": net_amount,
            "settlement_date": settlement_date.strftime("%Y-%m-%d"),
            "type": "duplicate_settlement",
        })

    # refund leg
    if status == "refunded":
        settlement_counter += 1
        settlements.append({
            "settlement_id": f"SETL{settlement_counter}",
            "order_id": order_id,
            "gross_amount": -gross_amount,
            "fee": 0,
            "tax": 0,
            "net_amount": -gross_amount,
            "settlement_date": (settlement_date + timedelta(days=random.randint(1, 4))).strftime("%Y-%m-%d"),
            "type": "refund",
        })
    elif status == "partial_refund":
        partial = money(gross_amount * random.uniform(0.2, 0.5))
        settlement_counter += 1
        settlements.append({
            "settlement_id": f"SETL{settlement_counter}",
            "order_id": order_id,
            "gross_amount": -partial,
            "fee": 0,
            "tax": 0,
            "net_amount": -partial,
            "settlement_date": (settlement_date + timedelta(days=random.randint(1, 4))).strftime("%Y-%m-%d"),
            "type": "partial_refund",
        })

    # ---- Bank leg(s) : what actually happened to the main settlement ----
    missing_case = random.random() < 0.07  # money never arrived / stuck
    split_case = (not missing_case) and status == "partial_refund" and random.random() < 0.5
    rounding_noise_case = (not missing_case) and random.random() < 0.12

    if is_duplicate_case:
        # bank only ever pays once, even though Razorpay shows 2 settlement rows
        pass  # the single bank credit below covers the *real* settlement

    if not missing_case:
        bank_amount = net_amount
        if rounding_noise_case:
            bank_amount = money(net_amount + random.choice([-2.5, -1.75, 1.1, 2.0, -0.9]))

        bank_date = settlement_date + timedelta(days=random.choice([0, 0, 1, 1, 2]))
        include_id_in_narration = random.random() < 0.65  # ~35% of narrations are generic, no ID

        narration = (
            f"NEFT/RAZORPAY/{order_id}/SETL" if include_id_in_narration
            else "RZRPAY SETTLEMENT CR"
        )

        if split_case:
            part1 = money(bank_amount * random.uniform(0.4, 0.6))
            part2 = money(bank_amount - part1)
            for part in (part1, part2):
                bank_counter += 1
                bank_rows.append({
                    "bank_txn_id": f"BANK{bank_counter}",
                    "amount": part,
                    "value_date": (bank_date + timedelta(days=random.randint(0, 1))).strftime("%Y-%m-%d"),
                    "narration": narration + " (split)",
                })
        else:
            bank_counter += 1
            bank_rows.append({
                "bank_txn_id": f"BANK{bank_counter}",
                "amount": bank_amount,
                "value_date": bank_date.strftime("%Y-%m-%d"),
                "narration": narration,
            })

# a couple of totally unexplained bank credits (e.g. a stray NEFT, cashback) — not tied to any settlement
for _ in range(3):
    bank_counter += 1
    bank_rows.append({
        "bank_txn_id": f"BANK{bank_counter}",
        "amount": money(random.uniform(100, 800)),
        "value_date": rand_date(START_DATE).strftime("%Y-%m-%d"),
        "narration": "UNKNOWN CREDIT - BANK REF",
    })

orders_df = pd.DataFrame(orders)
settlements_df = pd.DataFrame(settlements)
bank_df = pd.DataFrame(bank_rows)

orders_df.to_csv("data/order_ledger.csv", index=False)
settlements_df.to_csv("data/razorpay_settlement.csv", index=False)
bank_df.to_csv("data/bank_statement.csv", index=False)

print(f"Generated {len(orders_df)} orders, {len(settlements_df)} settlement rows, {len(bank_df)} bank rows")
print("Files written to ./data/")