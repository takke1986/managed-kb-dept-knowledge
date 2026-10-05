"""文書の台帳（DynamoDB）。元ファイル・書き起こし・タグ・状態の紐付けの正本。

    文書の表（DOCUMENTS_TABLE）  department（PK）+ key（SK、ファイル置き場でのキー）
        docId, textKey, fileName, folder, sha（中身の SHA-256）, versionId（元ファイルの版）,
        sequencer（S3 のイベントの前後）, status, reason, folderTags, extraTags, tags,
        uploadedBy, scanStatus, size, uploadedAt, updatedAt
        GSI bySha（sha）: 同じ中身の文書を探す / GSI byStatus（status + updatedAt）: 止まったものを探す
    中身の表（CONTENTS_TABLE）  sha（PK）
        transcriptKey（書き起こしの控え）, extraTags（一括で足したタグ）, lastUsedAt

S3 には書き起こしの本文と KB 用のメタデータ（.metadata.json）を置く。メタデータはこの台帳から作る。

状態: scanning（マルウェア検査待ち）→ converting → converted・failed・skipped（対象外）・blocked（脅威あり）
"""

import os
from datetime import datetime, UTC

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from common import DEPARTMENTS, DocRef

_ddb = boto3.resource("dynamodb")
documents = _ddb.Table(os.environ.get("DOCUMENTS_TABLE", "none"))
contents = _ddb.Table(os.environ.get("CONTENTS_TABLE", "none"))

IN_PROGRESS = ("scanning", "converting")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def seq(value: str) -> str:
    """S3 のイベントの sequencer は、長さをそろえた16進の文字列として比べる（AWS の決まり）。"""
    return (value or "").upper().rjust(64, "0")


def get(doc: DocRef) -> dict | None:
    return documents.get_item(Key={"department": doc.department, "key": doc.original_key}).get("Item")


def base(doc: DocRef) -> dict:
    return {"docId": doc.doc_id, "textKey": doc.text_key, "fileName": doc.file_name, "folder": doc.folder,
            "folderTags": [p for p in doc.folder.split("/") if p]}


def conditional(fn, **kwargs) -> bool:
    """条件付きの書き込み。条件が合わなければ（古いイベントなど）False。"""
    try:
        fn(**kwargs)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def update(doc: DocRef, values: dict, condition=None, remove: tuple = ()) -> bool:
    values = {**values, "updatedAt": now()}
    names = {f"#{i}": k for i, k in enumerate(values)}
    exprs = [f"#{i} = :{i}" for i in range(len(values))]
    expr = "SET " + ", ".join(exprs)
    if remove:
        names.update({f"#r{i}": k for i, k in enumerate(remove)})
        expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove)))
    kwargs = {"Key": {"department": doc.department, "key": doc.original_key}, "UpdateExpression": expr,
              "ExpressionAttributeNames": names,
              "ExpressionAttributeValues": {f":{i}": v for i, v in enumerate(values.values())}}
    if condition is not None:
        kwargs["ConditionExpression"] = condition
    return conditional(documents.update_item, **kwargs)


def for_version(doc: DocRef, version_id: str, values: dict, remove: tuple = ()) -> bool:
    """その版の文書のときだけ書く（書き起こしの途中で新しい版が置かれたら、古い結果は捨てる）。"""
    return update(doc, values, Attr("versionId").eq(version_id or "null"), remove)


def query_department(department: str, prefix: str = "") -> list[dict]:
    cond = Key("department").eq(department)
    if prefix:
        cond = cond & Key("key").begins_with(prefix)
    items, kwargs = [], {"KeyConditionExpression": cond}
    while True:
        r = documents.query(**kwargs)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def all_documents() -> list[dict]:
    return [i for d in DEPARTMENTS for i in query_department(d)]


def documents_with_sha(sha: str) -> list[dict]:
    return documents.query(IndexName="bySha", KeyConditionExpression=Key("sha").eq(sha))["Items"]


def in_progress_before(cutoff: str) -> list[dict]:
    items = []
    for status in IN_PROGRESS:
        items += documents.query(IndexName="byStatus",
                                 KeyConditionExpression=Key("status").eq(status) & Key("updatedAt").lt(cutoff))["Items"]
    return items


def content(sha: str) -> dict:
    return contents.get_item(Key={"sha": sha}).get("Item") or {}


def put_content(sha: str, **values) -> None:
    values["lastUsedAt"] = now()
    contents.update_item(Key={"sha": sha},
                         UpdateExpression="SET " + ", ".join(f"#{i} = :{i}" for i in range(len(values))),
                         ExpressionAttributeNames={f"#{i}": k for i, k in enumerate(values)},
                         ExpressionAttributeValues={f":{i}": v for i, v in enumerate(values.values())})


def render_metadata(item: dict, acl: list[dict]) -> dict:
    """KB に渡すメタデータを台帳から作る。空の値は入れない（空のリストがあると KB が取り込まない）。"""
    attrs = {
        "department": item["department"], "tags": item.get("tags", []), "folder_tags": item.get("folderTags", []),
        "extra_tags": item.get("extraTags", []), "original_key": item["key"], "file_name": item["fileName"],
        "folder": item.get("folder", ""), "uploaded_by": item.get("uploadedBy", ""),
        "malware_scan": item.get("scanStatus", ""), "content_sha256": item.get("sha", ""),
    }
    return {"metadataAttributes": {k: v for k, v in attrs.items() if v not in ([], "", None)},
            "accessControlList": acl}
