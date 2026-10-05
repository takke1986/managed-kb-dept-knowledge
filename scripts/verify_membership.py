"""部署の入れ替えと、止まった変換の確認。scripts/verify.py のあとに流す（書類が置いてある前提）。

- 変換が2回続けて途中で止まったとき（DLQ に移ったとき）、エラーが残り、取り込み状況が「失敗」になる
- Cognito のコンソールで部署を変えた（scripts/users.py を通さない）ときも:
  - 外した直後から、API はその部署のフォルダの認証情報を出さない（トークンは古いままでも）
  - 定期の書き直し（acl_sync）のあと、発行済みの認証情報はその部署のフォルダだけ使えなくなる
  - 検索にもその部署の文書が出なくなる
  - 戻すと元どおり

兼務の人（mkb-both）を法務部から一時的に外す。終わったら必ず戻す。

    .venv/bin/python scripts/verify_membership.py
"""

import json
import sys
import time


sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

c = v.Checks()
fb = v.OUT["FilesBucket"]
pool = v.OUT["UserPoolId"]


def acl_sync():
    r = v.lam.invoke(FunctionName=v.OUT["AclSyncFunction"], Payload=b"{}")
    return json.loads(r["Payload"].read())


def wait_sync():
    time.sleep(10)
    while True:
        job = v.bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=v.OUT["KnowledgeBaseId"], dataSourceId=v.OUT["DataSourceId"],
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"}, maxResults=1)["ingestionJobSummaries"][0]
        if job["status"] not in ("STARTING", "IN_PROGRESS"):
            return
        time.sleep(15)


def search_departments(tok):
    _, res, err = v.mcp_retrieve(tok, "契約書の反社会的勢力の排除条項と、見積書の合計金額")
    return err or v.departments_of(res)


def main():
    # ---- 止まった変換: 失敗先に、落ちたときと同じ形のイベントを渡す ----
    tok = v.token(v.SALES)
    sales = v.storage(tok, "sales")
    key = "sales/確認用/止まった変換.txt"
    sales.put_object(Bucket=fb, Key=key, Body="止まった変換の確認".encode())
    time.sleep(40)  # 検査と書き起こしが終わるのを待つ
    failed_fn = next(f["FunctionName"] for p in v.lam.get_paginator("list_functions").paginate()
                     for f in p["Functions"] if f["FunctionName"].startswith("ManagedKbPrototype-ConvertFailed"))
    scan_result = {"detail-type": "GuardDuty Malware Protection Object Scan Result", "detail": {
        "s3ObjectDetails": {"bucketName": fb, "objectKey": key},
        "scanResultDetails": {"scanResultStatus": "NO_THREATS_FOUND"}}}
    # 書き起こし中のまま止まった状態にしてから（台帳）、DLQ から届いたときと同じように失敗先を呼ぶ
    ddb = v.boto3.resource("dynamodb", region_name=v.REGION).Table(v.OUT["DocumentsTable"])
    ddb.update_item(Key={"department": "sales", "key": key}, UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "converting"})
    v.lam.invoke(FunctionName=failed_fn, Payload=json.dumps({  # DLQ から届く形（検査結果のイベント）
        "Records": [{"eventSource": "aws:sqs", "body": json.dumps(scan_result)}],
    }).encode())
    _, files = v.api("GET", "/api/files?department=sales", tok)
    f = next((f for f in files["files"] if f["key"] == key), {})
    c.check("変換が止まって DLQ に移ると、取り込み状況が「失敗」になる", f.get("status") == "failed", f.get("status"))
    sales.delete_object(Bucket=fb, Key=key)

    # ---- 部署の入れ替え（Cognito の API で直接。scripts/users.py を通さない） ----
    both = v.token(v.BOTH)  # 外す前のトークン（cognito:groups に legal が入ったまま）
    legal_old = v.storage(both, "legal")
    sales_old = v.storage(both, "sales")
    c.check("外す前: 兼務の人は法務部のフォルダを一覧できる",
            not v.denied(lambda: legal_old.list_objects_v2(Bucket=fb, Prefix="legal/")))
    acl_sync()  # 記録を今の状態にそろえる
    try:
        v.cognito.admin_remove_user_from_group(UserPoolId=pool, Username=v.BOTH, GroupName="legal")

        status, _ = v.api("POST", "/api/storage/credentials", both,
                          {"scope": f"s3://{fb}/legal/*", "permissions": ["list"]})
        c.check("外した直後: 古いトークンでも法務部の認証情報は出ない", status == 403, status)
        _, loc = v.api("GET", "/api/storage/locations", both)
        c.check("外した直後: 見えるフォルダは sales/ だけ", [l["prefix"] for l in loc["items"]] == ["sales/"], loc)

        result = acl_sync()  # 5分おきの定期実行と同じ（引数なし）
        c.check("定期の書き直しが、外れた人を見つける", result.get("legal", {}).get("removed") == [v.BOTH], result)
        time.sleep(15)  # IAM の反映を待つ
        c.check("書き直しのあと: 発行済みの法務部の認証情報は使えない",
                v.denied(lambda: legal_old.list_objects_v2(Bucket=fb, Prefix="legal/"))
                and v.denied(lambda: legal_old.get_object(Bucket=fb, Key=f"legal/確認用/{v.DOCS['legal'][0]}")))
        c.check("書き直しのあと: 営業部の認証情報は使える",
                not v.denied(lambda: sales_old.list_objects_v2(Bucket=fb, Prefix="sales/")))
        wait_sync()
        for _ in range(8):
            d = search_departments(both)
            if d == {"sales"}:
                break
            time.sleep(20)
        c.check("書き直しのあと: 検索に法務部の文書が出ない", d == {"sales"}, d)
    finally:
        v.cognito.admin_add_user_to_group(UserPoolId=pool, Username=v.BOTH, GroupName="legal")
        print("  法務部に戻した:", acl_sync(), flush=True)

    wait_sync()
    both = v.token(v.BOTH)
    legal_new = v.storage(both, "legal")
    c.check("戻したあと: 新しい認証情報で法務部のフォルダを一覧できる",
            not v.denied(lambda: legal_new.list_objects_v2(Bucket=fb, Prefix="legal/")))
    for _ in range(8):
        d = search_departments(both)
        if d == {"sales", "legal"}:
            break
        time.sleep(20)
    c.check("戻したあと: 検索に両方の部署の文書が出る", d == {"sales", "legal"}, d)
    sys.exit(c.summary())


if __name__ == "__main__":
    main()
