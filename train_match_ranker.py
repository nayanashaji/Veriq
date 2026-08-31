"""Train a Spark ML candidate-ranking model from approved analyst decisions.

It intentionally refuses to train until real reviewer labels exist. The model
is for ranking review candidates, never for bypassing the evidence rules.
"""
import argparse
from pyspark.sql import SparkSession, functions as F
from pyspark.ml.feature import VectorAssembler
from pyspark.ml.classification import LogisticRegression


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse", default="warehouse")
    parser.add_argument("--model-path", default="warehouse/models/candidate_ranker")
    args = parser.parse_args()
    spark = SparkSession.builder.appName("veriq-candidate-ranker").master("local[*]").getOrCreate()
    try:
        labels = spark.read.parquet(f"{args.warehouse}/review_labels").filter(F.lower("approved").isin("true", "1", "yes"))
    except Exception:
        print("No reviewer labels found. Add data/reviewer_decisions.csv and run scalable_reconcile.py first.")
        return
    edges = spark.read.parquet(f"{args.warehouse}/candidate_edges")
    training = (edges.join(labels.select("settlement_id", "actual_bank_txn_id"), "settlement_id")
        .withColumn("label", (F.col("bank_txn_id") == F.col("actual_bank_txn_id")).cast("double"))
        .withColumn("has_id_evidence", (F.col("id_evidence") > 0).cast("double")))
    if training.limit(1).count() == 0 or training.select("label").distinct().count() < 2:
        print("Need both approved and rejected candidate examples before training.")
        return
    features = VectorAssembler(inputCols=["amount_difference", "date_difference", "has_id_evidence"], outputCol="features")
    model = LogisticRegression(featuresCol="features", labelCol="label", maxIter=50).fit(features.transform(training))
    model.write().overwrite().save(args.model_path)
    print(f"Saved review-candidate ranking model to {args.model_path}")


if __name__ == "__main__":
    main()
