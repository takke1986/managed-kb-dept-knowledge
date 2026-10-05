"""画面から呼ぶ API（Lambda の関数 URL）。

画面と同じ CloudFront の /api/* から呼ぶ（関数 URL は IAM 認証で、CloudFront からしか呼べない）。
利用者のトークンは x-app-token ヘッダーで受け取る。

    GET    /api/me                      利用者のメールアドレスと部署
    GET    /api/storage/locations       Storage Browser に見せるフォルダ（属する部署のフォルダだけ）
    POST   /api/storage/credentials     そのフォルダだけに絞った一時的な認証情報
    GET    /api/files?department=<id>   部署のファイル一覧と、書き起こしの状況
    GET    /api/download-url?key=<key>  元ファイルを開くための署名付き URL（検索結果から開く）
    PUT    /api/files/tags              選んだ文書にタグを一括で足す・外す（部署の人。部署のタグの一覧の中から）
    GET    /api/tags?department=<id>    部署のタグの一覧（説明・色・アーカイブ・付いている件数）
    POST   /api/tags                    タグを足す（管理者だけ。以下同じ）
    PUT    /api/tags                    タグの説明・色を変える、アーカイブする・戻す
    PUT    /api/tags/order              タグの並び順を変える
    DELETE /api/tags?department=&name=&version=[&detach=1]
                                        タグを消す（付いている文書があれば detach=1 のときだけ、外してから消す）
    POST   /api/chat                    エージェントに質問する（処理の ID を返す。答えは裏で作る）
    GET    /api/chat?id=<id>            答えの途中経過・結果（画面が1秒弱ごとに問い合わせる）
    GET    /api/files/text?key=<key>    書き起こし（ページごと）。人が直したかどうか
    PUT    /api/files/text              書き起こしのページを直す（部署の人）
    GET    /api/notifications           自分へのお知らせ（置いたファイルの失敗・対象外・ブロック）
    POST   /api/notifications/read      お知らせを既読にする

どの操作もアクセストークンを検証し、利用者が属する部署の範囲しか触らせない。
ファイルの一覧・アップロード・削除・移動は、画面の Storage Browser が S3 に直接行う。
その認証情報はここで発行し、部署のフォルダの外には触れないセッションポリシーを付ける。
チャットの検索は AgentCore Gateway を通す。部署の分離は Gateway（Interceptor・Cedar）と
KB（ACL）が担い、ここでは検索結果の元ファイルを開くときに部署をもう一度確かめる。
"""

import base64
import json
import logging
import os
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, UTC

import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from auth import Unauthorized, User, app_token, verify
import audit
import ledger
import tags as tag_table
from common import (
    ADMIN_GROUP, BUCKET, DEPARTMENTS, FILES_BUCKET, DocRef, acl_entries, department_members, request_sync,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
sts = boto3.client("sts")
lambda_client = boto3.client("lambda")
jobs = boto3.resource("dynamodb").Table(os.environ.get("CHAT_JOBS_TABLE", "none"))
notifications = boto3.resource("dynamodb").Table(os.environ.get("NOTIFICATIONS_TABLE", "none"))
cognito = boto3.client("cognito-idp")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
STORAGE_ROLE_ARN = os.environ.get("STORAGE_ROLE_ARN", "")
URL_TTL = 300
CREDENTIALS_TTL = 900  # STS の最短。部署から外れたあとに使える時間を短くする
STORAGE_PERMISSIONS = ["get", "list", "write", "delete"]
# 検査待ち・書き起こし中のまま、これより長く台帳が変わらなければ止まっているとみなす
STALLED_AFTER = timedelta(minutes=60)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def handler(event, context):
    if "chatJob" in event:  # 自分自身を非同期で呼んだもの（答えを裏で作る）
        return run_chat_job(event["chatJob"])
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    try:
        user = with_current_departments(verify(app_token(event.get("headers", {}))))
        route = ROUTES.get((method, path))
        if not route:
            raise HttpError(404, "見つからない")
        return respond(200, route(user, event))
    except Unauthorized as e:
        return respond(401, {"message": str(e)})
    except HttpError as e:
        if e.status == 403:  # 権限の無い操作の試みは記録する
            audit.record("denied", user.email, method=method, path=path, query=event.get("queryStringParameters"),
                         reason=str(e))
        return respond(e.status, {"message": str(e)})
    except Exception:
        log.exception("失敗: %s %s", method, path)
        return respond(500, {"message": "サーバーで失敗しました"})


def with_current_departments(user: User) -> User:
    """部署は Cognito に今のグループを問い合わせて決める。

    アクセストークンの cognito:groups は発行した時点のもので、最長1時間古いまま残る。
    部署から外した人に、その間もファイル置き場の認証情報を出さないようにする。"""
    groups = []
    for page in cognito.get_paginator("admin_list_groups_for_user").paginate(
            UserPoolId=os.environ["USER_POOL_ID"], Username=user.username):
        groups += [g["GroupName"] for g in page["Groups"]]
    return replace(user, departments=tuple(g for g in groups if g in DEPARTMENTS), admin=ADMIN_GROUP in groups)


def respond(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False)}


def json_body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def query(event, name: str) -> str:
    return (event.get("queryStringParameters") or {}).get(name, "")


def require_department(user: User, department: str) -> str:
    if department not in user.departments:
        raise HttpError(403, "この部署のファイルは扱えません")
    return department


def doc_of(user: User, key: str) -> DocRef:
    try:
        doc = DocRef.from_original_key(key)
    except ValueError:
        raise HttpError(400, "ファイルの指定が正しくありません") from None
    require_department(user, doc.department)
    return doc


# ---- ルート ----

def me(user: User, event) -> dict:
    defs = {d: tag_table.list_tags(d) for d in (DEPARTMENTS if user.admin else user.departments)}
    active = {d: [t["name"] for t in ts if not t["archived"]] for d, ts in defs.items()}
    look = lambda d: [{k: t[k] for k in ("name", "description", "color", "archived")} for t in defs[d]]  # noqa: E731
    return {"email": user.email, "admin": user.admin,
            "departments": [{"id": d, "name": DEPARTMENTS[d], "tags": active[d], "tagDefs": look(d)}
                            for d in user.departments],
            "allDepartments": [{"id": d, "name": n, "tags": active[d], "tagDefs": look(d)}
                               for d, n in DEPARTMENTS.items()] if user.admin else []}


def require_admin(user: User) -> None:
    if not user.admin:
        raise HttpError(403, "タグの一覧を変えられるのは管理者だけです")


def admin_department(body_or_query: dict) -> str:
    dept = str(body_or_query.get("department", ""))
    if dept not in DEPARTMENTS:
        raise HttpError(400, "部署の指定が正しくありません")
    return dept


def tag_usage(dept: str) -> dict[str, int]:
    """追加のタグが付いている文書の数（台帳から。取り込み済みでないものも数える）。"""
    counts: dict[str, int] = {}
    for item in ledger.query_department(dept):
        for t in item.get("extraTags", []):
            counts[t] = counts.get(t, 0) + 1
    return counts


def get_department_tags(user: User, event) -> dict:
    """部署のタグの一覧と、付いている件数。部署の人と管理者が見られる。

    一覧に無いのに文書に付いているタグ（消したあとに外し損ねたもの）も unlisted として返す。"""
    dept = admin_department({"department": query(event, "department")})
    if not user.admin:
        require_department(user, dept)
    usage = tag_usage(dept)
    items = [{**t, "count": usage.get(t["name"], 0)} for t in tag_table.list_tags(dept)]
    listed = {t["name"] for t in items}
    return {"department": dept, "tags": items, "maxActive": tag_table.MAX_ACTIVE, "colors": list(tag_table.COLORS),
            "unlisted": [{"name": n, "count": c} for n, c in sorted(usage.items()) if n not in listed]}


def create_tag(user: User, event) -> dict:
    require_admin(user)
    body = json_body(event)
    dept = admin_department(body)
    name, description = str(body.get("name", "")).strip(), str(body.get("description", "")).strip()
    color = str(body.get("color", "gray"))
    if problem := tag_table.validate(name, description, color):
        raise HttpError(400, problem)
    try:
        tag = tag_table.create(dept, name, description, color, user.email)
    except tag_table.Conflict as e:
        raise HttpError(409, str(e)) from None
    except ValueError as e:
        raise HttpError(400, str(e)) from None
    audit.record("tag_create", user.email, department=dept, tag=name, description=description, color=color)
    return tag


def update_tag(user: User, event) -> dict:
    """説明・色・アーカイブを変える。名前は変えない（付いている文書の付け替えになるため）。"""
    require_admin(user)
    body = json_body(event)
    dept = admin_department(body)
    name = str(body.get("name", ""))
    values = {}
    if "description" in body:
        values["description"] = str(body["description"]).strip()
    if "color" in body:
        values["color"] = str(body["color"])
    if "archived" in body:
        values["archived"] = bool(body["archived"])
    if not values:
        raise HttpError(400, "変える項目がありません")
    if problem := tag_table.validate(name, values.get("description", ""), values.get("color", "gray")):
        raise HttpError(400, problem)
    try:
        tag = tag_table.update(dept, name, int(body.get("version", 0)), user.email, **values)
    except tag_table.Conflict as e:
        raise HttpError(409, str(e)) from None
    except ValueError as e:
        raise HttpError(400, str(e)) from None
    audit.record("tag_update", user.email, department=dept, tag=name, **values)
    return tag


def order_tags(user: User, event) -> dict:
    require_admin(user)
    body = json_body(event)
    dept = admin_department(body)
    names = [str(n) for n in body.get("names", [])][:200]
    tag_table.reorder(dept, names)
    audit.record("tag_order", user.email, department=dept, names=names)
    return {"department": dept, "names": names}


def delete_tag(user: User, event) -> dict:
    """タグを消す。付いている文書があれば、detach=1 のときだけ外してから消す（無ければ 409 で件数を返す）。

    付いたまま残したいときは、消さずにアーカイブする。"""
    require_admin(user)
    dept = admin_department({"department": query(event, "department")})
    name = query(event, "name")
    version = int(query(event, "version") or 0)
    in_use = [item for item in ledger.query_department(dept) if name in item.get("extraTags", [])]
    if in_use and query(event, "detach") != "1":
        raise HttpError(409, f"{len(in_use)}件の文書に付いています。外してから消すか、アーカイブしてください")
    try:
        tag_table.delete(dept, name, version)
    except tag_table.Conflict as e:
        raise HttpError(409, str(e)) from None
    updated, skipped = retag(in_use, [], {name})
    audit.record("tag_delete", user.email, department=dept, tag=name, detached=updated, skipped=skipped)
    return {"department": dept, "name": name, "detached": len(updated), "skipped": skipped}


def set_file_tags(user: User, event) -> dict:
    """選んだ文書に、部署のタグの一覧から一括でタグを足す・外す。

    足したタグは中身（SHA-256）に紐づけて持つので、フォルダを移しても（コピーと削除でも）引き継がれる。
    フォルダ名のタグは置いた場所で決まるので、ここでは外せない（フォルダを移す）。"""
    body = json_body(event)
    keys = [str(k) for k in body.get("keys", [])][:500]
    add = list(dict.fromkeys(str(t) for t in body.get("add", [])))
    remove = set(str(t) for t in body.get("remove", []))
    if not keys or not (add or remove):
        raise HttpError(400, "ファイルと、足す・外すタグを選んでください")
    docs = [doc_of(user, k) for k in keys]
    for dept in {d.department for d in docs}:
        if any(t not in tag_table.active_names(dept) for t in add):
            raise HttpError(400, "部署のタグの一覧に無いタグ・アーカイブしたタグは付けられません")
    items, skipped = [], []
    for doc in docs:
        item = ledger.get(doc)
        if not item or item.get("status") != "converted":
            skipped.append(doc.original_key)  # まだナレッジに入っていない
        else:
            items.append(item)
    updated, late = retag(items, add, remove)
    skipped += late
    audit.record("file_tags", user.email, keys=updated, add=add, remove=sorted(remove), skipped=skipped)
    return {"updated": updated, "skipped": skipped}


def retag(items: list[dict], add: list[str], remove: set[str]) -> tuple[list[str], list[str]]:
    """台帳の文書に追加のタグを足す・外し、KB のメタデータと書き起こしの先頭のタグの行を合わせる。

    取り込み済みでない文書は、中身（Contents）のタグだけ変える（取り込むときにそこから付く）。"""
    updated, skipped = [], []
    for item in items:
        doc = DocRef.from_original_key(item["key"])
        extras = [t for t in item.get("extraTags", []) if t not in remove]
        extras += [t for t in add if t not in extras]
        tags = list(dict.fromkeys(item.get("folderTags", []) + extras))
        if item.get("status") != "converted":
            if item.get("sha"):
                ledger.put_content(item["sha"], extraTags=extras)
            ledger.update(doc, {"extraTags": extras, "tags": tags}, condition="attribute_exists(department)")
            updated.append(doc.original_key)
            continue
        if not ledger.for_version(doc, item["versionId"], {"extraTags": extras, "tags": tags}):
            skipped.append(doc.original_key)  # 途中で新しい版が置かれた
            continue
        ledger.put_content(item["sha"], extraTags=extras)  # 中身に紐づけて持つ（移動しても引き継ぐ）
        item.update({"extraTags": extras, "tags": tags})
        meta = ledger.render_metadata(item, acl_entries(department_members(cognito, doc.department)))
        s3.put_object(Bucket=BUCKET, Key=doc.metadata_key, ContentType="application/json",
                      Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        text = read_text(doc.text_key)
        if text:  # 書き起こしの先頭のタグの行も合わせる（検索で見えるので）
            text = re.sub(r"タグ: [^\n]*", f"タグ: {'・'.join(tags) or 'なし'}", text, count=1)
            s3.put_object(Bucket=BUCKET, Key=doc.text_key, Body=text.encode("utf-8"),
                          ContentType="text/markdown; charset=utf-8")
        updated.append(doc.original_key)
    if updated:
        request_sync(s3, lambda_client)
    _vocab_cache.clear()
    return updated, skipped


def list_files(user: User, event) -> dict:
    """部署の文書と、ナレッジへの取り込みの状況（台帳から）。"""
    dept = require_department(user, query(event, "department"))
    stalled_before = (datetime.now(UTC) - STALLED_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = []
    for item in ledger.query_department(dept):
        status = item.get("status", "scanning")
        if status in ledger.IN_PROGRESS and item.get("updatedAt", "") < stalled_before:
            status = "stalled"  # 毎日の突き合わせが入れ直す
        files.append({"key": item["key"], "fileName": item["fileName"], "folder": item.get("folder", ""),
                      "size": int(item.get("size", 0)), "uploadedAt": item.get("uploadedAt", ""),
                      "status": "converting" if status == "scanning" else status, "reason": item.get("reason", ""),
                      "tags": item.get("tags", []), "folderTags": item.get("folderTags", []),
                      "extraTags": item.get("extraTags", [])})
    files.sort(key=lambda f: f["uploadedAt"], reverse=True)
    return {"department": dept, "files": files}


def read_text(key: str) -> str:
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except ClientError:
        return ""


def storage_locations(user: User, event) -> dict:
    """Storage Browser に見せるフォルダ。属する部署の最上位のフォルダだけ。"""
    return {"items": [{"id": d, "bucket": FILES_BUCKET, "prefix": f"{d}/", "type": "PREFIX",
                       "permissions": STORAGE_PERMISSIONS, "name": DEPARTMENTS[d]} for d in user.departments]}


def storage_credentials(user: User, event) -> dict:
    """フォルダ（scope）だけに絞った一時的な認証情報。

    scope は Storage Browser が渡す s3://<バケット>/<部署>/… の形。部署に属していなければ断る。
    ロールの権限はバケット全体だが、セッションポリシーでその部署のフォルダに絞る。
    セッション名にメールアドレスを入れ、S3 のイベントから置いた人が分かるようにする。"""
    body = json_body(event)
    scope = str(body.get("scope", ""))
    head = f"s3://{FILES_BUCKET}/"
    if not scope.startswith(head):
        raise HttpError(400, "このバケットの場所ではありません")
    dept = scope[len(head):].split("/", 1)[0].rstrip("*")
    require_department(user, dept)
    folder = f"arn:aws:s3:::{FILES_BUCKET}/{dept}/*"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectTagging", "s3:PutObjectTagging",
                                       "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
         "Resource": folder},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{FILES_BUCKET}",
         "Condition": {"StringLike": {"s3:prefix": [f"{dept}/*", f"{dept}/"]}}},
    ]}
    session = re.sub(r"[^\w+=,.@-]", "-", user.email)[:64]
    audit.record("storage_credentials", user.email, department=dept, scope=scope)
    creds = sts.assume_role(RoleArn=STORAGE_ROLE_ARN, RoleSessionName=session,
                            Policy=json.dumps(policy), DurationSeconds=CREDENTIALS_TTL)["Credentials"]
    return {"scope": scope, "credentials": {
        "accessKeyId": creds["AccessKeyId"], "secretAccessKey": creds["SecretAccessKey"],
        "sessionToken": creds["SessionToken"], "expiration": creds["Expiration"].isoformat()}}


def download_url(user: User, event) -> dict:
    doc = doc_of(user, query(event, "key"))
    audit.record("open_original", user.email, key=doc.original_key)
    return {"url": presign_get(doc)}


# 置いた道具によっては種類（Content-Type）が付かず、ブラウザがその場で表示せずにダウンロードしてしまう。
# 開くときは拡張子から種類を決めて返す（書き起こしの画面で元ファイルを並べて表示するため）
INLINE_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp", "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
                "csv": "text/csv; charset=utf-8"}


def presign_get(doc: DocRef) -> str:
    params = {"Bucket": FILES_BUCKET, "Key": doc.original_key,
              "ResponseContentDisposition": "inline; filename*=UTF-8''" + quote(doc.file_name)}
    ext = doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else ""
    if ext in INLINE_TYPES:
        params["ResponseContentType"] = INLINE_TYPES[ext]
    return s3.generate_presigned_url("get_object", ExpiresIn=URL_TTL, Params=params)


def quote(s: str) -> str:
    from urllib.parse import quote as q
    return q(s, safe="")


def chat(user: User, event) -> dict:
    """質問を受け付ける。答えは自分自身を非同期で呼んで作り、途中経過を DynamoDB に書き足していく。
    1回の呼び出しを短くすることで、CloudFront の待ち時間の上限（60秒）にもかからない。"""
    body = json_body(event)
    message = str(body.get("message", "")).strip()
    if not message:
        raise HttpError(400, "質問が空です")
    history = [m for m in body.get("history", [])[-10:]
               if m.get("role") in ("user", "assistant") and isinstance(m.get("text"), str)]
    job_id = uuid.uuid4().hex
    jobs.put_item(Item={"id": job_id, "owner": user.sub, "status": "running", "text": "",
                        "expiresAt": int(time.time()) + 86400})
    scope = [d for d in body.get("departments", []) if d in user.departments]
    job = {"id": job_id, "token": app_token(event.get("headers", {})), "message": message, "history": history,
           "scope": scope if 0 < len(scope) < len(user.departments) else []}
    lambda_client.invoke(FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"], InvocationType="Event",
                         Payload=json.dumps({"chatJob": job}, ensure_ascii=False).encode("utf-8"))
    return {"jobId": job_id}


def chat_status(user: User, event) -> dict:
    item = jobs.get_item(Key={"id": query(event, "id")}).get("Item")
    if not item or item["owner"] != user.sub:
        raise HttpError(404, "見つかりません")
    return {"status": item["status"], "text": item.get("text", ""), "answer": item.get("answer", ""),
            "sources": json.loads(item.get("sources", "[]")), "searches": json.loads(item.get("searches", "[]"))}


def run_chat_job(job: dict) -> None:
    """答えを作る（非同期で呼ばれる）。書けた分は0.5秒ごとに DynamoDB に書き足す。
    トークンはここでもう一度検証し、部署も Cognito に問い合わせ直す。"""
    from agent import ask

    job_id = job["id"]
    buffer, last = [], [0.0]

    def flush(force=False):
        if force or time.time() - last[0] > 0.5:
            jobs.update_item(Key={"id": job_id}, UpdateExpression="SET #t = :t",
                             ExpressionAttributeNames={"#t": "text"}, ExpressionAttributeValues={":t": "".join(buffer)})
            last[0] = time.time()

    def on_text(delta: str):
        buffer.append(delta)
        flush()

    try:
        user = with_current_departments(verify(job["token"]))
        scope = [d for d in job.get("scope", []) if d in user.departments]
        if scope:
            user = replace(user, departments=tuple(scope))  # 元ファイルも選んだ部署の分だけ出す
        tags = search_vocabulary(user.departments)
        answer, results, searches = ask(job["token"], job["message"], job["history"], tags, on_text, scope)
        sources = build_sources(user, results)
        audit.record("chat", user.email, question=job["message"], scope=scope, searches=searches,
                     sources=[x["key"] for x in sources], answerChars=len(answer))
        flush(force=True)
        jobs.update_item(Key={"id": job_id}, UpdateExpression="SET #s = :s, answer = :a, sources = :r, searches = :q",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":s": "done", ":a": answer,
                                                    ":r": json.dumps(sources, ensure_ascii=False),
                                                    ":q": json.dumps(searches, ensure_ascii=False)})
    except Exception:
        log.exception("答えを作れなかった: %s", job_id)
        jobs.update_item(Key={"id": job_id}, UpdateExpression="SET #s = :s, answer = :a",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":s": "failed", ":a": "答えを作れませんでした。もう一度お試しください"})


VOCAB_TTL = 60  # タグの一覧を控える秒数（足した・説明を変えたタグが、遅くともこの時間で質問に効く）
VOCAB_MAX_FOLDERS = 80  # 指示文に載せるフォルダの上限（件数の多い順）
_vocab_cache: dict[str, tuple[float, dict]] = {}


def department_vocabulary(dept: str) -> dict:
    """部署の、KB に入っている文書のタグの集計（台帳から）。フォルダは階層のまま、件数付き。"""
    hit = _vocab_cache.get(dept)
    if hit and time.time() - hit[0] < VOCAB_TTL:
        return hit[1]
    folders, extras = {}, {}
    for item in ledger.query_department(dept):
        if item.get("status") != "converted":
            continue  # 書き起こし中・失敗・対象外は KB に無いので、そのタグで絞っても見つからない
        if item.get("folder"):
            folders[item["folder"]] = folders.get(item["folder"], 0) + 1
        for t in item.get("extraTags", []):
            extras[t] = extras.get(t, 0) + 1
    described = {t["name"]: t["description"] for t in tag_table.list_tags(dept) if t["description"]}
    vocab = {"folders": folders, "extras": extras, "descriptions": described}
    _vocab_cache[dept] = (time.time(), vocab)
    return vocab


def search_vocabulary(departments) -> str:
    """エージェントに教える、絞り込みに使えるタグ。KB に入っている文書のタグだけを、件数付きで。

    フォルダは階層のまま（契約/2026）伝える。タグ（tags）にはフォルダの各階層が入っているので、
    フォルダで絞るときは各階層の listContains を andAll でまとめる。多いときは件数の多い順に上限まで。"""
    folders, extras, descriptions = {}, {}, {}
    for d in departments:
        v = department_vocabulary(d)
        descriptions.update(v["descriptions"])
        for k, n in v["folders"].items():
            folders[k] = folders.get(k, 0) + n
        for k, n in v["extras"].items():
            extras[k] = extras.get(k, 0) + n
    ranked = sorted(folders.items(), key=lambda x: (-x[1], x[0]))
    lines = ["フォルダ（階層は / 区切り。件数は KB に入っている文書の数）:"]
    lines += [f"- {k}（{n}件）" for k, n in ranked[:VOCAB_MAX_FOLDERS]] or ["- （なし）"]
    if len(ranked) > VOCAB_MAX_FOLDERS:
        lines.append(f"- ほか {len(ranked) - VOCAB_MAX_FOLDERS} フォルダ（件数の少ないもの）")
    lines.append("追加のタグ（フォルダをまたぐ分類。: のあとは管理者が書いたタグの説明）:")
    lines += [f"- {k}（{n}件）" + (f": {descriptions[k]}" if descriptions.get(k) else "")
              for k, n in sorted(extras.items(), key=lambda x: (-x[1], x[0]))] or ["- （なし）"]
    return "\n".join(lines)


def build_sources(user: User, results: list[dict]) -> list[dict]:
    """検索で返った文書から、画面に出す元ファイルの一覧を作る。"""
    sources, seen = [], {}
    texts: dict[str, str] = {}
    for r in results:
        meta = r.get("metadata", {})
        key = meta.get("original_key", "")
        try:
            doc = DocRef.from_original_key(key)
        except ValueError:
            continue
        # KB は ACL で絞っているが、元ファイルを渡す前に部署をもう一度確かめる
        if doc.department not in user.departments:
            log.error("他部署の検索結果が返った: %s (user=%s)", key, user.email)
            continue
        if key not in texts:
            texts[key] = read_text(doc.text_key)
        page = locate_page(texts[key], r.get("text", ""))
        if key in seen:  # 同じ文書の別の箇所はページだけ足す
            if page and page not in seen[key]["pages"]:
                seen[key]["pages"].append(page)
            continue
        seen[key] = {"key": key, "fileName": doc.file_name, "folder": doc.folder,
                     "department": DEPARTMENTS[doc.department], "tags": meta.get("tags", []),
                     "excerpt": r.get("text", "")[:300], "pages": [page] if page else [],
                     "pageKind": page_kind(doc.file_name), "url": presign_get(doc)}
        sources.append(seen[key])
    for src in sources:
        src["pages"].sort()
    return sources


PAGE_MARKS = {"page": re.compile(r"<!--page(\d+)-->"), "slide": re.compile(r"##スライド(\d+)")}


def page_kind(file_name: str) -> str:
    ext = file_name.rsplit(".", 1)[-1].lower()
    return "page" if ext == "pdf" else "slide" if ext == "pptx" else ""


def locate_page(markdown: str, chunk: str) -> int | None:
    """検索で返った部分が、書き起こしのどのページ（スライド）にあるか。

    書き起こしには、PDF は各ページの先頭に <!-- page N -->、PowerPoint は ## スライド N がある。
    返った部分の先頭を書き起こしの中で探し、その手前にある最後の印をページとする。
    Managed KB のページ番号（_excerpt_page_number）は取り込みの後に消える不具合があるので使わない。"""
    if not markdown or not chunk:
        return None
    flat = re.sub(r"\s+", "", markdown)
    head = re.sub(r"\s+", "", chunk)
    if len(head) < 6:
        return None
    for size in (60, 30, 12):
        at = flat.find(head[:size])
        if at >= 0:
            break
    else:
        return None
    for pattern in PAGE_MARKS.values():
        marks = pattern.findall(flat[:at + len(head[:60])])
        if marks:
            return int(marks[-1])
    return None


# ---- 書き起こしの確認と修正 ----
# 書き起こしは中身（SHA-256）ごとに持つ（control/transcripts/<sha>.md）。直すとそれを書き換え、同じ中身の
# 文書（コピー・移動したもの）すべてに反映する。最初に直す前の生成AIの書き起こしは <sha>.orig.md に残す。
# 人が直した書き起こしは、書き起こし直し（scripts/reconvert.py）でも残す。

# 区切りの印（PDF のページ・スライド・シート）。印ごとに、区切り方と番号の取り出し方を持つ
PAGE_MARKERS = [re.compile(r"^<!-- page (\d+) -->", re.M), re.compile(r"^## スライド (\d+)", re.M),
                re.compile(r"^## シート: (.+)$", re.M)]
PAGE_NUMBER = re.compile(r"^(?:<!-- page (\d+) -->|## スライド (\d+)|## シート: (.+))")


def split_pages(markdown: str) -> list[dict]:
    """書き起こしをページ（PDF）・スライド・シートごとに分ける。印が無ければ全体で1つ。
    印より前の部分（ヘッダーなど）は「先頭」として分ける。つなげると元に戻る。"""
    for marker in PAGE_MARKERS:
        starts = [m.start() for m in marker.finditer(markdown)]
        if not starts:
            continue
        bounds = ([0] if starts[0] > 0 else []) + starts + [len(markdown)]
        out = []
        for i in range(len(bounds) - 1):
            part = markdown[bounds[i]:bounds[i + 1]]
            m = marker.match(part)
            out.append({"index": i, "label": m.group(1).strip() if m else "先頭", "text": part})
        return out
    return [{"index": 0, "label": "全体", "text": markdown}]


def transcript_of(doc: DocRef) -> tuple[dict, dict, str]:
    item = ledger.get(doc)
    if not item or item.get("status") != "converted":
        raise HttpError(409, "まだナレッジに入っていないファイルです")
    c = ledger.content(item["sha"])
    text = read_text(f"control/transcripts/{item['sha']}.md")
    if not text:
        raise HttpError(404, "書き起こしが見つかりません")
    return item, c, text


def get_text(user: User, event) -> dict:
    doc = doc_of(user, query(event, "key"))
    item, c, text = transcript_of(doc)
    return {"key": doc.original_key, "fileName": doc.file_name, "pageKind": page_kind(doc.file_name),
            "pages": split_pages(text), "edited": bool(c.get("edited")), "editedBy": c.get("editedBy", ""),
            "editedAt": c.get("editedAt", ""), "version": int(c.get("editVersion", 0)), "url": presign_get(doc)}


def put_text(user: User, event) -> dict:
    """書き起こしのページ（index）を直す。version が今のものと違えば（ほかの人が先に直した）断る。"""
    body = json_body(event)
    doc = doc_of(user, str(body.get("key", "")))
    item, c, text = transcript_of(doc)
    version = int(c.get("editVersion", 0))
    if int(body.get("version", -1)) != version:
        raise HttpError(409, "ほかの人が先に直しました。読み込み直してから直してください")
    pages = split_pages(text)
    index = int(body.get("index", 0))
    if not 0 <= index < len(pages):
        raise HttpError(400, "ページの指定が正しくありません")
    new_text = str(body.get("text", ""))
    marker = PAGE_NUMBER.match(pages[index]["text"])
    if marker and not new_text.startswith(marker.group(0)):  # ページの印は消させない（ページの割り出しに使う）
        new_text = marker.group(0) + "\n\n" + new_text.lstrip()
    if not new_text.endswith("\n"):
        new_text += "\n"
    pages[index]["text"] = new_text
    merged = "".join(p["text"] for p in pages)
    sha = item["sha"]
    if not c.get("edited"):  # 最初に直す前の、生成AIの書き起こしを残す
        s3.copy_object(Bucket=BUCKET, Key=f"control/transcripts/{sha}.orig.md",
                       CopySource={"Bucket": BUCKET, "Key": f"control/transcripts/{sha}.md"})
    s3.put_object(Bucket=BUCKET, Key=f"control/transcripts/{sha}.md", Body=merged.encode("utf-8"),
                  ContentType="text/markdown; charset=utf-8")
    ok = ledger.conditional(ledger.contents.update_item, Key={"sha": sha},
                            UpdateExpression="SET edited = :t, editedBy = :u, editedAt = :at, editVersion = :n",
                            ConditionExpression="attribute_not_exists(editVersion) OR editVersion = :v",
                            ExpressionAttributeValues={":t": True, ":u": user.email, ":at": ledger.now(),
                                                       ":n": version + 1, ":v": version})
    if not ok:
        raise HttpError(409, "ほかの人が先に直しました。読み込み直してから直してください")
    # 同じ中身の文書すべての KB の書き起こしを作り直す（先頭の行はそれぞれの文書のもの）
    for other in ledger.documents_with_sha(sha):
        if other.get("status") != "converted":
            continue
        d = DocRef(other["key"])
        head = read_text(d.text_key).split("\n\n", 2)
        header = "\n\n".join(head[:2]) + "\n\n" if len(head) == 3 else ""
        s3.put_object(Bucket=BUCKET, Key=d.text_key, Body=(header + merged).encode("utf-8"),
                      ContentType="text/markdown; charset=utf-8")
    request_sync(s3, lambda_client)
    audit.record("edit_text", user.email, key=doc.original_key, sha=sha, page=pages[index]["label"],
                 version=version + 1)
    return {"version": version + 1}


def list_notifications(user: User, event) -> dict:
    items = notifications.query(KeyConditionExpression=Key("user").eq(user.email), ScanIndexForward=False,
                                Limit=50)["Items"]
    return {"items": [{k: i.get(k) for k in ("sk", "key", "fileName", "status", "title", "reason", "read", "createdAt")}
                      for i in items], "unread": sum(1 for i in items if not i.get("read"))}


def read_notifications(user: User, event) -> dict:
    for sk in [str(x) for x in json_body(event).get("ids", [])][:50]:
        notifications.update_item(Key={"user": user.email, "sk": sk}, UpdateExpression="SET #r = :t",
                                  ConditionExpression="attribute_exists(sk)",
                                  ExpressionAttributeNames={"#r": "read"}, ExpressionAttributeValues={":t": True})
    return {"ok": True}


ROUTES = {
    ("GET", "/api/me"): me,
    ("GET", "/api/files"): list_files,
    ("GET", "/api/storage/locations"): storage_locations,
    ("POST", "/api/storage/credentials"): storage_credentials,
    ("GET", "/api/download-url"): download_url,
    ("PUT", "/api/files/tags"): set_file_tags,
    ("GET", "/api/tags"): get_department_tags,
    ("POST", "/api/tags"): create_tag,
    ("PUT", "/api/tags"): update_tag,
    ("PUT", "/api/tags/order"): order_tags,
    ("DELETE", "/api/tags"): delete_tag,
    ("POST", "/api/chat"): chat,
    ("GET", "/api/chat"): chat_status,
    ("GET", "/api/files/text"): get_text,
    ("PUT", "/api/files/text"): put_text,
    ("GET", "/api/notifications"): list_notifications,
    ("POST", "/api/notifications/read"): read_notifications,
}
