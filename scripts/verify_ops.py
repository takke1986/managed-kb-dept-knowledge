"""監視・操作の記録・お知らせ・書き起こしの修正の確認。終わったら置いたものを消し、変えたものを戻す。

1 監視: アラームがすべて通知先（SNS）につながる。鳴ると通知が届く。API にわざと 500 を返させると実際に鳴る
3 記録: 開く・質問・認証情報の発行・権限の無い操作の試みがアプリの記録に、ファイル置き場への直接の書き込みが
        CloudTrail に（メールアドレス付きで）残る。部署を変えると、5分おきを待たずに ACL が書き直される
5 お知らせ: マルウェアの疑い・対象外のファイルを置くと、置いた人のお知らせに入る。既読にできる
2 修正: 直すと検索にも効く。ほかの人が先に直していたら断る。他部署の人は見られない。同じ中身の文書にも効く。
        書き起こし直しでも直したものは残り、捨てる指定のときだけ戻る。直した記録が残る

    .venv/bin/python scripts/verify_ops.py        （CloudTrail の反映を待つので 15〜20分かかる）
"""

import json
import sys
import time
import urllib.parse

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

c = v.Checks()
R = v.REGION
fb, db = v.OUT["FilesBucket"], v.OUT["DocsBucket"]
cw = v.boto3.client("cloudwatch", region_name=R)
sns = v.boto3.client("sns", region_name=R)
sqs = v.boto3.client("sqs", region_name=R)
logs = v.boto3.client("logs", region_name=R)
s3 = v.boto3.client("s3", region_name=R)
ct = v.boto3.client("cloudtrail", region_name=R)
tok = {e: v.token(e) for e in (v.SALES, v.LEGAL, v.BOTH)}
sales = v.storage(tok[v.SALES], "sales")
started = int(time.time() * 1000)
placed = []
queue_url = None


def insights(group, query, since_ms):
    qid = logs.start_query(logGroupName=group, startTime=since_ms // 1000 - 60, endTime=int(time.time()) + 60,
                           queryString=query, limit=200)["queryId"]
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            return [{f["field"]: f["value"] for f in row} for row in r["results"]]
        time.sleep(1)


def wait_status(key, statuses, timeout=240):
    start = time.time()
    while time.time() - start < timeout:
        _, r = v.api("GET", "/api/files?department=sales", tok[v.SALES])
        f = next((f for f in r["files"] if f["key"] == key), None)
        if f and f["status"] in statuses:
            return f
        time.sleep(6)
    return f


def put(key, body):
    sales.put_object(Bucket=fb, Key=key, Body=body)
    placed.append(key)


try:
    # ================= 1 監視 =================
    topic = v.OUT["AlertsTopic"]
    alarms = [a for p in cw.get_paginator("describe_alarms").paginate(AlarmNamePrefix="ManagedKbPrototype-")
              for a in p["MetricAlarms"]]
    c.check(f"アラームがすべて通知先につながっている（{len(alarms)} 個）",
            len(alarms) >= 15 and all(topic in a["AlarmActions"] for a in alarms), len(alarms))
    queue_url = sqs.create_queue(QueueName=f"mkbproto-verify-alerts-{int(time.time())}")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "sns.amazonaws.com"}, "Action": "sqs:SendMessage", "Resource": qarn,
        "Condition": {"ArnEquals": {"aws:SourceArn": topic}}}]})})
    sub = sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn, ReturnSubscriptionArn=True)["SubscriptionArn"]
    dlq_alarm = next(a["AlarmName"] for a in alarms if "ConvertDlq" in a["AlarmName"])
    time.sleep(5)
    cw.set_alarm_state(AlarmName=dlq_alarm, StateValue="ALARM", StateReason="確認用に鳴らす")
    got = []
    for _ in range(10):
        got += sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=5).get("Messages", [])
        if got:
            break
    c.check("アラームが鳴ると通知が届く", any(dlq_alarm in m["Body"] for m in got), len(got))
    cw.set_alarm_state(AlarmName=dlq_alarm, StateValue="OK", StateReason="確認のあと戻す")

    # API にわざと 500 を返させる（壊れた JSON を送る）
    req = v.urllib.request.Request(v.API + "/api/chat", data=b"{not json", method="POST", headers={
        "x-app-token": tok[v.SALES], "Content-Type": "application/json",
        "x-amz-content-sha256": v.hashlib.sha256(b"{not json").hexdigest()})
    try:
        v.urllib.request.urlopen(req, timeout=30)
        status = 200
    except v.urllib.error.HTTPError as e:
        status = e.code
    api_alarm = next(a["AlarmName"] for a in alarms if "Api500" in a["AlarmName"])
    for _ in range(40):  # 5分ごとの集計なので、鳴るまで最大10分ほど
        state = cw.describe_alarms(AlarmNames=[api_alarm])["MetricAlarms"][0]["StateValue"]
        if state == "ALARM":
            break
        time.sleep(15)
    c.check("API が 500 を返すと、アラームが実際に鳴る", status == 500 and state == "ALARM", (status, state))

    # ================= 5 お知らせ / 2 修正 の準備（置く） =================
    eicar = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS" + "-TEST-FILE!$H+H*"
    run = time.strftime("%H%M%S")  # 実行ごとに名前と中身を変える（前の実行のお知らせ・直した書き起こしに影響されない）
    k_virus, k_skip = f"sales/確認用/運用/{run}/テスト用ウイルス.txt", f"sales/確認用/運用/{run}/図面.dwg"
    k_text, k_copy = f"sales/確認用/運用/{run}/会議の案内.txt", f"sales/確認用/運用/{run}/写し/会議の案内.txt"
    put(k_virus, eicar.encode())
    put(k_skip, b"AC1032" + run.encode() + b"\0" * 4000)
    put(k_text, f"定例会議は第3会議室で行う。議題は予算の見直し。（確認 {run}）".encode())

    # ================= 3 記録 =================
    status, _ = v.api("POST", "/api/storage/credentials", tok[v.SALES],
                      {"scope": f"s3://{fb}/legal/*", "permissions": ["list"]})  # 権限の無い試み
    f = wait_status(k_text, ("converted",))
    v.api("GET", f"/api/download-url?key={urllib.parse.quote(k_text)}", tok[v.SALES])
    v.chat(tok[v.SALES], "定例会議はどこで行いますか？")
    expected = {"storage_credentials", "denied", "open_original", "chat"}
    for _ in range(8):  # Logs Insights は書いた直後の記録を返さないことがあるので、そろうまで問い合わせ直す
        time.sleep(15)
        rows = insights(v.OUT["AuditLogGroup"], f'filter user = "{v.SALES}" | fields @message', started)
        actions = {json.loads(r["@message"])["action"] for r in rows}
        if expected <= actions:
            break
    for action, label in (("storage_credentials", "ファイル置き場の認証情報の発行"), ("denied", "権限の無い操作の試み"),
                          ("open_original", "元ファイルを開いた"), ("chat", "質問と答えに使った元ファイル")):
        c.check(f"記録に残る: {label}", action in actions, sorted(actions))
    trail = ct.describe_trails(trailNameList=["mkbproto-audit"])["trailList"][0]
    c.check("CloudTrail: 改ざんの検知が有効", trail.get("LogFileValidationEnabled") is True)
    lock = s3.get_object_lock_configuration(Bucket=trail["S3BucketName"])["ObjectLockConfiguration"]
    c.check("CloudTrail: 記録の置き場所は1年間書き換え・削除できない",
            lock["Rule"]["DefaultRetention"] == {"Mode": "GOVERNANCE", "Days": 365}, lock)

    # 部署の変更 → ACL の書き直し（すぐ）
    acl_group = [g["logGroupName"] for g in logs.describe_log_groups(logGroupNamePrefix="ManagedKbPrototype-AclSyncLogs")["logGroups"]][0]
    changed_at = int(time.time() * 1000)
    v.cognito.admin_remove_user_from_group(UserPoolId=v.OUT["UserPoolId"], Username=v.BOTH, GroupName="legal")
    try:
        hit = False
        for _ in range(24):
            ev = logs.filter_log_events(logGroupName=acl_group, startTime=changed_at,
                                        filterPattern='"department-changed"').get("events", [])
            if ev:
                hit = (ev[0]["timestamp"] - changed_at) / 1000
                break
            time.sleep(10)
        c.check("部署を変えると、5分おきを待たずに ACL が書き直される", hit is not False and hit < 240, hit)
    finally:
        v.cognito.admin_add_user_to_group(UserPoolId=v.OUT["UserPoolId"], Username=v.BOTH, GroupName="legal")

    # ================= 5 お知らせ =================
    wait_status(k_virus, ("blocked",))
    wait_status(k_skip, ("skipped",))
    time.sleep(5)
    _, n = v.api("GET", "/api/notifications", tok[v.SALES])
    mine = {i["key"]: i for i in n["items"] if i["key"] in (k_virus, k_skip)}
    c.check("マルウェアの疑いのファイルが、置いた人のお知らせに入る", mine.get(k_virus, {}).get("status") == "blocked", mine.get(k_virus))
    c.check("対象外のファイルが、置いた人のお知らせに入る", mine.get(k_skip, {}).get("status") == "skipped", mine.get(k_skip))
    _, other = v.api("GET", "/api/notifications", tok[v.LEGAL])
    c.check("ほかの人のお知らせには入らない", not any(i["key"] in (k_virus, k_skip) for i in other["items"]))
    v.api("POST", "/api/notifications/read", tok[v.SALES],
          {"ids": [i["sk"] for i in n["items"] if i["key"] in (k_virus, k_skip)]})
    _, n = v.api("GET", "/api/notifications", tok[v.SALES])
    c.check("既読にできる", all(i["read"] for i in n["items"] if i["key"] in (k_virus, k_skip)))

    # ================= 2 修正 =================
    status, t = v.api("GET", f"/api/files/text?key={urllib.parse.quote(k_text)}", tok[v.SALES])
    c.check("書き起こしを見られる", status == 200 and "第3会議室" in t["pages"][0]["text"], status)
    status, _ = v.api("GET", f"/api/files/text?key={urllib.parse.quote(k_text)}", tok[v.LEGAL])
    c.check("他部署の人は書き起こしを見られない", status == 403, status)
    fixed = t["pages"][0]["text"].replace("第3会議室", "第5会議室")
    status, r = v.api("PUT", "/api/files/text", tok[v.SALES],
                      {"key": k_text, "index": 0, "text": fixed, "version": t["version"]})
    c.check("書き起こしを直せる", status == 200, (status, r))
    status, _ = v.api("PUT", "/api/files/text", tok[v.BOTH],
                      {"key": k_text, "index": 0, "text": fixed + "（古い版からの上書き）", "version": t["version"]})
    c.check("ほかの人が先に直していたら断る", status == 409, status)

    v.wait_ingested([])
    for _ in range(8):
        _, res, _ = v.mcp_retrieve(tok[v.SALES], "定例会議はどの会議室で行う？")
        text = " ".join(x.get("content", {}).get("text", "") for x in (res or []))
        if "第5会議室" in text:
            break
        time.sleep(20)
    c.check("直した書き起こしで検索できる", "第5会議室" in text and "第3会議室" not in text)

    sales.copy_object(Bucket=fb, Key=k_copy, CopySource={"Bucket": fb, "Key": k_text})
    placed.append(k_copy)
    wait_status(k_copy, ("converted",))
    md = s3.get_object(Bucket=db, Key=v.text_key(k_copy))["Body"].read().decode()
    c.check("同じ中身の文書（コピー）にも、直した書き起こしが使われる", "第5会議室" in md)

    head = sales.head_object(Bucket=fb, Key=k_text)
    queue = next(u for u in sqs.list_queues(QueueNamePrefix="ManagedKbPrototype-ConvertQueue")["QueueUrls"])

    def reconvert(discard):
        before = v.boto3.resource("dynamodb", region_name=R).Table(v.OUT["DocumentsTable"]).get_item(
            Key={"department": "sales", "key": k_text})["Item"]["updatedAt"]
        sqs.send_message(QueueUrl=queue, MessageBody=json.dumps({
            "detail-type": "GuardDuty Malware Protection Object Scan Result",
            "detail": {"s3ObjectDetails": {"bucketName": fb, "objectKey": k_text, "versionId": head.get("VersionId")},
                       "scanResultDetails": {"scanResultStatus": "NO_THREATS_FOUND"}},
            "reconvert": True, "discardEdits": discard}, ensure_ascii=False))
        for _ in range(30):
            time.sleep(5)
            it = v.boto3.resource("dynamodb", region_name=R).Table(v.OUT["DocumentsTable"]).get_item(
                Key={"department": "sales", "key": k_text})["Item"]
            if it["updatedAt"] > before and it["status"] == "converted":
                break
        return s3.get_object(Bucket=db, Key=v.text_key(k_text))["Body"].read().decode()

    c.check("書き起こし直しをしても、人が直したものは残る", "第5会議室" in reconvert(False))
    c.check("捨てる指定のときだけ、生成AIの書き起こしに戻る", "第3会議室" in reconvert(True))
    time.sleep(10)
    rows = insights(v.OUT["AuditLogGroup"], 'filter action = "edit_text" | fields @message', started)
    c.check("直した記録が残る", any(json.loads(r["@message"]).get("key") == k_text for r in rows), len(rows))

    # CloudTrail（届くまで数分〜15分）
    trail_group = v.OUT["TrailLogGroup"]
    seen = []
    for _ in range(40):
        rows = insights(trail_group, 'filter eventSource = "s3.amazonaws.com" and eventName = "PutObject" '
                        '| fields userIdentity.arn, requestParameters.key', started)
        seen = [r for r in rows if r.get("requestParameters.key") == k_text]
        if seen:
            break
        time.sleep(30)
    c.check("ファイル置き場への直接の書き込みが、CloudTrail にメールアドレス付きで残る",
            bool(seen) and seen[0]["userIdentity.arn"].endswith("/" + v.SALES), seen[:1])
finally:
    for k in placed:
        sales.delete_object(Bucket=fb, Key=k)
    if queue_url:
        try:
            sns.unsubscribe(SubscriptionArn=sub)
        except Exception:
            pass
        sqs.delete_queue(QueueUrl=queue_url)
    print("  置いたものを消し、確認用の通知先を片付けた", flush=True)
sys.exit(c.summary())
