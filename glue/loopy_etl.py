"""
╔══════════════════════════════════════════════════════════════╗
║  Loopy ETL — AWS Glue job                                    ║
║  raw S3 (payment events JSON)  →  curated S3 (Parquet)        ║
╚══════════════════════════════════════════════════════════════╝

The Loopy Pay service writes one JSON object per transaction to:
    s3://<raw>/payments/dt=YYYY/MM/DD/<txn_id>.json

This job (scheduled hourly by the Glue crawler + trigger) cleans those events,
derives daily fintech KPIs, and writes partitioned Parquet to:
    s3://<curated>/payments_curated/          (cleaned fact table)
    s3://<curated>/payments_daily_kpi/        (aggregated KPIs)

The curated tables are registered in the Glue Data Catalog so they are
immediately queryable from Amazon Athena / QuickSight.

This file is uploaded to S3 by Terraform and referenced by aws_glue_job.etl.
It is written so its pure transformation (`transform`) can also be unit-tested
locally with pandas — see glue/test_etl_local.py.
"""
import sys

# ── Glue runtime imports (present only on the Glue worker) ────
try:
    from awsglue.transforms import *          # noqa: F401,F403
    from awsglue.utils import getResolvedOptions
    from awsglue.context import GlueContext
    from awsglue.job import Job
    from pyspark.context import SparkContext
    from pyspark.sql import functions as F
    GLUE = True
except Exception:                              # local / unit-test mode
    GLUE = False


# ── pure transformation (Spark) ──────────────────────────────
def transform(df):
    """Clean the raw event frame and return (fact_df, kpi_df) as Spark frames."""
    fact = (
        df.where(F.col("txn_id").isNotNull())
          .withColumn("event_date", F.to_date(F.from_unixtime(F.col("ts"))))
          .withColumn("minutes", F.coalesce(F.col("minutes"), F.lit(0)))
          .withColumn("coins", F.coalesce(F.col("coins"), F.lit(0)))
          .dropDuplicates(["txn_id"])
    )
    kpi = (
        fact.groupBy("event_date", "type")
            .agg(
                F.count("*").alias("txn_count"),
                F.sum("minutes").alias("minutes_total"),
                F.sum("coins").alias("coins_total"),
                F.countDistinct("user").alias("active_users"),
            )
    )
    return fact, kpi


def main():
    args = getResolvedOptions(sys.argv, ["JOB_NAME", "RAW_BUCKET", "CURATED_BUCKET"])
    sc = SparkContext()
    glue = GlueContext(sc)
    spark = glue.spark_session
    job = Job(glue)
    job.init(args["JOB_NAME"], args)

    raw_path = f"s3://{args['RAW_BUCKET']}/payments/"
    cur_fact = f"s3://{args['CURATED_BUCKET']}/payments_curated/"
    cur_kpi = f"s3://{args['CURATED_BUCKET']}/payments_daily_kpi/"

    df = spark.read.json(raw_path)
    fact, kpi = transform(df)

    (fact.write.mode("overwrite").partitionBy("event_date").parquet(cur_fact))
    (kpi.write.mode("overwrite").partitionBy("event_date").parquet(cur_kpi))

    job.commit()


if __name__ == "__main__" and GLUE:
    main()
