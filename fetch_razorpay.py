"""Fetch Razorpay *Test Mode* settlement reconciliation data without altering demo data.

Writes raw API JSON and a normalized settlement CSV under data/razorpay_test_mode/.
It does not invent bank or ledger records, so this mode has no synthetic ground
truth and must not be used to claim precision/recall.
"""
import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import base64
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

API = "https://api.razorpay.com/v1/settlements/recon/combined"

def money(paise): return str((Decimal(str(paise or 0)) / Decimal("100")).quantize(Decimal("0.01")))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--month", type=int, default=datetime.now().month)
    parser.add_argument("--day", type=int)
    parser.add_argument("--count", type=int, default=1000)
    args = parser.parse_args()
    key, secret = os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")
    if not key or not secret:
        raise SystemExit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in your environment. No request was made.")
    params = {"year": args.year, "month": args.month, "count": args.count}
    if args.day: params["day"] = args.day
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    request = Request(f"{API}?{urlencode(params)}", headers={"Authorization": f"Basic {token}"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise SystemExit(f"Razorpay API returned HTTP {exc.code}. Check Test Mode keys and requested period.")
    except URLError as exc:
        raise SystemExit(f"Could not reach Razorpay API: {exc.reason}")
    output = Path("data/razorpay_test_mode")
    output.mkdir(parents=True, exist_ok=True)
    (output / "raw_settlement_recon.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    rows = []
    for item in payload.get("items", []):
        event_type = item.get("type", "payment")
        net = Decimal(money(item.get("credit"))) - Decimal(money(item.get("debit")))
        created = datetime.fromtimestamp(item.get("settled_at") or item.get("created_at"), timezone.utc).date().isoformat()
        rows.append({"settlement_id": item.get("entity_id") or item.get("id"), "order_id": item.get("order_receipt") or item.get("order_id") or "UNKNOWN_ORDER",
                     "gross_amount": money(item.get("amount")), "fee": money(item.get("fee")), "tax": money(item.get("tax")),
                     "net_amount": str(net), "settlement_date": created,
                     "type": {"payment":"settlement", "refund":"refund", "adjustment":"settlement_adjustment"}.get(event_type, event_type),
                     "currency": item.get("currency", "INR"), "source": "razorpay_test_mode"})
    pd.DataFrame(rows).to_csv(output / "razorpay_settlement.csv", index=False)
    (output / "README.txt").write_text("Razorpay Test Mode API export. It is not synthetic evaluation data and has no ground truth. Supply matching merchant ledger and bank statement exports before running Veriq.\n", encoding="utf-8")
    print(f"Saved {len(rows)} Test Mode recon records to {output}. Synthetic data was not changed.")

if __name__ == "__main__": main()
