# Veriq: AI-Powered Multi-Source Reconciliation

> **Don't just match transactions. Understand them.**

Veriq is an AI-powered financial reconciliation system that helps finance teams reconcile transaction records across multiple sources, identify discrepancies, and understand what happened to the money.

## Live Demo

https://veriq-97r1.onrender.com/

---

## Problem Statement

Financial reconciliation is often a slow and manual process.

A merchant's internal transaction ledger, payment gateway settlement records, and bank statement may not match because of:

- Settlement delays
- Amount discrepancies
- Refunds and partial refunds
- Split transactions
- Duplicate records
- Missing bank entries
- Lifecycle adjustments
- Ambiguous transactions

Traditional reconciliation mainly asks:

> "Do these records match?"

Veriq asks:

> "What actually happened to the money?"

---

## Solution

Veriq performs multi-source reconciliation across three financial sources:

1. Merchant Order or Transaction Ledger
2. Razorpay Settlement Data
3. Bank Statement

The system combines deterministic reconciliation with AI-assisted analysis for ambiguous cases.

Instead of forcing every transaction into a match, Veriq identifies uncertain transactions and creates an explicit exception list for human review.

The goal is:

> **High throughput + measured accuracy + honest exceptions**

One cherry-picked match proves nothing. Veriq evaluates its reconciliation performance across a labelled dataset.

---

## Key Objectives

### High Throughput

Automatically process a large number of financial records instead of requiring manual transaction-by-transaction comparison.

### Measured Accuracy

Measure reconciliation performance using:

- Precision
- Recall
- Match rate
- Exception count

### Honest Exceptions

When the available evidence is insufficient, Veriq does not force a match.

The transaction is instead marked as an exception for human review.

---

## How It Works

```text
Merchant Ledger
       |
       |
Razorpay Settlement
       |
       |
Bank Statement
       |
       v
+--------------------------+
| Veriq Reconciliation     |
| Engine                   |
+------------+-------------+
             |
       +-----+-----+
       |           |
       v           v
 Reconciled    Exceptions
 Transactions  for Review
       |
       v
Transaction Story
```

---

## Reconciliation Pipeline

### Stage 1: Exact Matching

The system first looks for strong evidence such as:

- Matching transaction identifiers
- Matching amounts
- Compatible dates
- Matching references

Strong matches are reconciled automatically.

### Stage 2: Fuzzy Matching

When records have small differences, Veriq evaluates controlled tolerance windows for:

- Amount differences
- Settlement delays
- Date differences
- Reference variations

### Stage 3: Relationship Matching

Some financial events cannot be represented as a simple one-to-one match.

Veriq handles relationships such as:

- Partial refunds
- Split refunds
- Split bank credits
- Lifecycle adjustments

### Stage 4: Duplicate and Missing Record Detection

The system identifies situations such as:

- Duplicate ledger or webhook records
- Missing bank transactions
- Missing settlement records

These cases are surfaced rather than incorrectly reconciled.

### Stage 5: AI-Assisted Review

AI is used for ambiguous cases where deterministic evidence is insufficient.

The AI works with the candidates and evidence already identified by the reconciliation engine.

It does not blindly override deterministic reconciliation decisions.

### Stage 6: Exception Handling

If a transaction does not meet the required confidence level, Veriq leaves it unresolved.

This creates an honest exception list for human investigation.

---

## Transaction Forensics

Veriq goes beyond simply displaying "Matched" or "Unmatched".

It reconstructs the lifecycle of a transaction to explain how the final financial movement occurred.

Example:

```text
Customer Payment
       |
      ₹5,000
       |
     Refund
       |
      ₹500
       |
   Settlement
       |
     ₹4,500
       |
   Bank Credit
       |
       ✓
   Reconciled
```

This allows finance teams to understand why records differ instead of simply seeing that they differ.

---

## Evaluation Results

Veriq was evaluated against **101 labelled settlement records**.

| Metric                   | Result |
| ------------------------ | -----: |
| Records evaluated        |    101 |
| Precision                |   100% |
| Recall                   |  97.8% |
| Match Rate               |  89.1% |
| Unresolved Exceptions    |     12 |
| Incorrect Forced Matches |      0 |

### Interpretation

**100% Precision**

Every automatically accepted match in the evaluation was correct.

**97.8% Recall**

The system identified nearly all labelled matches.

**89.1% Match Rate**

Most records could be reconciled automatically.

**12 Unresolved Exceptions**

The system explicitly surfaced uncertain records instead of forcing them into incorrect matches.

**0 Incorrect Forced Matches**

The system prioritizes financial correctness over artificially increasing the match rate.

---

## Scenarios Covered

The evaluation dataset contains multiple real-world reconciliation scenarios:

- Normal transaction matches
- Small amount discrepancies
- Partial refunds
- Split refunds
- Lifecycle adjustments
- Duplicate records
- Missing bank transactions
- Ambiguous candidate matches

This ensures that the system is evaluated across different financial situations rather than a single ideal transaction.

---

## AI Architecture

Veriq follows an evidence-first approach.

AI is not used to replace the core financial reconciliation logic.

```text
              Financial Records
                     |
                     v
          Deterministic Matching
                     |
            +--------+--------+
            |                 |
            v                 v
       Strong Evidence    Weak Evidence
            |                 |
            v                 v
          Match         AI-Assisted Review
                              |
                              v
                    Candidate Recommendation
                              |
                       +------+------+
                       |             |
                       v             v
                 Human Review    Exception
```

This approach reduces the risk of unsupported or overconfident financial decisions.

---

## Why AI?

Traditional rule-based reconciliation works well when there is strong evidence.

The difficult cases are usually the ambiguous tail:

- Similar transaction amounts
- Different transaction dates
- Different bank narrations
- Split movements
- Related refunds
- Multiple possible candidates

Veriq uses AI to assist with these cases while keeping deterministic evidence and human review in the loop.

---

## Technical Architecture

```text
                 +----------------------+
                 |   Merchant Ledger    |
                 +----------+-----------+
                            |
                 +----------v-----------+
                 | Razorpay Settlement  |
                 +----------+-----------+
                            |
                 +----------v-----------+
                 |    Bank Statement    |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Reconciliation Engine|
                 +----------+-----------+
                            |
          +-----------------+-----------------+
          |                 |                 |
          v                 v                 v
     Exact Match       Fuzzy Match      Relationship
                                           Matching
          |                 |                 |
          +-----------------+-----------------+
                            |
                            v
                 +----------------------+
                 | Ambiguous Case Review|
                 |       with AI        |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Results + Exceptions |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |     Web Dashboard    |
                 +----------------------+
```

---

## Technology Stack

### Backend

- Python
- FastAPI
- Uvicorn
- Pandas

### Data Processing

- CSV-based financial datasets
- Deterministic reconciliation algorithms
- Amount and date tolerance matching
- Split-sum matching
- Duplicate detection
- Transaction lifecycle analysis

### AI

- AI-assisted analysis for ambiguous reconciliation cases
- Candidate-based recommendations
- Evidence-first decision making
- Human review for uncertain cases

### Frontend

- HTML
- CSS
- JavaScript
- Interactive reconciliation dashboard
- Transaction explorer
- Exception views
- Evaluation metrics

### Deployment

- Render
- FastAPI application served using Uvicorn

---

## Project Structure

```text
Veriq/
|
├── app.py
├── reconcile.py
├── traction_story.py
├── evaluate.py
├── ai_review_ambiguous.py
├── generate_data.py
├── fetch_razorpay.py
├── train_match_ranker.py
|
├── data/
│   ├── order_ledger.csv
│   ├── razorpay_settlement.csv
│   └── bank_statement.csv
|
├── output/
|
├── requirements.txt
└── README.md
```

---

## Core Files

### `app.py`

Runs the FastAPI application and provides the web interface and API endpoints.

The application supports:

- Demo dataset processing
- CSV uploads
- Reconciliation execution
- Results display
- Transaction stories
- Exception reporting

### `reconcile.py`

Contains the core reconciliation engine.

It performs:

1. Exact matching
2. Fuzzy matching
3. Split-sum matching
4. Lifecycle and relationship analysis
5. Exception generation

### `traction_story.py`

Generates transaction lifecycle stories that explain what happened to individual transactions.

### `evaluate.py`

Evaluates the reconciliation engine against labelled data and calculates:

- Precision
- Recall
- Match rate
- True positives
- False positives
- False negatives
- True negatives
- Exception counts

### `ai_review_ambiguous.py`

Provides AI-assisted analysis for ambiguous reconciliation cases.

The AI reviews candidates supplied by the reconciliation system and does not independently invent transaction matches.

### `generate_data.py`

Generates the synthetic evaluation and demonstration dataset.

The generated dataset includes realistic reconciliation scenarios such as:

- Normal matches
- Amount discrepancies
- Refunds
- Duplicates
- Missing records
- Ambiguous cases
- Lifecycle adjustments

### `fetch_razorpay.py`

Fetches Razorpay Test Mode settlement reconciliation data using the Razorpay API.

Test Mode data is kept separate from the synthetic evaluation dataset.

---

## Running Locally

### 1. Clone the repository

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd Veriq
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

### 3. Activate the environment

On Windows:

```bash
.venv\Scripts\activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Generate the demo data

```bash
python generate_data.py
```

### 6. Run the application

```bash
uvicorn app:app --reload
```

Open:

```text
http://localhost:8000
```

---

## Environment Variables

For integrations that require credentials, configure the following environment variables:

```text
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
ANTHROPIC_API_KEY=
```

Do not commit credentials to the repository.

A local `.env` file can be used during development.

Example:

```text
RAZORPAY_KEY_ID=your_key_id
RAZORPAY_KEY_SECRET=your_key_secret
ANTHROPIC_API_KEY=your_api_key
```

Add `.env` to `.gitignore`.

---

## Deployment

Veriq is deployed on Render.

### Build Command

```bash
pip install -r requirements.txt
```

### Start Command

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

### Live Application

[https://veriq-97r1.onrender.com/](https://veriq-97r1.onrender.com/)

Environment variables are configured through the deployment platform rather than committing secrets to the repository.

---

## Data Sources

Veriq is designed around three sources:

### Merchant Ledger

Represents the merchant's internal record of transactions and orders.

### Razorpay Settlement Data

Represents payment gateway settlement and reconciliation information.

### Bank Statement

Represents the actual bank-side financial movement.

The system compares evidence across these sources to reconstruct the financial state.

---

## Razorpay Test Mode

Veriq includes support for retrieving Razorpay Test Mode settlement reconciliation data.

The integration uses the Razorpay settlement reconciliation API.

Test Mode is kept separate from the synthetic evaluation dataset.

The Razorpay integration does not claim to provide bank statement data. The bank statement remains a separate merchant-side source.

---

## Safety and Reliability

Financial reconciliation requires conservative decision making.

Veriq follows these principles:

### No Forced Matches

Uncertain transactions are not automatically marked as reconciled.

### Evidence-Based Decisions

Reconciliation decisions are based on available transaction evidence.

### AI as Assistance

AI is used to assist with ambiguous cases rather than replacing the core reconciliation engine.

### Transparent Exceptions

Unresolved cases are explicitly displayed so finance teams know where human investigation is required.

### Measurable Evaluation

System performance is measured across a labelled dataset rather than demonstrated using only selected examples.

---

## Key Value Proposition

Traditional reconciliation asks:

> **"Matched or unmatched?"**

Veriq asks:

> **"What happened to the money, what evidence supports it, and what still needs attention?"**

Veriq helps finance teams:

- Reconcile transactions faster
- Reduce manual investigation
- Detect discrepancies
- Understand transaction lifecycles
- Prioritize exceptions
- Make reconciliation more measurable and explainable

---

## Future Scope

Potential future improvements include:

- Direct bank statement integrations
- Additional payment gateway integrations
- Continuous reconciliation
- Automated month-end close workflows
- Finance controller conversational Q&A
- Advanced anomaly detection
- ERP and accounting system integrations
- Role-based review and approval workflows
- Automated exception resolution with approval controls

---

## Conclusion

Veriq is designed to demonstrate that financial reconciliation can be both automated and conservative.

Rather than maximizing the number of matched transactions, the system focuses on:

> **Throughput + measured accuracy + honest exceptions**

The result is a reconciliation workflow that helps finance teams understand not only whether records match, but also **what happened to the money and what still needs attention.**

---

## Team

**Veriq: AI-Powered Multi-Source Reconciliation**

Built for the **AI Finance Controller** track of **Razorpay Buildathon**.
