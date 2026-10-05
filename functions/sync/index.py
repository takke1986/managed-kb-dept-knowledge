"""同期待ちの印があれば KB の取り込みを始める。

変換・削除・ACL の書き直しのたびに印を立てて呼ばれるほか、取り込み中で始められなかった分を
拾うため数分おきにも動く。取り込みは変わったファイルだけを読む（増分同期）。

あわせて、前回見てから終わった取り込みの結果（失敗した文書の数）を監視用の数字として出す。
取り込みの失敗はほかに知る手段が無い（KB は失敗した文書を黙って飛ばす）。
"""

import logging
import os

import boto3
from botocore.exceptions import ClientError

from common import BUCKET, SYNC_FLAG_KEY, put_metrics

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
agent = boto3.client("bedrock-agent")
cloudwatch = boto3.client("cloudwatch")
CHECKED_KEY = "control/ingestion-checked-until"


def handler(event, context):
    try:
        report_finished_jobs()
    except Exception:  # 監視のための数字が出せなくても、同期は止めない
        log.exception("取り込みの結果を数えられなかった")
    try:
        s3.head_object(Bucket=BUCKET, Key=SYNC_FLAG_KEY)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return {"started": False, "reason": "同期待ちなし"}
        raise
    # 印は取り込みを始める前に消す。消した後に立った印は次の回が拾う
    s3.delete_object(Bucket=BUCKET, Key=SYNC_FLAG_KEY)
    try:
        job = agent.start_ingestion_job(
            knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
            dataSourceId=os.environ["DATA_SOURCE_ID"],
        )["ingestionJob"]
    except ClientError as e:
        s3.put_object(Bucket=BUCKET, Key=SYNC_FLAG_KEY, Body=b"1")
        if e.response["Error"]["Code"] == "ConflictException":
            log.info("取り込み中のため次の回に回す")
            return {"started": False, "reason": "取り込み中"}
        raise
    log.info("取り込み開始: %s", job["ingestionJobId"])
    return {"started": True, "ingestionJobId": job["ingestionJobId"]}


def report_finished_jobs() -> None:
    """前回見てから終わった取り込みの、失敗した文書の数を出す。"""
    try:
        since = s3.get_object(Bucket=BUCKET, Key=CHECKED_KEY)["Body"].read().decode()
    except ClientError:
        since = "1970-01-01T00:00:00+00:00"
    jobs = agent.list_ingestion_jobs(knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
                                     dataSourceId=os.environ["DATA_SOURCE_ID"],
                                     sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                                     maxResults=20)["ingestionJobSummaries"]
    done = [j for j in jobs if j["status"] in ("COMPLETE", "FAILED", "STOPPED") and j["updatedAt"].isoformat() > since]
    if not done:
        return
    failed_docs = sum(j.get("statistics", {}).get("numberOfDocumentsFailed", 0) for j in done)
    failed_jobs = sum(1 for j in done if j["status"] == "FAILED")
    put_metrics(cloudwatch, {"IngestionFailedDocuments": failed_docs, "IngestionFailedJobs": failed_jobs})
    if failed_docs or failed_jobs:
        log.error("取り込みの失敗: 文書 %d 件、取り込み %d 回", failed_docs, failed_jobs)
    s3.put_object(Bucket=BUCKET, Key=CHECKED_KEY, Body=max(j["updatedAt"] for j in done).isoformat().encode())
