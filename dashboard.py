"""
dashboard.py
------------
Turns output/summary.json + output/exceptions.csv + output/reconciliation_report.csv
into a single static HTML report you can open in a browser or drop into a demo.

Run: python dashboard.py
Output: output/report.html
"""

import json
import pandas as pd

with open("output/summary.json") as f:
    summary = json.load(f)

exceptions_df = pd.read_csv("output/exceptions.csv")
matched_df = pd.read_csv("output/reconciliation_report.csv")

breakdown = summary.get("match_type_breakdown", {})
breakdown_labels = json.dumps(list(breakdown.keys()))
breakdown_values = json.dumps(list(breakdown.values()))

def df_to_table(df, max_rows=100):
    if df.empty:
        return "<p style='color:#888'>None 🎉</p>"
    return df.head(max_rows).to_html(index=False, classes="data-table", border=0)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Reconciliation Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1420; --card: #161d2e; --accent: #6ee7b7; --accent2: #f87171;
    --text: #e6e9f0; --muted: #93a0b8; --border: #232c42;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); font-family: 'Segoe UI', Arial, sans-serif;
    margin: 0; padding: 32px 48px;
  }}
  h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); margin-bottom: 28px; font-size: 14px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 22px; min-width: 180px; flex: 1;
  }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  .card .value {{ font-size: 30px; font-weight: 700; margin-top: 6px; }}
  .card.good .value {{ color: var(--accent); }}
  .card.warn .value {{ color: var(--accent2); }}
  .section {{ margin-bottom: 36px; }}
  .section h2 {{ font-size: 16px; border-left: 3px solid var(--accent); padding-left: 10px; }}
  .chart-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
                 padding: 20px; max-width: 420px; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.data-table th {{
    text-align: left; color: var(--muted); font-weight: 600; padding: 8px 10px;
    border-bottom: 1px solid var(--border); text-transform: uppercase; font-size: 11px;
  }}
  table.data-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  table.data-table tr:hover td {{ background: #1c2540; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; }}
  .badge.exact_id {{ background: #14532d; color: #86efac; }}
  .badge.fuzzy_amount_date {{ background: #713f12; color: #fde68a; }}
  .badge.split_bank_entries {{ background: #1e3a8a; color: #93c5fd; }}
  .badge.llm_assisted {{ background: #581c87; color: #d8b4fe; }}
</style>
</head>
<body>
  <h1>Reconciliation Agent — Report</h1>
  <div class="subtitle">Generated {summary['generated_at']}</div>

  <div class="cards">
    <div class="card good">
      <div class="label">Match Rate</div>
      <div class="value">{summary['match_rate_pct']}%</div>
    </div>
    <div class="card">
      <div class="label">Settlements Considered</div>
      <div class="value">{summary['total_settlements_considered']}</div>
    </div>
    <div class="card good">
      <div class="label">Matched</div>
      <div class="value">{summary['matched_count']}</div>
    </div>
    <div class="card warn">
      <div class="label">Exceptions</div>
      <div class="value">{summary['exception_count']}</div>
    </div>
    <div class="card warn">
      <div class="label">Unexplained Bank Credits</div>
      <div class="value">{summary['unexplained_bank_credit_count']}</div>
    </div>
  </div>

  <div class="section">
    <h2>Match type breakdown</h2>
    <div class="chart-wrap">
      <canvas id="breakdownChart" width="380" height="260"></canvas>
    </div>
  </div>

  <div class="section">
    <h2>Matched settlements ({len(matched_df)})</h2>
    {df_to_table(matched_df)}
  </div>

  <div class="section">
    <h2>Exceptions — honest, unresolved cases ({len(exceptions_df)})</h2>
    {df_to_table(exceptions_df)}
  </div>

<script>
  const ctx = document.getElementById('breakdownChart');
  new Chart(ctx, {{
    type: 'doughnut',
    data: {{
      labels: {breakdown_labels},
      datasets: [{{
        data: {breakdown_values},
        backgroundColor: ['#6ee7b7', '#fde68a', '#93c5fd', '#d8b4fe', '#f87171'],
        borderWidth: 0,
      }}]
    }},
    options: {{
      plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#e6e9f0' }} }} }}
    }}
  }});
</script>
</body>
</html>
"""

with open("output/report.html", "w") as f:
    f.write(html)

print("Written: output/report.html")