"""まとめて置かれたときの確認。20件を一度に置き、全部が失敗なく取り込まれることと、
変換 Lambda が同時に4つまでしか動かないことを確かめる。終わったら置いたものを消す。

    .venv/bin/python scripts/verify_burst.py
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402
import boto3  # noqa: E402

N = 20
c = v.Checks()
fb = v.OUT["FilesBucket"]
tok = v.token(v.SALES)
sales = v.storage(tok, "sales")
keys = [f"sales/確認用/まとめて/{i:02d}-社内連絡.txt" for i in range(N)]
start = datetime.now(UTC)
with ThreadPoolExecutor(10) as pool:
    list(pool.map(lambda k: sales.put_object(Bucket=fb, Key=k, Body=f"まとめて置いた確認用の連絡 {k}".encode()), keys))
print(f"{N} 件置いた", flush=True)
try:
    for _ in range(40):
        _, files = v.api("GET", "/api/files?department=sales", tok)
        status = {f["key"]: f["status"] for f in files["files"] if f["key"] in keys}
        done = sum(1 for s in status.values() if s in ("converted", "failed"))
        print(f"  書き起こし済み {done}/{N}", flush=True)
        if done == N:
            break
        time.sleep(15)
    c.check(f"{N} 件すべて書き起こされた", sum(1 for s in status.values() if s == "converted") == N,
            {s: list(status.values()).count(s) for s in set(status.values())})
    cw = boto3.client("cloudwatch", region_name=v.REGION)
    fn = next(f["FunctionName"] for p in v.lam.get_paginator("list_functions").paginate()
              for f in p["Functions"] if f["FunctionName"].startswith("ManagedKbPrototype-Convert")
              and "Failed" not in f["FunctionName"])
    time.sleep(60)  # メトリクスが出るのを待つ
    points = cw.get_metric_statistics(Namespace="AWS/Lambda", MetricName="ConcurrentExecutions",
                                      Dimensions=[{"Name": "FunctionName", "Value": fn}],
                                      StartTime=start - timedelta(minutes=1), EndTime=datetime.now(UTC),
                                      Period=60, Statistics=["Maximum"])["Datapoints"]
    peak = max((p["Maximum"] for p in points), default=None)
    c.check("変換の同時実行は4つまで", peak is not None and peak <= 4, peak)
finally:
    for k in keys:
        sales.delete_object(Bucket=fb, Key=k)
    print("  置いたものを消した", flush=True)
sys.exit(c.summary())
