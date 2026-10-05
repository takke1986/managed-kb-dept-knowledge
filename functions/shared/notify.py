"""置いた人へのお知らせ。書き起こしの失敗・対象外・ブロック（マルウェアの疑い）を知らせる。

画面の中のお知らせ（NOTIFICATIONS_TABLE、90日で消える）に入れる。設定で送り元（NOTIFY_FROM）が
あれば、SES でメールも送る（SES のサンドボックスでは検証済みの宛先にしか届かない）。
"""

import logging
import os
import time
import uuid
from datetime import datetime, UTC

import boto3

log = logging.getLogger()
table = boto3.resource("dynamodb").Table(os.environ.get("NOTIFICATIONS_TABLE", "none"))
NOTIFY_FROM = os.environ.get("NOTIFY_FROM", "")
LABEL = {"failed": "書き起こしに失敗しました", "skipped": "ナレッジに入れられないファイルです",
         "blocked": "マルウェアの疑いがあるため取り込みませんでした"}


def file_problem(user_email: str, key: str, file_name: str, status: str, reason: str) -> None:
    """置いたファイルに問題があったことを、置いた人に知らせる。置いた人が分からなければ何もしない。"""
    if not user_email or status not in LABEL:
        return
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    table.put_item(Item={"user": user_email, "sk": f"{now}#{uuid.uuid4().hex[:8]}", "key": key,
                         "fileName": file_name, "status": status, "title": LABEL[status], "reason": reason,
                         "read": False, "createdAt": now, "expiresAt": int(time.time()) + 90 * 86400})
    if NOTIFY_FROM:
        try:
            boto3.client("ses").send_email(
                Source=NOTIFY_FROM, Destination={"ToAddresses": [user_email]},
                Message={"Subject": {"Data": f"【部署別ナレッジ】{file_name}: {LABEL[status]}", "Charset": "UTF-8"},
                         "Body": {"Text": {"Charset": "UTF-8",
                                           "Data": f"{file_name}（{key}）\n{LABEL[status]}\n理由: {reason}"}}})
        except Exception:  # メールが送れなくても、画面の中のお知らせは残る
            log.exception("お知らせのメールを送れなかった: %s", user_email)
