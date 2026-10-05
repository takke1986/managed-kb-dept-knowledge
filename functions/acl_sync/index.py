"""部署の人の入れ替えを、KB の ACL とファイル置き場の権限に反映する。

5分おきに動き、部署（Cognito のグループ）の人を前回の記録と比べる。変わった部署だけ次を行う。

- KB の ACL を書き直して同期する。Managed KB の S3 の ACL はメールアドレス単位でしか書けない
  （グループを書けない）ので、部署の文書のメタデータを今の人で書き直す
- 外れた人に発行済みの、ファイル置き場の認証情報（最長15分）を、その部署のフォルダだけ無効にする。
  ロールに「その人のセッションで、外れた時刻より前に発行されたものは拒否」を足す。
  ほかの部署のフォルダの分は止めない。認証情報が切れる時間を過ぎたら消す

Cognito のコンソールで変えても拾えるように、Cognito のイベントではなく定期的に比べる
（Cognito のグループの変更を EventBridge で受けるには CloudTrail の証跡が要る）。
scripts/users.py は変えた直後にこれを呼ぶので、すぐに反映される。

    event: {"departments": ["sales"], "force": true}   省略時はすべての部署、変わったものだけ
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, UTC

import boto3
from botocore.exceptions import ClientError

import ledger
from common import BUCKET, DEPARTMENTS, FILES_BUCKET, DocRef, acl_entries, department_members, request_sync

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
iam = boto3.client("iam")
cognito = boto3.client("cognito-idp")
lambda_client = boto3.client("lambda")

STORAGE_ROLE_NAME = os.environ.get("STORAGE_ROLE_NAME", "")
REVOKE_POLICY_NAME = "RevokedStorageSessions"
REVOCATIONS_KEY = "control/storage-revocations.json"
REVOKE_KEEP = timedelta(minutes=20)  # 認証情報の有効期限（15分）より長く残す


def snapshot_key(dept: str) -> str:
    return f"control/acl-members/{dept}.json"


def session_name(email: str) -> str:
    """API が認証情報を発行するときのセッション名と同じ規則。"""
    return re.sub(r"[^\w+=,.@-]", "-", email)[:64]


def handler(event, context):
    event = event or {}
    log.info("起動: %s", event.get("trigger", "定期"))
    departments = event.get("departments") or list(DEPARTMENTS)
    now = datetime.now(UTC)
    result, removed = {}, []
    for dept in departments:
        if dept not in DEPARTMENTS:
            raise ValueError(f"知らない部署: {dept}")
        members = department_members(cognito, dept)
        before = read_json(snapshot_key(dept))
        if before is not None and before == members and not event.get("force"):
            continue
        removed += [(email, dept) for email in (before or []) if email not in members]
        result[dept] = {"members": len(members), "documentsUpdated": rewrite_acl(dept, members),
                        "removed": [e for e, d in removed if d == dept]}
        write_json(snapshot_key(dept), members)
        log.info("%s: %s", dept, result[dept])
    if removed:
        revoke(removed, now)
    else:
        prune_revocations(now)  # 期限を過ぎた無効化を消す
    if any(r["documentsUpdated"] for r in result.values()):
        request_sync(s3, lambda_client)
    return result


def rewrite_acl(dept: str, members: list[str]) -> int:
    """部署の取り込み済みの文書のメタデータを、今の人の ACL で作り直す（台帳から）。"""
    acl = acl_entries(members)
    changed = 0
    for item in ledger.query_department(dept):
        if item.get("status") != "converted":
            continue
        key = DocRef(item["key"]).metadata_key
        s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json",
                      Body=json.dumps(ledger.render_metadata(item, acl), ensure_ascii=False).encode("utf-8"))
        changed += 1
    return changed


def revoke(removed: list[tuple[str, str]], now: datetime) -> None:
    entries = read_json(REVOCATIONS_KEY) or []
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # IAM の日付の条件に渡す形
    entries += [{"email": e, "department": d, "at": at} for e, d in removed]
    write_revocations(entries, now)


def prune_revocations(now: datetime) -> None:
    entries = read_json(REVOCATIONS_KEY)
    if entries:
        write_revocations(entries, now)


def write_revocations(entries: list[dict], now: datetime) -> None:
    entries = [e for e in entries if now - datetime.fromisoformat(e["at"]) < REVOKE_KEEP]
    write_json(REVOCATIONS_KEY, entries)
    if not STORAGE_ROLE_NAME:
        return
    if not entries:
        try:
            iam.delete_role_policy(RoleName=STORAGE_ROLE_NAME, PolicyName=REVOKE_POLICY_NAME)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
        return
    statements = []
    for i, e in enumerate(entries):
        folder = f"arn:aws:s3:::{FILES_BUCKET}/{e['department']}/"
        condition = {"StringLike": {"aws:userid": f"*:{session_name(e['email'])}"},
                     "DateLessThan": {"aws:TokenIssueTime": e["at"]}}
        statements.append({"Sid": f"Revoke{i}", "Effect": "Deny", "Action": "s3:*",
                           "Resource": folder + "*", "Condition": condition})
        statements.append({"Sid": f"RevokeList{i}", "Effect": "Deny", "Action": "s3:ListBucket",
                           "Resource": f"arn:aws:s3:::{FILES_BUCKET}",
                           "Condition": {**condition, "StringLike": {**condition["StringLike"],
                                                                     "s3:prefix": f"{e['department']}/*"}}})
    iam.put_role_policy(RoleName=STORAGE_ROLE_NAME, PolicyName=REVOKE_POLICY_NAME,
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))
    log.info("ファイル置き場の認証情報を無効にした: %s", [(e["email"], e["department"]) for e in entries])


def read_json(key: str):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def write_json(key: str, value) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json",
                  Body=json.dumps(value, ensure_ascii=False).encode("utf-8"))
