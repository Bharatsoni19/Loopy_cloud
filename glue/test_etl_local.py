"""
Local, Spark-free mirror of the Glue ETL transformation — lets us unit-test the
business logic on a laptop / in CI without standing up a Glue cluster. The
aggregation here matches glue/loopy_etl.py:transform() one-for-one.

Run:  python glue/test_etl_local.py
"""
import datetime as dt
import json
import os
import tempfile

import pandas as pd


def transform_pandas(records: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.DataFrame(records)
    df = df[df["txn_id"].notna()].drop_duplicates("txn_id").copy()
    df["event_date"] = pd.to_datetime(df["ts"], unit="s").dt.date
    df["minutes"] = df.get("minutes", 0)
    df["coins"] = df.get("coins", 0)
    df["minutes"] = df["minutes"].fillna(0)
    df["coins"] = df["coins"].fillna(0)
    kpi = (df.groupby(["event_date", "type"])
             .agg(txn_count=("txn_id", "count"),
                  minutes_total=("minutes", "sum"),
                  coins_total=("coins", "sum"),
                  active_users=("user", pd.Series.nunique))
             .reset_index())
    return df, kpi


def _sample_events() -> list[dict]:
    base = dt.datetime(2026, 5, 27, 10, 0).timestamp()
    return [
        {"txn_id": "t1", "type": "issue",    "user": "bharat", "ts": base},
        {"txn_id": "t2", "type": "recharge", "user": "bharat", "ts": base, "minutes": 60, "coins": 120},
        {"txn_id": "t3", "type": "recharge", "user": "riya",   "ts": base, "minutes": 60, "coins": 120},
        {"txn_id": "t4", "type": "transfer", "user": "bharat", "ts": base, "minutes": 25},
        {"txn_id": "t4", "type": "transfer", "user": "bharat", "ts": base, "minutes": 25},  # dup
    ]


def main():
    fact, kpi = transform_pandas(_sample_events())
    assert len(fact) == 4, "duplicate txn_id should be removed"
    recharge = kpi[kpi["type"] == "recharge"].iloc[0]
    assert recharge["txn_count"] == 2
    assert recharge["minutes_total"] == 120
    assert recharge["coins_total"] == 240
    assert recharge["active_users"] == 2

    # write curated parquet locally to prove the output path works
    out = tempfile.mkdtemp()
    fact.to_parquet(os.path.join(out, "payments_curated.parquet"), index=False)
    kpi.to_parquet(os.path.join(out, "payments_daily_kpi.parquet"), index=False)

    print(json.dumps({
        "fact_rows": int(len(fact)),
        "kpi_rows": int(len(kpi)),
        "recharge_minutes_total": int(recharge["minutes_total"]),
        "curated_written_to": out,
    }, indent=2))
    print("ETL logic OK ✓")


if __name__ == "__main__":
    main()
