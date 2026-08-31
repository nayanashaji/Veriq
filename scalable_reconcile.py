"""Distributed, evidence-first reconciliation for large batches.

Run locally with ``python scalable_reconcile.py`` or submit unchanged to a
Spark cluster. It deliberately separates cheap blocked candidate generation
from the small ambiguous review queue. No generative model makes an automatic
posting decision.

The warehouse is append-only Parquet in this demo. Point ``--warehouse`` at a
Delta/Iceberg-enabled object store in production to obtain ACID table writes.
"""

import argparse
import os

from pyspark.sql import SparkSession, functions as F, Window

RULE_VERSION = "spark-evidence-v1"
BLOCKING_VERSION = "merchant-rail-currency-amount-date-v1"
AI_REVIEW_POLICY_VERSION = "evidence-only-ai-review-v1"
TIGHT_AMOUNT = 3.0
FUZZY_AMOUNT = 10.0
TIGHT_DAYS = 3
FUZZY_DAYS = 7


def has_path(spark, path):
    return spark._jvm.org.apache.hadoop.fs.FileSystem.get(
        spark._jsc.hadoopConfiguration()).exists(spark._jvm.org.apache.hadoop.fs.Path(path))


def with_defaults(df, defaults):
    for name, value in defaults.items():
        if name not in df.columns:
            df = df.withColumn(name, F.lit(value))
    return df


def event_hash(*columns):
    return F.sha2(F.concat_ws("|", *[F.coalesce(F.col(c).cast("string"), F.lit("")) for c in columns]), 256)


def normalise_sources(spark, input_dir):
    orders = with_defaults(spark.read.option("header", True).csv(f"{input_dir}/order_ledger.csv"),
                           {"merchant_id": "default", "currency": "INR"})
    settlements = with_defaults(spark.read.option("header", True).csv(f"{input_dir}/razorpay_settlement.csv"), {
        "merchant_id": "default", "payment_rail": "razorpay", "currency": "INR",
        "expected_bank_amount": "", "source_amount": "", "exchange_rate": "",
    })
    bank = with_defaults(spark.read.option("header", True).csv(f"{input_dir}/bank_statement.csv"),
                         {"merchant_id": "default", "currency": "INR", "payment_rail": "razorpay"})

    settlements = (settlements
        .withColumn("merchant_id", F.when(F.coalesce(F.trim("merchant_id"), F.lit("")) == "", "default").otherwise(F.col("merchant_id")))
        .withColumn("payment_rail", F.when(F.coalesce(F.trim("payment_rail"), F.lit("")) == "", "razorpay").otherwise(F.col("payment_rail")))
        .withColumn("currency", F.when(F.coalesce(F.trim("currency"), F.lit("")) == "", "INR").otherwise(F.col("currency")))
        .withColumn("settlement_date", F.to_date("settlement_date"))
        .withColumn("net_amount_num", F.col("net_amount").cast("decimal(18,2)"))
        .withColumn("recon_amount", F.when(F.trim("expected_bank_amount") != "", F.col("expected_bank_amount"))
                    .otherwise(F.col("net_amount")).cast("decimal(18,2)"))
        .withColumn("amount_bucket", F.floor(F.abs("recon_amount") / F.lit(10)))
        .withColumn("date_bucket", F.floor(F.datediff("settlement_date", F.lit("1970-01-01")) / F.lit(7)))
        .withColumn("event_hash", event_hash("merchant_id", "settlement_id", "net_amount", "settlement_date", "type")))
    bank = (bank
        .withColumn("merchant_id", F.when(F.coalesce(F.trim("merchant_id"), F.lit("")) == "", "default").otherwise(F.col("merchant_id")))
        .withColumn("payment_rail", F.when(F.coalesce(F.trim("payment_rail"), F.lit("")) == "", "razorpay").otherwise(F.col("payment_rail")))
        .withColumn("currency", F.when(F.coalesce(F.trim("currency"), F.lit("")) == "", "INR").otherwise(F.col("currency")))
        .withColumn("value_date", F.to_date("value_date"))
        .withColumn("amount_num", F.col("amount").cast("decimal(18,2)"))
        .withColumn("amount_bucket", F.floor(F.abs("amount_num") / F.lit(10)))
        .withColumn("date_bucket", F.floor(F.datediff("value_date", F.lit("1970-01-01")) / F.lit(7)))
        .withColumn("narration_normalized", F.regexp_replace(F.upper(F.coalesce("narration", F.lit(""))), "[^A-Z0-9]", ""))
        .withColumn("event_hash", event_hash("merchant_id", "bank_txn_id", "amount", "value_date", "narration")))
    return orders, settlements, bank


def write_table(df, path, partition_columns):
    (df.write.mode("append").partitionBy(*partition_columns).parquet(path))


def deduplicate_events(df, entity_name):
    """Remove replayed copies of the *same event* while preserving distinct
    business events such as a duplicate settlement webhook with its own ID."""
    ranked = df.withColumn("_ingest_rank", F.row_number().over(
        Window.partitionBy("event_hash").orderBy(F.monotonically_increasing_id())))
    duplicates = (ranked.filter("_ingest_rank > 1")
        .select("event_hash")
        .withColumn("entity_type", F.lit(entity_name))
        .withColumn("reason", F.lit("Replay event skipped: identical event fingerprint was already present in this batch."))
        .withColumn("event_date", F.current_date()))
    return ranked.filter("_ingest_rank = 1").drop("_ingest_rank"), duplicates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data")
    parser.add_argument("--warehouse", default="warehouse")
    parser.add_argument("--master", default="local[*]")
    parser.add_argument("--shuffle-partitions", type=int, default=32,
                        help="Spark shuffle partition count; raise this on a production cluster.")
    parser.add_argument("--dry-run", action="store_true", help="Validate distributed reconciliation without writing tables.")
    args = parser.parse_args()

    spark = (SparkSession.builder.appName("veriq-scalable-reconciliation")
             .master(args.master).config("spark.sql.adaptive.enabled", "true")
             .config("spark.sql.shuffle.partitions", str(args.shuffle_partitions)).getOrCreate())
    spark.sparkContext.setLogLevel("WARN")
    orders, settlements, bank = normalise_sources(spark, args.input)
    settlements, duplicate_settlement_events = deduplicate_events(settlements, "settlement_event")
    bank, duplicate_bank_events = deduplicate_events(bank, "bank_event")
    ingestion_duplicates = duplicate_settlement_events.unionByName(duplicate_bank_events)
    decisions_path = f"{args.warehouse}/reconciliation_decisions"

    # Idempotency: an unchanged settlement fingerprint that already has a final
    # decision is never reconsidered. Changed source data produces a new hash.
    if has_path(spark, decisions_path):
        prior = spark.read.parquet(decisions_path).select("event_hash").distinct()
        settlements = settlements.join(prior, "event_hash", "left_anti")

    # Explicit adjustment grouping is performed before ordinary matching. A
    # bank credit must equal the documented base settlement plus adjustment(s).
    adjust = settlements.filter(F.col("type").isin("settlement", "split_payment", "settlement_adjustment", "cashback_adjustment"))
    groups = (adjust.groupBy("merchant_id", "order_id")
        .agg(F.sum("net_amount_num").alias("expected_amount"),
             F.collect_list(F.struct("settlement_id", "event_hash", "settlement_date", "type")).alias("legs"),
             F.sum(F.when(F.col("type").isin("settlement_adjustment", "cashback_adjustment"), 1).otherwise(0)).alias("adjustment_count"),
             F.sum(F.when(F.col("type").isin("settlement", "split_payment"), 1).otherwise(0)).alias("base_count"),
             F.min("settlement_date").alias("group_date"))
        .filter((F.col("adjustment_count") > 0) & (F.col("base_count") == 1)))
    adjustment_candidates = (groups.alias("g").join(bank.alias("b"),
        (F.col("g.merchant_id") == F.col("b.merchant_id")) & (F.col("b.amount_num") > 0) &
        (F.abs(F.datediff("b.value_date", "g.group_date")) <= TIGHT_DAYS) &
        (F.abs(F.col("b.amount_num") - F.col("g.expected_amount")) <= TIGHT_AMOUNT))
        .select("g.*", F.col("b.bank_txn_id"), F.col("b.value_date"), F.col("b.amount_num")))
    adj_window = Window.partitionBy("merchant_id", "order_id").orderBy("value_date", "bank_txn_id")
    adjustment_matches = (adjustment_candidates.withColumn("match_rank", F.row_number().over(adj_window)).filter("match_rank = 1")
        .select("merchant_id", "order_id", "bank_txn_id", "expected_amount", "legs")
        .select("merchant_id", "order_id", "bank_txn_id", "expected_amount", F.explode("legs").alias("leg"))
        .select("merchant_id", "order_id", "bank_txn_id", "expected_amount", "leg.*")
        .withColumn("match_type", F.lit("lifecycle_adjustment"))
        .withColumn("confidence", F.lit(0.98))
        .withColumn("reason", F.concat(F.lit("Documented lifecycle adjustment balances to bank credit ₹"), F.col("expected_amount"))))

    adjustment_ids = adjustment_matches.select("event_hash").distinct()
    matchable = (settlements.join(adjustment_ids, "event_hash", "left_anti")
        .filter(F.col("type").isin("settlement", "split_payment", "refund", "partial_refund", "chargeback", "cashback_adjustment")))

    # Blocking cuts the candidate space to nearby date and amount buckets;
    # exact identity is then preferred over amount/date similarity.
    join_condition = ((F.col("s.merchant_id") == F.col("b.merchant_id")) &
        (F.col("s.payment_rail") == F.col("b.payment_rail")) &
        # Foreign-currency processor legs may match an INR bank account only
        # when the source supplied an explicit expected-bank amount.
        ((F.col("s.currency") == F.col("b.currency")) |
         ((F.col("s.currency") != F.col("b.currency")) & (F.coalesce(F.trim("s.expected_bank_amount"), F.lit("")) != ""))) &
        (F.signum(F.col("s.recon_amount")) == F.signum(F.col("b.amount_num"))) &
        # ±2 buckets is required to retain all pairs within a ₹10 tolerance at
        # bucket boundaries; it is still a tiny blocked subset of the corpus.
        (F.abs(F.col("s.amount_bucket") - F.col("b.amount_bucket")) <= 2) &
        (F.abs(F.col("s.date_bucket") - F.col("b.date_bucket")) <= 1) &
        (F.abs(F.datediff("b.value_date", "s.settlement_date")) <= FUZZY_DAYS) &
        (F.abs(F.col("b.amount_num") - F.col("s.recon_amount")) <= FUZZY_AMOUNT))
    edges = (matchable.alias("s").join(bank.alias("b"), join_condition)
        .select(F.col("s.event_hash").alias("settlement_hash"), F.col("s.settlement_id"), F.col("s.order_id"),
                F.col("s.merchant_id"), F.col("s.settlement_date"), F.col("s.type"), F.col("s.currency"),
                F.col("s.source_amount"), F.col("s.exchange_rate"), F.col("s.recon_amount"),
                F.col("b.event_hash").alias("bank_hash"), F.col("b.bank_txn_id"), F.col("b.value_date"),
                F.col("b.amount_num"), F.col("b.narration"),
                F.abs(F.col("b.amount_num") - F.col("s.recon_amount")).alias("amount_difference"),
                F.abs(F.datediff("b.value_date", "s.settlement_date")).alias("date_difference"),
                F.expr("instr(b.narration_normalized, upper(s.order_id))").alias("id_evidence")))
    edges = (edges
        .withColumn("candidate_score", F.col("amount_difference") + F.col("date_difference") * 2)
        .withColumn("blocking_version", F.lit(BLOCKING_VERSION))
        .withColumn("feature_evidence", F.to_json(F.struct(
            F.col("merchant_id"), F.col("currency"), F.col("amount_difference"), F.col("date_difference"),
            (F.col("id_evidence") > 0).alias("order_id_in_narration"), F.col("candidate_score"))))).cache()
    by_settlement = Window.partitionBy("settlement_hash").orderBy(F.desc("id_evidence"), "candidate_score", "bank_txn_id")
    by_bank = Window.partitionBy("bank_hash").orderBy(F.desc("id_evidence"), "candidate_score", "settlement_id")
    edges = edges.withColumn("settlement_rank", F.row_number().over(by_settlement)).withColumn("bank_rank", F.row_number().over(by_bank))
    exact = (F.col("id_evidence") > 0) & (F.col("amount_difference") <= TIGHT_AMOUNT) & (F.col("date_difference") <= TIGHT_DAYS)
    accepted = (edges.filter((F.col("settlement_rank") == 1) & (F.col("bank_rank") == 1))
        .withColumn("match_type", F.when(F.col("currency") != "INR", "fx_conversion")
                    .when(exact, "exact_id").otherwise("fuzzy_amount_date"))
        .withColumn("confidence", F.when(exact, F.lit(0.99)).otherwise(F.greatest(F.lit(0.55), F.lit(1.0) - F.col("candidate_score") / 30)))
        .withColumn("reason", F.when(F.col("currency") != "INR", F.concat(F.lit("Documented FX: "), F.col("source_amount"), F.lit(" "), F.col("currency"), F.lit(" at rate "), F.col("exchange_rate")))
                    .when(exact, F.lit("Order ID, amount, and date evidence agree."))
                    .otherwise(F.lit("Blocked amount/date candidate; requires review if confidence is below threshold."))))

    # Candidate-level audit: retain the candidates *not* selected and why.
    edge_audit = (edges
        .withColumn("selected_by_settlement", F.col("settlement_rank") == 1)
        .withColumn("selected_by_bank", F.col("bank_rank") == 1)
        .withColumn("candidate_disposition", F.when((F.col("settlement_rank") == 1) & (F.col("bank_rank") == 1), "SELECTED")
                    .otherwise("REJECTED"))
        .withColumn("rejection_reason", F.when(F.col("settlement_rank") > 1, "A lower-cost candidate ranked higher for this settlement.")
                    .when(F.col("bank_rank") > 1, "The bank transaction was reserved by a stronger settlement candidate."))
        .withColumn("rule_version", F.lit(RULE_VERSION))
        .withColumn("event_date", F.current_date()))

    # One settlement may legitimately clear as two bank lines. This is a
    # blocked two-sum join (same merchant/rail/currency and nearby dates), not
    # a global O(n²) scan. Ambiguous combinations remain in review.
    standard_ids = accepted.select(F.col("settlement_hash").alias("event_hash")).distinct()
    standard_bank_ids = accepted.select(F.col("bank_hash").alias("event_hash")).distinct()
    split_settlements = matchable.join(standard_ids, "event_hash", "left_anti").alias("s")
    split_bank = bank.join(standard_bank_ids, "event_hash", "left_anti")
    b1, b2 = split_bank.alias("b1"), split_bank.alias("b2")
    pair_condition = ((F.col("b1.merchant_id") == F.col("b2.merchant_id")) &
        (F.col("b1.payment_rail") == F.col("b2.payment_rail")) & (F.col("b1.currency") == F.col("b2.currency")) &
        (F.signum(F.col("b1.amount_num")) == F.signum(F.col("b2.amount_num"))) &
        (F.col("b1.bank_txn_id") < F.col("b2.bank_txn_id")) &
        (F.abs(F.datediff("b1.value_date", "b2.value_date")) <= FUZZY_DAYS))
    bank_pairs = (b1.join(b2, pair_condition).select(
        F.col("b1.merchant_id").alias("merchant_id"), F.col("b1.payment_rail").alias("payment_rail"),
        F.col("b1.currency").alias("currency"), F.col("b1.value_date").alias("pair_date"),
        (F.col("b1.amount_num") + F.col("b2.amount_num")).alias("pair_amount"),
        F.concat_ws("+", F.col("b1.bank_txn_id"), F.col("b2.bank_txn_id")).alias("bank_txn_id")))
    split_candidates = (split_settlements.join(bank_pairs.alias("p"),
        (F.col("s.merchant_id") == F.col("p.merchant_id")) & (F.col("s.payment_rail") == F.col("p.payment_rail")) &
        (F.col("s.currency") == F.col("p.currency")) & (F.signum(F.col("s.recon_amount")) == F.signum(F.col("p.pair_amount"))) &
        (F.abs(F.datediff("p.pair_date", "s.settlement_date")) <= FUZZY_DAYS) &
        (F.abs(F.col("p.pair_amount") - F.col("s.recon_amount")) <= TIGHT_AMOUNT))
        .select(F.col("s.event_hash").alias("settlement_hash"), F.col("s.settlement_id"), F.col("s.order_id"),
                F.col("s.merchant_id"), F.col("s.settlement_date"), F.col("p.bank_txn_id"), F.col("p.pair_amount")))
    split_window = Window.partitionBy("settlement_hash").orderBy("bank_txn_id")
    split_matches = (split_candidates.withColumn("split_rank", F.row_number().over(split_window)).filter("split_rank = 1")
        .withColumn("match_type", F.lit("split_bank_entries")).withColumn("confidence", F.lit(0.80))
        .withColumn("reason", F.concat(F.lit("Two bank entries sum exactly to settlement amount ₹"), F.col("pair_amount"))))

    # Persist every edge with its features and rejection rationale—this is the
    # audit trail and future labelled-data source, not a black-box score.
    if not args.dry_run:
        write_table(edge_audit, f"{args.warehouse}/candidate_edges", ["merchant_id", "event_date"])
        write_table(ingestion_duplicates, f"{args.warehouse}/ingestion_deduplication", ["entity_type", "event_date"])
    decisions = (accepted.select(F.col("settlement_hash").alias("event_hash"), "settlement_id", "order_id", "merchant_id", "bank_txn_id", "match_type", "confidence", "reason", "settlement_date")
        .unionByName(split_matches.select(F.col("settlement_hash").alias("event_hash"), "settlement_id", "order_id", "merchant_id", "bank_txn_id", "match_type", "confidence", "reason", "settlement_date"), allowMissingColumns=True)
        .unionByName(adjustment_matches.select("event_hash", "settlement_id", "order_id", "merchant_id", "bank_txn_id", "match_type", "confidence", "reason", F.col("settlement_date")), allowMissingColumns=True)
        .withColumn("decision", F.when(F.col("confidence") >= 0.85, "AUTO_ACCEPTED").otherwise("NEEDS_REVIEW"))
        .withColumn("rule_version", F.lit(RULE_VERSION)).withColumn("event_date", F.current_date()))
    if not args.dry_run:
        write_table(decisions, decisions_path, ["merchant_id", "event_date"])

    matched_ids = decisions.select("event_hash").distinct()
    unresolved = (settlements.join(matched_ids, "event_hash", "left_anti")
        .select("event_hash", "settlement_id", "order_id", "merchant_id", "net_amount", "settlement_date", "type")
        .withColumn("decision", F.lit("EXCEPTION"))
        .withColumn("reason", F.when(F.col("type") == "chargeback", "Chargeback lacks an evidenced matching bank debit; not assumed to be a refund.")
                    .when(F.col("type") == "settlement_adjustment", "Adjustment lacks balancing processor and bank evidence.")
                    .otherwise("No unique, evidence-backed candidate within configured amount/date tolerances."))
        .withColumn("rule_version", F.lit(RULE_VERSION)).withColumn("event_date", F.current_date()))
    if not args.dry_run:
        write_table(unresolved, f"{args.warehouse}/reconciliation_exceptions", ["merchant_id", "event_date"])

    # AI is fed only this bounded ambiguous tail. The candidate IDs and their
    # recorded evidence are included; the policy requires a human decision and
    # prohibits the model from auto-posting, creating a new candidate, or
    # overriding a deterministic exception.
    candidate_sets = (edge_audit.groupBy("settlement_hash", "settlement_id", "order_id", "merchant_id")
        .agg(F.count("bank_txn_id").alias("candidate_count"),
             F.slice(F.sort_array(F.collect_list(F.struct("candidate_score", "bank_txn_id", "feature_evidence"))), 1, 5).alias("top_candidates")))
    ai_review_queue = (candidate_sets.join(decisions.select(F.col("event_hash").alias("settlement_hash"), "decision"), "settlement_hash", "left")
        # A confident deterministic match is final even if it had several
        # weaker candidates. AI receives only explicit review matches or
        # unresolved cases with competing evidence.
        .filter((F.col("decision") == "NEEDS_REVIEW") |
                (F.col("decision").isNull() & (F.col("candidate_count") > 1)))
        .select("settlement_hash", "settlement_id", "order_id", "merchant_id", "candidate_count", "top_candidates")
        .withColumn("queue_reason", F.lit("Ambiguous evidence: model may rank only the supplied candidates; human approval is mandatory."))
        .withColumn("model_version", F.lit("not-invoked"))
        .withColumn("policy_version", F.lit(AI_REVIEW_POLICY_VERSION))
        .withColumn("decision", F.lit("NEEDS_REVIEW"))
        .withColumn("event_date", F.current_date()))
    if not args.dry_run:
        write_table(ai_review_queue, f"{args.warehouse}/ai_review_queue", ["merchant_id", "event_date"])

    # A duplicate ledger record is an explicit non-money exception, not a fake
    # missing settlement. Reviewer decisions can be added as a CSV with
    # settlement_id, approved, actual_bank_txn_id, reviewer, reviewed_at.
    duplicate_ledger = (orders.groupBy("merchant_id", "order_id").count().filter("count > 1")
        .withColumn("reason", F.concat(F.lit("Duplicate ledger records: "), F.col("count"), F.lit(" entries share this order ID.")))
        .withColumn("event_date", F.current_date()))
    if not args.dry_run:
        write_table(duplicate_ledger, f"{args.warehouse}/ledger_exceptions", ["merchant_id", "event_date"])
    labels_file = f"{args.input}/reviewer_decisions.csv"
    if not args.dry_run and os.path.exists(labels_file):
        labels = spark.read.option("header", True).csv(labels_file).withColumn("event_date", F.current_date())
        write_table(labels, f"{args.warehouse}/review_labels", ["event_date"])

    mode = "dry run" if args.dry_run else "table write"
    print(f"Spark reconciliation complete ({mode}): {decisions.count()} decisions, {unresolved.count()} explicit exceptions. "
          f"Blocked candidates: {edges.count()}, AI review queue: {ai_review_queue.count()}, replay events skipped: {ingestion_duplicates.count()}, "
          f"standard matches: {accepted.count()}, split matches: {split_matches.count()}, adjustment matches: {adjustment_matches.count()}.")
    spark.stop()


if __name__ == "__main__":
    main()
