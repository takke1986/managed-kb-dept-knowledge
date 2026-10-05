"""台帳（DynamoDB）・版・突き合わせの確認。終わったら置いたものを消す。

- 保護: バケットの版・スタックを消しても残す設定、表のポイントインタイムリカバリと削除保護
- 紐付け: 置いたファイルが台帳に入り、版・中身のハッシュ・書き起こしの場所がそろう
- 順番: 古い「消された」・古い版の検査結果を、あとから入れても無視される
- 上書き: 同じ名前で中身を変えると、版と中身のハッシュが変わり、書き起こしも新しくなる
- 削除と復元: 消すと台帳と書き起こしが消え、前の版から戻すと書き起こしを使い回して戻る
- 突き合わせ: 消えた書き起こしを作り直す、取り残しを消す、止まったものを入れ直す、元ファイルの無い台帳を消す

    .venv/bin/python scripts/verify_ledger.py
"""

import json
import sys
import time
import urllib.parse

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

c = v.Checks()
fb, db = v.OUT["FilesBucket"], v.OUT["DocsBucket"]
s3 = v.boto3.client("s3", region_name=v.REGION)
sqs = v.boto3.client("sqs", region_name=v.REGION)
ddb = v.boto3.resource("dynamodb", region_name=v.REGION)
docs_table = ddb.Table(v.OUT["DocumentsTable"])
tok = v.token(v.SALES)
sales = v.storage(tok, "sales")
KEY = "sales/確認用/台帳/連絡メモ.txt"
queue = next(u for u in sqs.list_queues(QueueNamePrefix="ManagedKbPrototype-ConvertQueue")["QueueUrls"])


def item(key=KEY):
    return docs_table.get_item(Key={"department": key.split("/")[0], "key": key}).get("Item")


def wait_item(check, key=KEY, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        it = item(key)
        if check(it):
            return it
        time.sleep(4)
    return item(key)


def exists(key):
    try:
        s3.head_object(Bucket=db, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def send(body):
    sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(body, ensure_ascii=False))


def reconcile():
    r = v.lam.invoke(FunctionName=v.OUT["ReconcileFunction"], Payload=b"{}")
    return json.loads(r["Payload"].read())


# ---- 保護 ----
cfn = v.boto3.client("cloudformation", region_name=v.REGION)
template = json.loads(json.dumps(cfn.get_template(StackName="ManagedKbPrototype")["TemplateBody"]))
kept = {logical: res.get("DeletionPolicy") for logical, res in template["Resources"].items()
        if res["Type"] in ("AWS::S3::Bucket", "AWS::DynamoDB::Table")
        and any(logical.startswith(p) for p in ("Files", "Docs", "Documents", "Contents"))}
c.check("元ファイル・書き起こし・台帳はスタックを消しても残す", kept and all(p == "Retain" for p in kept.values()), kept)
c.check("バケットに版がある", all(s3.get_bucket_versioning(Bucket=b).get("Status") == "Enabled" for b in (fb, db)))
dyn = v.boto3.client("dynamodb", region_name=v.REGION)
for name in (v.OUT["DocumentsTable"], v.OUT["ContentsTable"]):
    pitr = dyn.describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
    prot = dyn.describe_table(TableName=name)["Table"].get("DeletionProtectionEnabled")
    c.check(f"表 {name.split('-')[1]}: ポイントインタイムリカバリと削除保護", pitr["PointInTimeRecoveryStatus"] == "ENABLED" and prot)

try:
    # ---- 紐付け ----
    v1 = sales.put_object(Bucket=fb, Key=KEY, Body="第1版: 定例会議は火曜日。".encode())["VersionId"]
    it = wait_item(lambda i: i and i.get("status") == "converted")
    c.check("置いたファイルが台帳に入り、取り込み済みになる", it and it["status"] == "converted", it and it.get("status"))
    c.check("台帳に版・中身のハッシュ・書き起こしの場所・置いた人がある",
            it and it["versionId"] == v1 and len(it.get("sha", "")) == 64 and exists(it["textKey"])
            and it.get("uploadedBy") == v.SALES, it and {k: it.get(k) for k in ("versionId", "textKey", "uploadedBy")})
    sha1 = it["sha"]

    # ---- 順番: 古い「消された」は無視 ----
    send({"Records": [{"eventName": "ObjectRemoved:Delete",
                       "s3": {"object": {"key": urllib.parse.quote_plus(KEY), "sequencer": "0001"}}}]})
    time.sleep(20)
    it = item()
    c.check("古い「消された」を入れても、台帳と書き起こしは残る", it and it["status"] == "converted" and exists(it["textKey"]))

    # ---- 上書き ----
    v2 = sales.put_object(Bucket=fb, Key=KEY, Body="第2版: 定例会議は水曜日に変わった。".encode())["VersionId"]
    it = wait_item(lambda i: i and i.get("versionId") == v2 and i.get("status") == "converted")
    text = s3.get_object(Bucket=db, Key=it["textKey"])["Body"].read().decode()
    sha2 = it["sha"]
    c.check("上書きすると版と中身のハッシュが変わり、書き起こしも新しくなる",
            it["versionId"] == v2 and it["sha"] != sha1 and "水曜日" in text, (it["versionId"] == v2, "水曜日" in text))

    # ---- 順番: 古い版の検査結果は無視 ----
    send({"detail-type": "GuardDuty Malware Protection Object Scan Result", "detail": {
        "s3ObjectDetails": {"bucketName": fb, "objectKey": KEY, "versionId": v1},
        "scanResultDetails": {"scanResultStatus": "NO_THREATS_FOUND"}}})
    time.sleep(20)
    it = item()
    text = s3.get_object(Bucket=db, Key=it["textKey"])["Body"].read().decode()
    c.check("古い版の検査結果を入れても、新しい版の書き起こしのまま", it["versionId"] == v2 and "水曜日" in text)

    # ---- 突き合わせ ----
    s3.delete_object(Bucket=db, Key=it["textKey"])
    s3.delete_object(Bucket=db, Key=it["textKey"] + ".metadata.json")
    orphan = "kb-source/sales/0000orphan/取り残し.txt.md"
    s3.put_object(Bucket=db, Key=orphan, Body="取り残し".encode())
    ghost = "sales/確認用/台帳/元ファイルの無い台帳.txt"
    docs_table.put_item(Item={"department": "sales", "key": ghost, "fileName": "元ファイルの無い台帳.txt",
                              "status": "converted", "versionId": "x", "updatedAt": "2026-01-01T00:00:00Z"})
    r = reconcile()
    c.check("突き合わせが、消えた書き起こしを作り直す", exists(it["textKey"]) and exists(it["textKey"] + ".metadata.json"), r)
    c.check("突き合わせが、台帳に無い書き起こし（取り残し）を消す", not exists(orphan))
    c.check("突き合わせが、元ファイルの無い台帳を消す", item(ghost) is None)

    # 移行: 台帳に無い（台帳を入れる前からある）文書を登録し直しても、その書き起こしを消さない
    # （実際に、登録したばかりの文書の書き起こしを取り残しとして消す不具合があった）
    docs_table.delete_item(Key={"department": "sales", "key": KEY})
    r = reconcile()
    c.check("突き合わせが、台帳に無い文書を登録し直し、その書き起こしは消さない",
            r.get("registered", 0) >= 1 and item() and item()["status"] == "converted"
            and exists(it["textKey"]) and exists(it["textKey"] + ".metadata.json"), r)

    docs_table.update_item(Key={"department": "sales", "key": KEY}, UpdateExpression="SET #s = :s, updatedAt = :u",
                           ExpressionAttributeNames={"#s": "status"},
                           ExpressionAttributeValues={":s": "converting", ":u": "2026-01-01T00:00:00Z"})
    r = reconcile()
    it = wait_item(lambda i: i and i.get("status") == "converted")
    c.check("突き合わせが、止まったものを入れ直して取り込み済みにする", r.get("requeued", 0) >= 1 and it["status"] == "converted", r)

    # ---- 削除と復元 ----
    sales.delete_object(Bucket=fb, Key=KEY)
    gone = wait_item(lambda i: i is None, timeout=90)
    c.check("消すと台帳と書き起こしが消える", gone is None and not exists(it["textKey"]))
    versions = s3.list_object_versions(Bucket=fb, Prefix=KEY)["Versions"]
    c.check("消しても前の版は残っている", any(x["VersionId"] == v2 for x in versions), len(versions))
    s3.copy_object(Bucket=fb, Key=KEY, CopySource={"Bucket": fb, "Key": KEY, "VersionId": v2})
    it = wait_item(lambda i: i and i.get("status") == "converted")
    c.check("前の版から戻すと、同じ中身の書き起こしを使い回して取り込み済みに戻る",
            it and it.get("sha") == sha2 and exists(it["textKey"]), it and it.get("status"))
finally:
    sales.delete_object(Bucket=fb, Key=KEY)
    print("  置いたものを消した", flush=True)
sys.exit(c.summary())
