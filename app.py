"""Minimal local FastAPI demo around Veriq's existing core engine."""
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Veriq")

def run_engine(data_dir):
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        shutil.copytree(data_dir, work / "data")
        for script in ("reconcile.py", "traction_story.py"):
            result = subprocess.run([sys.executable, str(ROOT / script)], cwd=work, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
        output = work / "output"
        return {"summary": json.loads((output / "summary.json").read_text()),
                "stories": json.loads((output / "transaction_stories.json").read_text()),
                "exceptions": list(csv.DictReader((output / "exceptions.csv").open(encoding="utf-8")))}

@app.get("/", response_class=HTMLResponse)
def home(): return HTML

@app.post("/api/demo")
def demo(): return run_engine(ROOT / "data")

@app.post("/api/upload")
async def upload(order_ledger: UploadFile = File(...), razorpay_settlement: UploadFile = File(...), bank_statement: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp)
        for uploaded, name in ((order_ledger, "order_ledger.csv"), (razorpay_settlement, "razorpay_settlement.csv"), (bank_statement, "bank_statement.csv")):
            if not uploaded.filename.lower().endswith(".csv"):
                raise HTTPException(400, "All uploads must be CSV files.")
            (data / name).write_bytes(await uploaded.read())
        try: return run_engine(data)
        except RuntimeError as exc: raise HTTPException(400, f"Could not reconcile uploaded data: {exc}")

HTML = """<!doctype html><html><head><meta charset='utf-8'><title>Veriq</title><style>
body{margin:auto;max-width:1100px;padding:48px;background:#0c1220;color:#edf2ff;font:15px Segoe UI,Arial}.brand{color:#6ee7b7;letter-spacing:.15em;font-weight:bold;font-size:12px}h1{font-size:42px;margin:8px 0}.sub,.muted{color:#9dacbf}.panel,.card{background:#151f32;border:1px solid #253550;border-radius:14px;padding:20px;margin:22px 0}.buttons,form{display:flex;gap:12px;flex-wrap:wrap}button{background:#6ee7b7;border:0;border-radius:9px;padding:12px 17px;font-weight:700;cursor:pointer}input{background:#0c1220;color:#dbeafe;border:1px solid #334155;padding:10px;border-radius:8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.card b{display:block;color:#6ee7b7;font-size:25px;margin-top:5px}.row{padding:13px;border-top:1px solid #253550;cursor:pointer}.row:hover{background:#1d2a42}.exception{color:#fca5a5}.timeline{border-left:2px solid #334155;padding-left:16px}.event{padding:7px 0}.hidden{display:none}</style></head><body>
<div class='brand'>VERIQ · MULTI-SOURCE RECONCILIATION</div><h1>Don’t just match transactions. Understand them.</h1><p class='sub'>Evidence-first reconciliation across merchant ledger, Razorpay settlements, and bank movements.</p><div class='buttons'><button onclick="run('/api/demo')">Try Demo Dataset</button><button onclick="document.querySelector('form').classList.toggle('hidden')">Upload Your Data</button></div><form class='panel hidden' onsubmit='send(event)'><label>Order Ledger <input name='order_ledger' type='file' accept='.csv' required></label><label>Razorpay Settlement <input name='razorpay_settlement' type='file' accept='.csv' required></label><label>Bank Statement <input name='bank_statement' type='file' accept='.csv' required></label><button>Run Reconciliation</button></form><div id='status' class='muted'></div><div id='results'></div><script>
async function run(u,b){status.textContent='Running reconciliation…';let r=await fetch(u,{method:'POST',body:b}),d=await r.json();if(!r.ok){status.textContent=d.detail;return}status.textContent='';show(d)}async function send(e){e.preventDefault();run('/api/upload',new FormData(e.target))}function show(d){let s=d.summary,g=s.ground_truth_scoring||{};results.innerHTML=`<div class='metrics'>${[['Transactions',s.total_settlements_considered],['Reconciled',s.matched_count],['Exceptions',s.exception_count],['Unexplained credits',s.unexplained_bank_credit_count],['Precision',g.precision==null?'—':(g.precision*100).toFixed(1)+'%'],['Recall',g.recall==null?'—':(g.recall*100).toFixed(1)+'%']].map(x=>`<div class='card'><span class='muted'>${x[0]}</span><b>${x[1]}</b></div>`).join('')}</div><div class='panel'><h2>Match breakdown</h2>${Object.entries(s.match_type_breakdown).map(x=>`<span class='card'>${x[0]} <b>${x[1]}</b></span>`).join('')} <span class='exception'>Exceptions: ${s.exception_count}</span></div><div class='panel'><h2>Transaction explorer</h2>${d.stories.map((x,i)=>`<div class='row' onclick='detail(${i})'><b>${x.order_id}</b> · ${x.status} · ${(x.confidence*100).toFixed(0)}%<div class='muted'>${x.summary}</div></div>`).join('')}</div><div class='panel'><h2>Classified exceptions</h2>${d.exceptions.map(x=>`<div class='row exception'><b>${x.order_id}</b>: ${x.reason}</div>`).join('')||'None'}</div>`;window.stories=d.stories}function detail(i){let x=stories[i];results.insertAdjacentHTML('afterbegin',`<div class='panel'><h2>${x.order_id} — ${x.status}</h2><p>${x.summary}</p><div class='timeline'>${x.events.map(e=>`<div class='event'><b>${e.date}</b> · ${e.text}</div>`).join('')}</div><p><b>Evidence:</b> connected settlement and bank records, amount/date constraints, and match notes. Alternatives are rejected when they fail those constraints.</p></div>`);scrollTo(0,0)}</script></body></html>"""
