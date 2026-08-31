# Veriq — AI-Powered Multi-Source Reconciliation

> Don't just match transactions. Understand them.

Veriq reconciles internal ledger, Razorpay settlement, and bank-statement
records in staged passes (exact → fuzzy → split → lifecycle evidence → AI review).
When a direct match is ambiguous, it reconstructs the transaction lifecycle—fees,
refunds, chargebacks, settlement adjustments, duplicates, split payments, and
cashback—to resolve the reconciliation case. Anything
it cannot support confidently becomes an explicit, classified exception rather
than a forced match.

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

Only `pandas` is required to run the deterministic reconciliation stages
and generate the dashboard. `anthropic` is only needed if you want to turn on
the optional AI review stage for the hardest remaining cases.

## Run it

```bash
python generate_data.py   # creates data/order_ledger.csv, razorpay_settlement.csv, bank_statement.csv
python reconcile.py       # runs the matching engine -> output/*.csv, output/summary.json
python dashboard.py       # builds output/report.html
python traction_story.py  # builds output/story_report.html with lifecycle explanations
python evaluate.py        # writes a case-type evaluation report for synthetic data
```

Open `output/report.html` for the reconciliation scorecard and
`output/story_report.html` for the explainable lifecycle evidence behind each
reconciliation verdict.

## Local demo UI

```bash
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`. **Try Demo Dataset** runs only the labelled
synthetic data. **Upload Your Data** accepts the three CSV exports and does not
claim precision/recall because no ground truth is available.

## Razorpay Test Mode ingestion

```bash
set RAZORPAY_KEY_ID=...
set RAZORPAY_KEY_SECRET=...
python fetch_razorpay.py --year 2026 --month 8
```

This separately saves the raw official Test Mode settlement-reconciliation
response and a normalized settlement CSV in `data/razorpay_test_mode/`. It
never overwrites the synthetic data or treats Test Mode records as real merchant
data. Supply corresponding merchant-ledger and bank exports before reconciling
the Test Mode file.

## Scalable Spark pipeline

For production-style batches, run the distributed pipeline instead of loading
every source into Pandas:

```bash
python scalable_reconcile.py --input data --warehouse warehouse
```

It writes partitioned, append-only Parquet tables for reconciliation decisions,
candidate edges, explicit exceptions, duplicate-ledger exceptions, reviewer
labels, and an AI-review queue. Candidate blocking uses merchant, payment rail,
currency, amount bucket, and date bucket before ranking, so it never performs
a full cross join. Every candidate edge keeps its features and rejection reason.
Identical replay events are skipped within a batch; unchanged decision
fingerprints are skipped on subsequent runs.

On this Windows workstation, use `--dry-run` unless Hadoop `winutils.exe` is
configured. Linux Spark clusters (Databricks, EMR, Kubernetes, standalone Spark)
do not have that local Windows requirement.

After analysts have supplied `data/reviewer_decisions.csv` with
`settlement_id,approved,actual_bank_txn_id,reviewer,reviewed_at`, train a
candidate-ranking model with:

```bash
python train_match_ranker.py --warehouse warehouse
```

The ranking model only prioritizes the review queue; deterministic evidence
rules remain the gate for automatic posting.

### Evidence and AI-review controls

The warehouse tables provide a complete audit path:

```text
ingestion_deduplication → candidate_edges → reconciliation_decisions
                                            → ai_review_queue → ai_recommendations
                                            → review_labels
```

`candidate_edges` stores every considered bank candidate, calculated features,
blocking and rule versions, selection/rejection status, and rejection reason.
`reconciliation_decisions` stores the final deterministic outcome, confidence,
reason, and rule version. The model is invoked only for `ai_review_queue` items
with competing candidates. It can select only an already supplied bank ID (or
no match), and its result never alters a decision automatically.

```bash
# Requires an existing warehouse written on a Linux/cluster environment.
export ANTHROPIC_API_KEY=...
python ai_review_ambiguous.py --warehouse warehouse

# Validate queue handling without sending any data to the model.
python ai_review_ambiguous.py --warehouse warehouse --dry-run
```

## Turning on AI-assisted review (optional)

AI review is skipped automatically if no key is set. When enabled, the model
can only select from supplied bank IDs and creates a **needs-review** candidate;
it never auto-posts or converts missing evidence into a match. To enable it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Windows: set ANTHROPIC_API_KEY=sk-ant-...
python reconcile.py
```

## What each file does

| File | Purpose |
|---|---|
| `generate_data.py` | Builds synthetic multi-source records for refunds, multiple partial refunds, chargebacks, documented settlement adjustments, FX conversion, duplicate ledger records, split payments, cashback, and deliberately unresolved cases. |
| `reconcile.py` | The matching engine. It resolves exact, fuzzy, split, FX, and evidenced lifecycle-adjustment matches. The optional advanced-model stage only proposes human-review candidates from supplied evidence; it cannot auto-post. Everything else becomes a classified exception. |
| `dashboard.py` | Renders `output/summary.json` + the CSVs into a static reconciliation scorecard: volume, match rate, precision/recall, match types, and exceptions. |
| `traction_story.py` | Renders lifecycle explanations for each reconciliation verdict. This is the evidence layer for difficult cases, not a separate forensics product. |

## Output files (in `output/`)

- `auto_accepted.csv` and `needs_review.csv` — matched settlements, their evidence, confidence, and bank row(s)
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
