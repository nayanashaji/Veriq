"""
dashboard.py (v2)
------------------
Renders output/summary.json + the CSVs into output/report.html.
Now leads with precision/recall (the honest metric) rather than match rate
alone, and shows the auto-accept vs needs-review split so it's visually
obvious that low-confidence matches don't get silently auto-posted.

Run: python dashboard.py
Output: output/report.html
"""

import json
import pandas as pd

with open("output/summary.json") as f:
    summary = json.load(f)

exceptions_df = pd.read_csv("output/exceptions.csv")
auto_df = pd.read_csv("output/auto_accepted.csv")
review_df = pd.read_csv("output/needs_review.csv")

scoring = summary.get("ground_truth_scoring") or {}
breakdown = summary.get("match_type_breakdown", {})
breakdown_labels = json.dumps(list(breakdown.keys()))
breakdown_values = json.dumps(list(breakdown.values()))

def df_to_table(df, max_rows=100):
    if df.empty:
        return "<p style='color:#888'>None 🎉</p>"
    return df.head(max_rows).to_html(index=False, classes="data-table", border=0)

def metric_or_dash(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"

precision_pct = metric_or_dash(round(scoring.get("precision", 0) * 100, 1) if scoring.get("precision") is not None else None, "%")
recall_pct = metric_or_dash(round(scoring.get("recall", 0) * 100, 1) if scoring.get("recall") is not None else None, "%")
auto_precision_pct = metric_or_dash(
    round(scoring.get("auto_accept_precision", 0) * 100, 1) if scoring.get("auto_accept_precision") is not None else None, "%")

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Veriq — AI-Powered Multi-Source Reconciliation</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1420; --card: #161d2e; --accent: #6ee7b7; --accent2: #f87171;
    --accent3: #93c5fd; --text: #e6e9f0; --muted: #93a0b8; --border: #232c42;
  }}
  * {{ box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', Arial, sans-serif;
    margin: 0; padding: 32px 48px; }}
  h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); margin-bottom: 28px; font-size: 14px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 22px; min-width: 170px; flex: 1; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  .card .value {{ font-size: 30px; font-weight: 700; margin-top: 6px; }}
  .card.good .value {{ color: var(--accent); }}
  .card.warn .value {{ color: var(--accent2); }}
  .card.info .value {{ color: var(--accent3); }}
  .note {{ color: var(--muted); font-size: 12px; margin: -8px 0 28px 2px; }}
  .section {{ margin-bottom: 36px; }}
  .section h2 {{ font-size: 16px; border-left: 3px solid var(--accent); padding-left: 10px; }}
  .chart-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; max-width: 420px; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.data-table th {{ text-align: left; color: var(--muted); font-weight: 600; padding: 8px 10px;
    border-bottom: 1px solid var(--border); text-transform: uppercase; font-size: 11px; }}
  table.data-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  table.data-table tr:hover td {{ background: #1c2540; }}
</style>
</head>
<body>
  <h1>Veriq — AI-Powered Multi-Source Reconciliation</h1>
  <div class="subtitle">Don't just match transactions. Understand them. · Generated {summary['generated_at']}</div>

  <div class="section">
    <h2>Reconciliation outcome</h2>
    <div class="cards">
      <div class="card info"><div class="label">Records Processed</div><div class="value">{summary['total_settlements_considered']}</div></div>
      <div class="card good"><div class="label">Match Rate</div><div class="value">{summary['match_rate_pct']}%</div></div>
      <div class="card good"><div class="label">Auto-Reconciled</div><div class="value">{summary['auto_accepted_count']}</div></div>
      <div class="card warn"><div class="label">Exceptions</div><div class="value">{summary['exception_count']}</div></div>
    </div>
    <div class="note">Multi-source reconciliation across the internal ledger, Razorpay settlements, and bank statement. Lifecycle reconstruction explains difficult cases; unresolved items remain visible as exceptions.</div>
  </div>

  <div class="section">
    <h2>Accuracy against held-out ground truth</h2>
    <div class="cards">
      <div class="card good"><div class="label">Precision</div><div class="value">{precision_pct}</div></div>
      <div class="card good"><div class="label">Recall</div><div class="value">{recall_pct}</div></div>
      <div class="card good"><div class="label">Auto-Accept Precision</div><div class="value">{auto_precision_pct}</div></div>
      <div class="card info"><div class="label">Comparisons Made</div><div class="value">{summary.get('pairwise_comparisons_made','—')}</div></div>
    </div>
    <div class="note">Precision/recall measured against a labeled synthetic answer key (data/ground_truth.csv) —
      not just "% of rows matched". Auto-accept precision is measured only on matches confident enough
      to post without human review (≥ 0.85).</div>
  </div>

  <div class="section">
    <h2>Volume</h2>
    <div class="cards">
      <div class="card"><div class="label">Settlements Considered</div><div class="value">{summary['total_settlements_considered']}</div></div>
      <div class="card good"><div class="label">Auto-Accepted</div><div class="value">{summary['auto_accepted_count']}</div></div>
      <div class="card"><div class="label">Needs Review</div><div class="value">{summary['needs_review_count']}</div></div>
      <div class="card warn"><div class="label">Exceptions</div><div class="value">{summary['exception_count']}</div></div>
      <div class="card warn"><div class="label">Unexplained Bank Credits</div><div class="value">{summary['unexplained_bank_credit_count']}</div></div>
    </div>
  </div>

  <div class="section">
    <h2>Match type breakdown</h2>
    <div class="chart-wrap"><canvas id="breakdownChart" width="380" height="260"></canvas></div>
  </div>

  <div class="section">
    <h2>Auto-accepted matches ({len(auto_df)}) — posted without human review</h2>
    {df_to_table(auto_df)}
  </div>

  <div class="section">
    <h2>Needs review ({len(review_df)}) — confident enough to surface, not enough to auto-post</h2>
    {df_to_table(review_df)}
  </div>

  <div class="section">
    <h2>Exceptions — honest, unresolved cases ({len(exceptions_df)})</h2>
    {df_to_table(exceptions_df)}
  </div>

<script>
  new Chart(document.getElementById('breakdownChart'), {{
    type: 'doughnut',
    data: {{ labels: {breakdown_labels}, datasets: [{{ data: {breakdown_values},
      backgroundColor: ['#6ee7b7', '#fde68a', '#93c5fd', '#d8b4fe', '#f87171'], borderWidth: 0 }}] }},
    options: {{ plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#e6e9f0' }} }} }} }}
  }});
</script>
</body>
</html>
"""

with open("output/report.html", "w", encoding="utf-8") as f:
    f.write(html)

print("Written: output/report.html")
