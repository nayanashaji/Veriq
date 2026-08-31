"""
traction_story.py
------------------
Renders the explainability layer of Veriq's multi-source reconciliation
workflow. When a direct match is insufficient, it reconstructs an order's
transaction lifecycle end to end and states, per order:

    RECONCILED / PARTIALLY RECONCILED / UNRESOLVED  +  a confidence score
    +  a plain-English narrative of what happened to that money

It reads ONLY what reconcile.py already produced (auto_accepted.csv,
needs_review.csv, exceptions.csv) plus the raw ledger/settlement data —
it does not re-run any matching itself. This is a presentation/reasoning
layer on top of the existing engine, not a replacement for it.

Run: python transaction_story.py
Output: output/transaction_stories.json, output/story_report.html
"""

import json
import pandas as pd

def load_match_lookup():
    """settlement_id -> {tier, match_type, bank_txn_ids, confidence, notes}"""
    lookup = {}
    for tier, path in [("auto_accepted", "output/auto_accepted.csv"),
                        ("needs_review", "output/needs_review.csv")]:
        df = pd.read_csv(path)
        for _, row in df.iterrows():
            lookup[row["settlement_id"]] = {
                "status": "matched", "tier": tier, "match_type": row["match_type"],
                "bank_txn_ids": row["matched_bank_txn_ids"], "confidence": row["confidence"],
                "notes": row["notes"],
            }
    exc_df = pd.read_csv("output/exceptions.csv")
    for _, row in exc_df.iterrows():
        lookup[row["settlement_id"]] = {
            "status": "exception", "tier": None, "match_type": None,
            "bank_txn_ids": None, "confidence": 0.0, "notes": row["reason"],
        }
    return lookup


LEG_LABELS = {
    "settlement": "Main settlement",
    "split_payment": "Split payment leg",
    "duplicate_settlement": "Duplicate settlement webhook",
    "refund": "Full refund",
    "partial_refund": "Partial refund",
    "chargeback": "Chargeback",
    "settlement_adjustment": "Settlement adjustment",
    "cashback_adjustment": "Cashback credit",
}


def build_story(order_id, order_row, legs_df, match_lookup):
    events = []
    events.append({
        "date": order_row["order_date"], "kind": "order_placed",
        "text": f"Customer paid ₹{order_row['gross_amount']}",
    })

    relevant_confidences = []
    unresolved_notes = []
    total_relevant_legs = 0
    resolved_legs = 0
    duplicate_flagged = False
    lifecycle_net = 0.0

    legs_sorted = legs_df.sort_values("settlement_date")
    for _, leg in legs_sorted.iterrows():
        label = LEG_LABELS.get(leg["type"], leg["type"])
        net = leg["net_amount"]

        if leg["type"] in ("settlement", "split_payment"):
            events.append({
                "date": order_row["order_date"], "kind": "captured",
                "text": f"Razorpay captured ₹{leg['gross_amount']}" + (" as one split-payment leg" if leg["type"] == "split_payment" else ""),
            })
            fee_total = round(float(leg["fee"]) + float(leg["tax"]), 2)
            if fee_total > 0:
                events.append({
                    "date": order_row["order_date"], "kind": "fee",
                    "text": f"₹{fee_total} deducted (fee ₹{leg['fee']} + tax ₹{leg['tax']})",
                })

        if leg["type"] == "duplicate_settlement":
            duplicate_flagged = True
            events.append({
                "date": leg["settlement_date"], "kind": "duplicate",
                "text": f"Duplicate settlement webhook detected for ₹{net} — "
                        f"no second bank credit expected, ignored in reconciliation.",
            })
            continue  # duplicates don't count toward resolved/unresolved totals

        total_relevant_legs += 1
        lifecycle_net += float(net)
        info = match_lookup.get(leg["settlement_id"])

        if leg["type"] in ("refund", "partial_refund", "chargeback"):
            events.append({
                "date": leg["settlement_date"], "kind": "chargeback" if leg["type"] == "chargeback" else "refund_initiated",
                "text": (f"Chargeback of ₹{abs(net)} recorded — distinct from a customer refund" if leg["type"] == "chargeback"
                         else f"{label} of ₹{abs(net)} initiated"),
            })
        elif leg["type"] in ("settlement_adjustment", "cashback_adjustment"):
            events.append({"date": leg["settlement_date"], "kind": "adjustment",
                           "text": f"{label}: {'+' if float(net) > 0 else ''}₹{net} recorded as a separate lifecycle event."})

        if info and info["status"] == "matched":
            resolved_legs += 1
            relevant_confidences.append(info["confidence"])
            verb = ("Chargeback debited from bank" if leg["type"] == "chargeback" else
                    "Refund debited from bank" if net < 0 else "Settled to bank")
            events.append({
                "date": leg["settlement_date"], "kind": "bank_matched",
                "text": f"{verb}: ₹{abs(net)} via {info['bank_txn_ids']} "
                        f"({info['match_type']}, confidence {info['confidence']:.0%}) — {info['notes']}",
            })
        elif info and info["status"] == "exception":
            unresolved_notes.append(f"{label} (₹{abs(net)}): {info['notes']}")
            events.append({
                "date": leg["settlement_date"], "kind": "unresolved",
                "text": f"No confirmed bank movement for {label.lower()} of ₹{abs(net)} — {info['notes']}",
            })
        else:
            unresolved_notes.append(f"{label} (₹{abs(net)}): not yet scored")
            events.append({
                "date": leg["settlement_date"], "kind": "unresolved",
                "text": f"{label} of ₹{abs(net)} has no recorded match status.",
            })

    # ---- roll up into a single story-level verdict ----
    if total_relevant_legs == 0:
        status, confidence = "RECONCILED", 1.0
    elif resolved_legs == total_relevant_legs:
        status = "RECONCILED"
        confidence = round(min(relevant_confidences), 3) if relevant_confidences else 1.0
    elif resolved_legs == 0:
        status, confidence = "UNRESOLVED", 0.0
    else:
        status = "PARTIALLY RECONCILED"
        avg_resolved_conf = sum(relevant_confidences) / len(relevant_confidences)
        confidence = round(avg_resolved_conf * (resolved_legs / total_relevant_legs), 3)

    summary_bits = []
    if status == "RECONCILED":
        summary_bits.append("Full lifecycle accounted for.")
    summary_bits.append(f"Net lifecycle movement: ₹{lifecycle_net:.2f}.")
    if duplicate_flagged:
        summary_bits.append("Duplicate webhook detected and excluded — did not affect the money movement.")
    if unresolved_notes:
        summary_bits.append("Open thread(s): " + "; ".join(unresolved_notes))

    return {
        "order_id": order_id,
        "customer": order_row["customer"],
        "gross_amount": order_row["gross_amount"],
        "status": status,
        "confidence": confidence,
        "summary": " ".join(summary_bits),
        "events": sorted(events, key=lambda e: e["date"]),
    }


def main():
    orders = pd.read_csv("data/order_ledger.csv")
    settlements = pd.read_csv("data/razorpay_settlement.csv")
    match_lookup = load_match_lookup()

    stories = []
    # Duplicate ledger entries are reported by reconcile.py as explicit
    # exceptions; render a single lifecycle rather than two fake transactions.
    for _, order_row in orders.drop_duplicates("order_id").iterrows():
        legs = settlements[settlements["order_id"] == order_row["order_id"]]
        stories.append(build_story(order_row["order_id"], order_row, legs, match_lookup))

    with open("output/transaction_stories.json", "w", encoding="utf-8") as f:
        json.dump(stories, f, indent=2, default=str)

    status_counts = pd.Series([s["status"] for s in stories]).value_counts().to_dict()
    avg_conf_reconciled = round(
        sum(s["confidence"] for s in stories if s["status"] == "RECONCILED") /
        max(status_counts.get("RECONCILED", 1), 1), 3)

    print(f"Built {len(stories)} transaction stories")
    print(json.dumps(status_counts, indent=2))
    print(f"Avg confidence among fully RECONCILED stories: {avg_conf_reconciled:.0%}")

    with open("output/summary.json") as f:
        reconciliation_summary = json.load(f)
    render_html(stories, status_counts, reconciliation_summary)


def render_html(stories, status_counts, reconciliation_summary):
    KIND_ICONS = {
        "order_placed": "🛒", "captured": "💳", "fee": "➖", "refund_initiated": "↩️",
        "chargeback": "⚖️", "adjustment": "🧾",
        "bank_matched": "✅", "unresolved": "❓", "duplicate": "⚠️",
    }
    STATUS_COLOR = {"RECONCILED": "#6ee7b7", "PARTIALLY RECONCILED": "#fde68a", "UNRESOLVED": "#f87171"}

    cards = []
    for s in sorted(stories, key=lambda x: {"UNRESOLVED": 0, "PARTIALLY RECONCILED": 1, "RECONCILED": 2}[x["status"]]):
        color = STATUS_COLOR[s["status"]]
        timeline_html = "".join(
            f'<div class="event"><span class="icon">{KIND_ICONS.get(e["kind"], "•")}</span>'
            f'<span class="edate">{e["date"]}</span><span class="etext">{e["text"]}</span></div>'
            for e in s["events"]
        )
        cards.append(f"""
        <div class="story-card" data-status="{s['status']}">
          <div class="story-head">
            <div>
              <span class="order-id">{s['order_id']}</span>
              <span class="customer">{s['customer']} · ₹{s['gross_amount']}</span>
            </div>
            <div class="status-pill" style="background:{color}22; color:{color}; border:1px solid {color}66;">
              {s['status']} · {s['confidence']:.0%} confidence
            </div>
          </div>
          <div class="summary">{s['summary']}</div>
          <div class="timeline">{timeline_html}</div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Veriq — AI-Powered Multi-Source Reconciliation</title>
<style>
  :root {{ --bg:#0f1420; --card:#161d2e; --text:#e6e9f0; --muted:#93a0b8; --border:#232c42; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',Arial,sans-serif; margin:0; padding:32px 48px; }}
  h1 {{ font-size:22px; margin-bottom:2px; }}
  .eyebrow {{ color:#6ee7b7; font-size:11px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; margin-bottom:8px; }}
  .tagline {{ color:var(--muted); font-size:14px; margin:0 0 10px; font-style:italic; }}
  .explanation {{ color:var(--muted); font-size:13px; line-height:1.5; max-width:850px; margin:0 0 20px; }}
  .metrics {{ display:flex; gap:10px; flex-wrap:wrap; margin:0 0 22px; }}
  .metric {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 16px; min-width:122px; }}
  .metric-label {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.06em; }}
  .metric-value {{ color:#6ee7b7; font-size:21px; font-weight:700; margin-top:4px; }}
  .flow {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:12px; margin:0 0 24px; }}
  .flow-step {{ background:#1c2540; border:1px solid var(--border); border-radius:999px; padding:6px 10px; }}
  .flow-arrow {{ color:#6ee7b7; }}
  .filters {{ margin-bottom:24px; display:flex; gap:10px; }}
  .filters button {{ background:var(--card); border:1px solid var(--border); color:var(--text);
    padding:6px 14px; border-radius:20px; font-size:12px; cursor:pointer; }}
  .filters button.active {{ border-color:#6ee7b7; color:#6ee7b7; }}
  .story-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:18px 22px; margin-bottom:16px; }}
  .story-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .order-id {{ font-weight:700; font-size:15px; margin-right:10px; }}
  .customer {{ color:var(--muted); font-size:13px; }}
  .status-pill {{ padding:4px 12px; border-radius:20px; font-size:12px; font-weight:600; }}
  .summary {{ color:var(--muted); font-size:13px; margin-bottom:14px; }}
  .timeline {{ border-left:2px solid var(--border); padding-left:16px; }}
  .event {{ display:flex; gap:10px; align-items:baseline; font-size:13px; padding:4px 0; }}
  .icon {{ width:20px; }}
  .edate {{ color:var(--muted); font-size:11px; width:88px; flex-shrink:0; }}
</style>
</head>
<body>
  <div class="eyebrow">Track 04 · Multi-source reconciliation</div>
  <h1>Veriq — AI-Powered Multi-Source Reconciliation</h1>
  <div class="tagline">Don't just match transactions. Understand them.</div>
  <p class="explanation">Veriq reconciles internal ledger, payment-processor settlement, and bank records. For difficult cases, it reconstructs the transaction lifecycle to explain timing, fees, refunds, duplicates, and split movements—then returns a reconciliation verdict or a clear exception.</p>
  <div class="metrics">
    <div class="metric"><div class="metric-label">Records processed</div><div class="metric-value">{reconciliation_summary['total_settlements_considered']}</div></div>
    <div class="metric"><div class="metric-label">Match rate</div><div class="metric-value">{reconciliation_summary['match_rate_pct']}%</div></div>
    <div class="metric"><div class="metric-label">Precision</div><div class="metric-value">{((reconciliation_summary.get('ground_truth_scoring') or {}).get('precision') or 0) * 100:.1f}%</div></div>
    <div class="metric"><div class="metric-label">Recall</div><div class="metric-value">{((reconciliation_summary.get('ground_truth_scoring') or {}).get('recall') or 0) * 100:.1f}%</div></div>
    <div class="metric"><div class="metric-label">Auto-reconciled</div><div class="metric-value">{reconciliation_summary['auto_accepted_count']}</div></div>
    <div class="metric"><div class="metric-label">Exceptions</div><div class="metric-value">{reconciliation_summary['exception_count']}</div></div>
  </div>
  <div class="flow" aria-label="Reconciliation workflow">
    <span class="flow-step">Exact matching</span><span class="flow-arrow">→</span>
    <span class="flow-step">Fuzzy &amp; split matching</span><span class="flow-arrow">→</span>
    <span class="flow-step">Lifecycle reconstruction</span><span class="flow-arrow">→</span>
    <span class="flow-step">AI-assisted resolution</span><span class="flow-arrow">→</span>
    <span class="flow-step">Match or exception</span>
  </div>
  <h2>Lifecycle explanations for reconciliation decisions</h2>
  <div class="filters">
    <button class="active" onclick="filterStatus('ALL', this)">All ({len(stories)})</button>
    <button onclick="filterStatus('RECONCILED', this)">Reconciled ({status_counts.get('RECONCILED', 0)})</button>
    <button onclick="filterStatus('PARTIALLY RECONCILED', this)">Partially Reconciled ({status_counts.get('PARTIALLY RECONCILED', 0)})</button>
    <button onclick="filterStatus('UNRESOLVED', this)">Unresolved ({status_counts.get('UNRESOLVED', 0)})</button>
  </div>
  {''.join(cards)}
<script>
  function filterStatus(status, btn) {{
    document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.story-card').forEach(c => {{
      c.style.display = (status === 'ALL' || c.dataset.status === status) ? 'block' : 'none';
    }});
  }}
</script>
</body>
</html>
"""
    with open("output/story_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Written: output/story_report.html")


if __name__ == "__main__":
    main()
