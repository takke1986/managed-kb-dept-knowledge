"""部署ごとのタグの一覧（DynamoDB）。一括で足す「追加のタグ」の選択肢と、その説明・色・並び順。

    タグの表（TAGS_TABLE）  department（PK）+ name（SK）
        description（何に付けるか。画面とエージェントに見せる）, color, order（並び順）,
        archived（アーカイブ: これから付ける候補には出さないが、付いている文書からは外さない）,
        version（同時に直したときに後の保存を断る）, createdBy, createdAt, updatedBy, updatedAt
    部署ごとに1件、name が "#seeded" の行を置く（最初の一覧を入れたしるし。全部消しても入れ直さない）

最初の一覧は app.json の tags に、すでに文書に付いている追加のタグを足したもの
（一覧から消えて管理できなくならないように）。
フォルダ名のタグはここには入れない（置いた場所で決まる）。
"""

import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import ledger
from common import TAGS

table = boto3.resource("dynamodb").Table(os.environ.get("TAGS_TABLE", "none"))
SEEDED = "#seeded"
COLORS = ("gray", "blue", "green", "orange", "red", "purple")
MAX_ACTIVE = 30
MAX_NAME = 20
MAX_DESCRIPTION = 200


class Conflict(Exception):
    """ほかの人が先に直した・同じ名前がある。"""


def _seed(department: str) -> None:
    now = ledger.now()
    try:
        table.put_item(Item={"department": department, "name": SEEDED, "createdAt": now},
                       ConditionExpression="attribute_not_exists(#n)", ExpressionAttributeNames={"#n": "name"})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # ほかの呼び出しが先に入れた
        raise
    used = [t for item in ledger.query_department(department) for t in item.get("extraTags", [])]
    for i, name in enumerate(dict.fromkeys([*TAGS, *used])):
        try:
            table.put_item(Item={"department": department, "name": name, "description": "", "color": "gray",
                                 "order": i, "archived": False, "version": 1,
                                 "createdBy": "（最初の一覧）", "createdAt": now, "updatedBy": "", "updatedAt": now},
                           ConditionExpression="attribute_not_exists(#n)", ExpressionAttributeNames={"#n": "name"})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


def list_tags(department: str) -> list[dict]:
    """部署のタグ（アーカイブも含む）。並び順・名前の順。まだ一覧が無ければ最初の一覧を入れる。"""
    items = _query(department)
    if not any(i["name"] == SEEDED for i in items):
        _seed(department)
        items = _query(department)
    tags = [_plain(i) for i in items if i["name"] != SEEDED]
    return sorted(tags, key=lambda t: (t["order"], t["name"]))


def active_names(department: str) -> list[str]:
    """これから付けられるタグ（アーカイブを除く）。"""
    return [t["name"] for t in list_tags(department) if not t["archived"]]


def _query(department: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("department").eq(department)}
    while True:
        r = table.query(**kwargs)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _plain(item: dict) -> dict:
    return {"name": item["name"], "description": item.get("description", ""), "color": item.get("color", "gray"),
            "order": int(item.get("order", 0)), "archived": bool(item.get("archived", False)),
            "version": int(item.get("version", 1)), "createdBy": item.get("createdBy", ""),
            "createdAt": item.get("createdAt", ""), "updatedBy": item.get("updatedBy", ""),
            "updatedAt": item.get("updatedAt", "")}


def validate(name: str, description: str, color: str) -> str:
    """直せない入力なら理由を返す。"""
    if not name or len(name) > MAX_NAME or name.startswith("#") or "/" in name:
        return f"タグの名前は1〜{MAX_NAME}文字で、/ を含まず # で始まらないものにしてください"
    if len(description) > MAX_DESCRIPTION:
        return f"説明は{MAX_DESCRIPTION}文字までです"
    if color not in COLORS:
        return "色の指定が正しくありません"
    return ""


def create(department: str, name: str, description: str, color: str, user: str) -> dict:
    now = ledger.now()
    current = list_tags(department)
    if sum(not t["archived"] for t in current) >= MAX_ACTIVE:
        raise ValueError(f"使えるタグは部署ごとに{MAX_ACTIVE}個までです（使わないものはアーカイブする）")
    item = {"department": department, "name": name, "description": description, "color": color,
            "order": max([t["order"] for t in current], default=-1) + 1, "archived": False, "version": 1,
            "createdBy": user, "createdAt": now, "updatedBy": user, "updatedAt": now}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(#n)", ExpressionAttributeNames={"#n": "name"})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("同じ名前のタグがあります（アーカイブも含む）") from None
        raise
    return _plain(item)


def update(department: str, name: str, version: int, user: str, **values) -> dict:
    """説明・色・アーカイブを変える。version が保存されているものと違えば断る（ほかの人が先に直した）。"""
    if values.get("archived") is False:  # アーカイブから戻すときも、使えるタグの上限を守る
        active = [t for t in list_tags(department) if not t["archived"] and t["name"] != name]
        if len(active) >= MAX_ACTIVE:
            raise ValueError(f"使えるタグは部署ごとに{MAX_ACTIVE}個までです")
    names = {f"#{k}": k for k in values}
    sets = [f"#{k} = :{k}" for k in values] + ["version = :next", "updatedBy = :by", "updatedAt = :at"]
    extra = {"ExpressionAttributeNames": names} if names else {}
    try:
        r = table.update_item(
            Key={"department": department, "name": name},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(department) AND version = :v",
            ExpressionAttributeValues={**{f":{k}": v for k, v in values.items()},
                                       ":v": version, ":next": version + 1, ":by": user, ":at": ledger.now()},
            ReturnValues="ALL_NEW", **extra)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("ほかの人が先に直したか、消されています。読み込み直してください") from None
        raise
    return _plain(r["Attributes"])


def reorder(department: str, names: list[str]) -> None:
    """並び順を names の順にする（載っていないタグの順は変えない）。"""
    for i, name in enumerate(names):
        try:
            table.update_item(Key={"department": department, "name": name}, UpdateExpression="SET #o = :o",
                              ConditionExpression="attribute_exists(department)",
                              ExpressionAttributeNames={"#o": "order"}, ExpressionAttributeValues={":o": i})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


def delete(department: str, name: str, version: int) -> None:
    try:
        table.delete_item(Key={"department": department, "name": name},
                          ConditionExpression="version = :v", ExpressionAttributeValues={":v": version})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("ほかの人が先に直したか、消されています。読み込み直してください") from None
        raise
