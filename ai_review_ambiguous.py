"""Optional, constrained AI triage for Veriq's ambiguous tail.

It reads only the bounded ``ai_review_queue`` and records a recommendation.
The model may choose only a supplied bank transaction ID or ``null``. It never
writes a reconciliation decision, posts money, or turns an exception into a
match; an analyst must supply the final reviewer label separately.
"""

import argparse
import json
import os

from pyspark.sql import SparkSession, functions as F

DEFAULT_MODEL = "claude-sonnet-4-5"
POLICY_VERSION = "evidence-only-ai-review-v1"


def parse_recommendation(raw, permitted_ids):
    """Fail closed: malformed, invented, or unsupported answers become null."""
    try:
        item = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, "Model response was not valid JSON; no recommendation recorded."
    candidate = item.get("recommended_bank_txn_id")
    if candidate is not None and candidate not in permitted_ids:
        return None, "Model proposed an ID outside the supplied candidates; rejected by policy."
    return candidate, str(item.get("reason", "No explanation supplied."))[:1000]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse", default="warehouse")
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required. No queue item was sent to a model.")
    if not args.dry_run:
        try:
            import anthropic
        except ImportError as exc:
            raise SystemExit("Install requirements.txt to enable AI review.") from exc

    spark = SparkSession.builder.appName("veriq-ai-review").getOrCreate()
    queue = (spark.read.parquet(f"{args.warehouse}/ai_review_queue")
             .orderBy("event_date", "settlement_id").limit(args.limit))
    rows = queue.toLocalIterator()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]) if not args.dry_run else None
    recommendations = []
    for row in rows:
        item = row.asDict(recursive=True)
        candidates = item["top_candidates"]
        permitted_ids = {candidate["bank_txn_id"] for candidate in candidates}
        prompt = {
            "task": "Recommend a review candidate only when provided evidence directly supports it.",
            "constraints": [
                "Choose recommended_bank_txn_id only from candidate_bank_txn_ids, or null.",
                "Do not infer missing events, amounts, fees, or dates.",
                "This is a human-review recommendation, not an automatic match.",
                "Reply with one JSON object only.",
            ],
            "settlement_id": item["settlement_id"],
            "candidate_bank_txn_ids": sorted(permitted_ids),
            "candidate_evidence": candidates,
            "response_schema": {"recommended_bank_txn_id": "candidate ID or null", "reason": "short evidence-based explanation"},
        }
        if args.dry_run:
            recommendation, reason = None, "Dry run: model was not invoked."
        else:
            response = client.messages.create(
                model=args.model, max_tokens=300,
                messages=[{"role": "user", "content": json.dumps(prompt)}],
            )
            recommendation, reason = parse_recommendation(response.content[0].text, permitted_ids)
        recommendations.append((item["settlement_hash"], item["settlement_id"], item["order_id"], item["merchant_id"],
                                recommendation, reason, args.model, POLICY_VERSION))

    result = spark.createDataFrame(recommendations, [
        "settlement_hash", "settlement_id", "order_id", "merchant_id", "recommended_bank_txn_id",
        "reason", "model_version", "policy_version",
    ]).withColumn("decision", F.lit("AI_RECOMMENDATION_REQUIRES_HUMAN_REVIEW")).withColumn("event_date", F.current_date())
    if args.dry_run:
        print(f"AI review dry run complete: {result.count()} queue item(s); model was not invoked.")
    else:
        result.write.mode("append").partitionBy("merchant_id", "event_date").parquet(f"{args.warehouse}/ai_recommendations")
        print(f"Recorded {result.count()} AI recommendations. No reconciliation decision was changed.")
    spark.stop()


if __name__ == "__main__":
    main()
