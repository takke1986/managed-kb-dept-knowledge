"""各 Lambda で共有する設定と S3 の置き場所。

ファイル置き場（FILES_BUCKET。利用者が Storage Browser で使う）:
    <部署>/<任意のフォルダ>/<ファイル名>        最上位のフォルダが部署。利用者は自分の部署のフォルダしか見えない

内部の置き場所（DOCS_BUCKET。利用者は触らない）:
    kb-source/<部署>/<文書ID>/<ファイル名>.md              書き起こしたテキスト（KB が取り込む）
    kb-source/<部署>/<文書ID>/<ファイル名>.md.metadata.json  部署・タグ・元ファイルの場所・ACL
    errors/<部署>/<文書ID>.txt                             変換に失敗した理由
    control/sync-pending                                   KB の同期待ちの印

文書ID は元ファイルのキーから作る。同じキーに置き直すと同じ文書として書き起こし直し、
移動・名前の変更（コピーと削除）は、古い文書を消して新しい文書を作ることになる。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

_CONFIG = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))

DEPARTMENTS: dict[str, str] = {d["id"]: d["name"] for d in _CONFIG["departments"]}
TAGS: list[str] = _CONFIG["tags"]
MODELS: dict[str, str] = _CONFIG["models"]

BUCKET = os.environ.get("DOCS_BUCKET", "")
FILES_BUCKET = os.environ.get("FILES_BUCKET", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
SYNC_FLAG_KEY = "control/sync-pending"
ADMIN_GROUP = "admins"          # タグの一覧を変えられる人（部署ではない）


@dataclass(frozen=True)
class DocRef:
    original_key: str  # ファイル置き場でのキー（<部署>/…/<ファイル名>）

    @property
    def department(self) -> str:
        return self.original_key.split("/", 1)[0]

    @property
    def file_name(self) -> str:
        return self.original_key.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        """部署のフォルダより下のフォルダ（無ければ空）。"""
        parts = self.original_key.split("/")
        return "/".join(parts[1:-1])

    @property
    def doc_id(self) -> str:
        return hashlib.sha256(self.original_key.encode("utf-8")).hexdigest()[:32]

    @property
    def text_key(self) -> str:
        return f"kb-source/{self.department}/{self.doc_id}/{self.file_name}.md"

    @property
    def metadata_key(self) -> str:
        return self.text_key + ".metadata.json"

    @classmethod
    def from_original_key(cls, key: str) -> "DocRef":
        dept, _, rest = key.partition("/")
        if dept not in DEPARTMENTS:
            raise ValueError(f"部署のフォルダの外: {key}")
        if not rest or key.endswith("/"):
            raise ValueError(f"ファイルではない（フォルダ）: {key}")
        return cls(key)


def department_members(cognito, department: str) -> list[str]:
    """部署（Cognito のグループ）に属する利用者のメールアドレス。"""
    emails = []
    for page in cognito.get_paginator("list_users_in_group").paginate(
        UserPoolId=USER_POOL_ID, GroupName=department
    ):
        for user in page["Users"]:
            if not user.get("Enabled", True):
                continue
            attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
            if attrs.get("email"):
                emails.append(attrs["email"].lower())
    return sorted(set(emails))


def acl_entries(emails: list[str]) -> list[dict]:
    """Managed KB の S3 ACL。S3 では USER（メールアドレス）単位でしか書けない。"""
    return [{"Name": e, "Type": "USER", "Access": "ALLOW"} for e in emails]


def request_sync(s3, lambda_client) -> None:
    """KB の同期を頼む。取り込み中なら同期 Lambda が次の回に拾う。"""
    s3.put_object(Bucket=BUCKET, Key=SYNC_FLAG_KEY, Body=b"1")
    fn = os.environ.get("SYNC_FUNCTION_NAME")
    if fn:
        lambda_client.invoke(FunctionName=fn, InvocationType="Event", Payload=b"{}")


METRIC_NAMESPACE = "ManagedKbPrototype"


def put_metrics(cloudwatch, values: dict[str, float]) -> None:
    """監視用の数字を CloudWatch に出す（アラームとダッシュボードが使う）。"""
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[
        {"MetricName": k, "Value": float(v), "Unit": "Count"} for k, v in values.items()])
