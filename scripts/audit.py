"""操作の記録を見る。アプリの操作（API）と、ファイル置き場への直接の操作（CloudTrail）を時系列に並べる。

    uv run --no-project --with 'boto3>=1.43.36' python scripts/audit.py [--user <email>] [--hours 24] [--key <文字列>]

    --user   その人の操作だけ
    --hours  さかのぼる時間（既定 24）
    --key    元ファイルのキーやパスに含まれる文字列で絞る

CloudTrail の記録は、操作から CloudWatch Logs に届くまで数分〜15分ほどかかる。
"""

import argparse
import json
import time

import boto3

from project import REGION, outputs

OUT = outputs()
logs = boto3.client("logs", region_name=REGION)


def insights(group: str, query: str, hours: float) -> list[dict]:
    end = int(time.time())
    qid = logs.start_query(logGroupName=group, startTime=end - int(hours * 3600), endTime=end,
                           queryString=query, limit=1000)["queryId"]
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            return [{f["field"]: f["value"] for f in row} for row in r["results"]]
        time.sleep(1)


def app_events(user: str, key: str, hours: float) -> list[tuple]:
    q = "fields @timestamp, @message | sort @timestamp asc"
    if user:
        q = f'filter user = "{user}" | ' + q
    rows = []
    for r in insights(OUT["AuditLogGroup"], q, hours):
        m = json.loads(r["@message"])
        text = json.dumps(m, ensure_ascii=False)
        if key and key not in text:
            continue
        detail = {k: v for k, v in m.items() if k not in ("action", "user")}
        rows.append((r["@timestamp"][:19], "アプリ", m["user"], m["action"], json.dumps(detail, ensure_ascii=False)[:160]))
    return rows


def trail_events(user: str, key: str, hours: float) -> list[tuple]:
    q = ('filter eventSource = "s3.amazonaws.com" and eventName in ["GetObject", "PutObject", "DeleteObject", '
         '"CopyObject", "CompleteMultipartUpload"] | fields @timestamp, userIdentity.arn, eventName, '
         'requestParameters.key, errorCode | sort @timestamp asc')
    rows = []
    for r in insights(OUT["TrailLogGroup"], q, hours):
        who = r.get("userIdentity.arn", "").rsplit("/", 1)[-1]
        if "@" not in who:  # 利用者の操作だけ（システムの読み書きは除く）
            continue
        if (user and who != user) or (key and key not in r.get("requestParameters.key", "")):
            continue
        rows.append((r["@timestamp"][:19], "S3", who, r["eventName"] + (f" ({r['errorCode']})" if r.get("errorCode") else ""),
                     r.get("requestParameters.key", "")))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--key", default="")
    a = ap.parse_args()
    rows = sorted(app_events(a.user, a.key, a.hours) + trail_events(a.user, a.key, a.hours))
    for r in rows:
        print(" | ".join(r))
    print(f"{len(rows)} 件")
