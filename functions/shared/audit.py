"""アプリの操作の記録（誰が・いつ・何を）。専用のロググループ（AUDIT_LOG_GROUP）に JSON で書く。

ファイル置き場への直接の操作（Storage Browser のアップロード・ダウンロード・削除）は、
CloudTrail の S3 のデータの記録に残る（認証情報のセッション名がメールアドレス）。
ここには API を通る操作を書く: 元ファイルを開く・質問と答えに使った元ファイル・タグの変更・
認証情報の発行・権限の無い操作の試み など。

記録に失敗しても、利用者の操作は止めない（ログに残して続ける）。
"""

import json
import logging
import os
import time
import uuid

import boto3

log = logging.getLogger()
_logs = boto3.client("logs")
_group = os.environ.get("AUDIT_LOG_GROUP", "")
_stream = None


def record(action: str, user_email: str, **detail) -> None:
    global _stream
    if not _group:
        return
    entry = {"action": action, "user": user_email, **detail}
    try:
        if _stream is None:
            _stream = f"{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}"
            _logs.create_log_stream(logGroupName=_group, logStreamName=_stream)
        _logs.put_log_events(logGroupName=_group, logStreamName=_stream, logEvents=[
            {"timestamp": int(time.time() * 1000), "message": json.dumps(entry, ensure_ascii=False, default=str)}])
    except Exception:  # 記録に失敗しても操作は止めない
        log.exception("操作の記録に失敗: %s", entry)
