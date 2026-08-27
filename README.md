# Reconciliation Agent

Matches Razorpay settlement records against bank statement entries in staged
passes (exact → fuzzy → split → LLM-assisted), and produces an honest
exception list for whatever can't be confidently matched — instead of
force-matching everything to inflate the number.

## Setup

```bash
cd reconciliation_agent
pip install -r requirements.txt --break-system-packages   # or use a venv, see below
```

If you'd rather use a virtual environment (recommended if this isn't a
throwaway sandbox):

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Only `pandas` is required to run stages 1–3 (exact, fuzzy, split matching)
and generate the dashboard. `anthropic` is only needed if you want to turn on
Stage 4 (LLM-assisted matching for the hardest remaining cases).

## Run it

```bash
python generate_data.py   # creates data/order_ledger.csv, razorpay_settlement.csv, bank_statement.csv
python reconcile.py       # runs the matching engine -> output/*.csv, output/summary.json
python dashboard.py       # builds output/report.html
```

Open `output/report.html` in a browser to see the dashboard.

## Turning on LLM-assisted matching (Stage 4, optional)

Stage 4 is skipped automatically if no key is set — everything still works,
it just leaves more items as exceptions. To enable it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Windows: set ANTHROPIC_API_KEY=sk-ant-...
python reconcile.py
```

## What each file does

| File | Purpose |
|---|---|
| `generate_data.py` | Builds 3 synthetic CSVs with realistic mismatch patterns: delayed settlements, fee-rounding differences, duplicate webhooks, split bank credits, missing payouts, and inconsistent bank narrations. Swap this out for real exports once you have merchant data. |
| `reconcile.py` | The matching engine. Stage 1: order ID found in bank narration + amount/date match. Stage 2: greedy fuzzy match on amount+date for rows with no ID in the narration. Stage 3: checks if a settlement equals the sum of two unmatched bank rows (split refunds etc.). Stage 4 (optional): asks Claude to reason about the hardest remaining cases. Everything left over becomes a classified exception, never a forced match. |
| `dashboard.py` | Renders `output/summary.json` + the CSVs into a single static `report.html` — match rate, breakdown by match type, and the full exception table. |

## Output files (in `output/`)

- `reconciliation_report.csv` — every matched settlement, its match type, confidence, and which bank row(s) it matched to
- `exceptions.csv` — every unmatched settlement with a specific, classified reason (not a generic "unmatched")
- `unexplained_bank_credits.csv` — bank money that doesn't map to any settlement (worth showing — real money merchants often don't even notice)
- `summary.json` — match rate %, breakdown by match type, counts
- `report.html` — the dashboard, ready to demo

## Tuning knobs

At the top of `reconcile.py`:

```python
AMOUNT_TOL_TIGHT = 3.0      # ₹ tolerance for exact-ID matches
AMOUNT_TOL_FUZZY = 10.0     # ₹ tolerance for fuzzy matches
AMOUNT_TOL_SPLIT = 5.0      # ₹ tolerance for split-sum matches
DATE_WINDOW_TIGHT = 3       # days
DATE_WINDOW_FUZZY = 7       # days
DATE_WINDOW_SPLIT = 5       # days
```

Loosen these and your match rate goes up — but so does the risk of a wrong
match. For the demo, it's worth showing the tolerance settings on screen:
judges specifically want to see you're not gaming the number.

## Swapping in real data

Replace the 3 CSVs in `data/` with real exports and keep the column names
the same:

- `order_ledger.csv`: `order_id, customer, gross_amount, order_date, status`
- `razorpay_settlement.csv`: `settlement_id, order_id, gross_amount, fee, tax, net_amount, settlement_date, type`
- `bank_statement.csv`: `bank_txn_id, amount, value_date, narration`

Razorpay's actual settlement report (Dashboard → Settlements → Export) and
your bank's statement export are the real-world equivalents — you'll need a
small adapter script to rename columns, but the matching engine itself
doesn't change.