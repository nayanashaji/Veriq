"""Judge-ready evaluation of the core Python reconciliation engine."""
import json
import os
import pandas as pd


def bank_ids(value):
    return {item for item in str(value).replace("+", ";").split(";") if item and item != "nan"}


def main():
    gt = pd.read_csv("data/ground_truth.csv", dtype=str).fillna("")
    matches = pd.concat([pd.read_csv("output/auto_accepted.csv"), pd.read_csv("output/needs_review.csv")], ignore_index=True)
    predicted = {row["settlement_id"]: bank_ids(row["matched_bank_txn_ids"]) for _, row in matches.iterrows()}
    rows = []
    for _, truth in gt.iterrows():
        expected = bank_ids(truth["expected_bank_ids"] or truth["true_bank_txn_ids"])
        actual = predicted.get(truth["settlement_id"], set())
        should_match = truth["expected_outcome"] == "RECONCILED"
        if should_match and actual == expected:
            result = "TP"
        elif should_match and actual:
            result = "FP_FN"  # wrong forced match: counts against both metrics
        elif should_match:
            result = "FN"
        elif actual:
            result = "FP"
        else:
            result = "TN"
        rows.append({"settlement_id": truth["settlement_id"], "case_type": truth["case_type"], "expected_outcome": truth["expected_outcome"], "result": result})
    results = pd.DataFrame(rows)
    tp = int((results.result == "TP").sum())
    fp = int(results.result.isin(["FP", "FP_FN"]).sum())
    fn = int(results.result.isin(["FN", "FP_FN"]).sum())
    tn = int((results.result == "TN").sum())
    report = {
        "total_records": len(results), "true_positives": tp, "false_positives": fp,
        "false_negatives": fn, "true_negatives": tn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "match_rate_pct": round(100 * len(matches) / len(results), 1),
        "unresolved_exceptions": int(len(pd.read_csv("output/exceptions.csv"))),
        "incorrect_forced_matches": int((results.result == "FP_FN").sum()),
        "by_case_type": results.groupby("case_type")["result"].agg(
            total="count", true_positives=lambda s: int((s == "TP").sum()),
            exceptions=lambda s: int(s.isin(["FN", "TN"]).sum()),
            incorrect_forced_matches=lambda s: int(s.isin(["FP", "FP_FN"]).sum()),
        ).reset_index().to_dict("records"),
    }
    os.makedirs("output", exist_ok=True)
    with open("output/evaluation.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
