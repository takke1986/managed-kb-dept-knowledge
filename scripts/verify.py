"""デプロイ後の確認。部署の分離が4層それぞれで効いていることと、取り込み・画面の API を確かめる。

    .venv/bin/python scripts/verify.py                 書類を置き、取り込みを待って全部確かめる
    .venv/bin/python scripts/verify.py --skip-upload   置いた書類がもう取り込まれているとき

書類は Storage Browser と同じ手順（API から部署のフォルダ用の認証情報をもらって S3 に置く）で置く。

先に `scripts/users.py test-accounts` で確認用の3人（営業・法務・兼務）を作っておく。
書類は testdata/fixtures の架空の書類を使う。
Cedar の確認では Interceptor に一時的に故障を入れ、終わったら必ず戻す。
"""

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, UTC
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from project import REGION, ROOT, TESTDATA, accounts, outputs  # noqa: F401  （ほかのスクリプトが v.ROOT などで使う）

OUT = outputs()
API = OUT["WebUrl"].rstrip("/")  # API は CloudFront の /api/*
FIXTURES = TESTDATA / "fixtures"  # 架空の書類（スキャンの PDF・写真・Excel・Word）
DOCS = {
    "sales": ["スキャン-見積書.pdf", "見積-整合.xlsx", "見積書の写真.png"],
    "legal": ["契約書-反社あり.docx", "スキャン-契約書-反社なし.pdf"],
}
# Office の読み取りの確認用（scripts/make_office_fixtures.py が作る）。営業部に置く
sys.path.insert(0, str(ROOT / "scripts"))
from make_office_fixtures import EXPECTED as OFFICE_EXPECTED, OUT as OFFICE_DIR  # noqa: E402
ACCOUNTS = accounts()
SALES, LEGAL, BOTH = "mkb-sales@example.com", "mkb-legal@example.com", "mkb-both@example.com"

cognito = boto3.client("cognito-idp", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
agent_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)


class Checks:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append(ok)
        print(f"{'OK ' if ok else 'NG '} {name}" + (f"  ({detail})" if detail != "" else ""), flush=True)

    def summary(self) -> int:
        n, ok = len(self.results), sum(self.results)
        print(f"\n{ok}/{n} OK")
        return 0 if ok == n else 1


def token(email: str) -> str:
    r = cognito.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=OUT["UserPoolClientId"],
                              AuthParameters={"USERNAME": email, "PASSWORD": ACCOUNTS[email].strip()})
    return r["AuthenticationResult"]["AccessToken"]


def http(method, url, tok=None, body=None, headers=None, form=None):
    h = dict(headers or {})
    data = None
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if form is not None:
        data, ctype = form
        h["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return res.status, res.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def api(method, path, tok, body=None):
    """画面の API。トークンは x-app-token、本文の SHA-256 は x-amz-content-sha256 で送る（CloudFront の OAC）。"""
    headers = {"x-app-token": tok}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["x-amz-content-sha256"] = hashlib.sha256(data).hexdigest()
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            status, text = res.status, res.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode(errors="replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"raw": text}


def chat(tok, message, history=None, timeout=240, departments=None):
    """チャット。受け付けてから、答えができるまで問い合わせる（画面と同じ）。"""
    body = {"message": message, "history": history or []}
    if departments:
        body["departments"] = departments
    status, r = api("POST", "/api/chat", tok, body)
    if status != 200:
        return status, r
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(1.5)
        status, j = api("GET", f"/api/chat?id={r['jobId']}", tok)
        if status != 200 or j["status"] != "running":
            return (200 if j.get("status") == "done" else 500), j
    return 504, {"answer": "時間切れ"}


def storage(tok, dept):
    """Storage Browser と同じく、API から部署のフォルダ用の認証情報をもらって S3 を使う。"""
    status, r = api("POST", "/api/storage/credentials", tok,
                    {"scope": f"s3://{OUT['FilesBucket']}/{dept}/*", "permissions": ["get", "list", "write", "delete"]})
    assert status == 200, (status, r)
    c = r["credentials"]
    return boto3.client("s3", region_name=REGION, aws_access_key_id=c["accessKeyId"],
                        aws_secret_access_key=c["secretAccessKey"], aws_session_token=c["sessionToken"])


def upload(client, dept, path: Path, folder: str = "確認用") -> str:
    key = f"{dept}/{folder}/{path.name}"
    client.put_object(Bucket=OUT["FilesBucket"], Key=key, Body=path.read_bytes())
    return key


def doc_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def text_key(key: str) -> str:
    return f"kb-source/{key.split('/', 1)[0]}/{doc_id(key)}/{key.rsplit('/', 1)[-1]}.md"


def denied(fn) -> bool:
    try:
        fn()
        return False
    except ClientError as e:
        return e.response["Error"]["Code"] in ("AccessDenied", "403")


def mcp_retrieve(tok, query, user_context=None, tool="Retrieve", extra_headers=None, flt=None):
    """Gateway に直接 tools/call を送る（エージェントを通さない）。tool は Retrieve か AgenticRetrieveStream。"""
    if tool == "Retrieve":
        args = {"retrievalQuery": {"text": query}}
        if flt:
            args["retrievalConfiguration"] = {"managedSearchConfiguration": {"filter": flt}}
    else:
        args = {"messages": [{"role": "user", "content": {"text": query}}]}
    if user_context:
        args["userContext"] = user_context
    status, text = http("POST", OUT["GatewayUrl"], tok,
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": f"kb___{tool}", "arguments": args}},
                        headers={"Accept": "application/json, text/event-stream", **(extra_headers or {})})
    # 応答が SSE のときは data: 行を取り出す（AgenticRetrieveStream は途中経過の通知も来るので最後の結果を使う）
    if text.lstrip().startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        lines = [l[5:].strip() for l in text.splitlines() if l.startswith("data:")]
        msgs = [json.loads(l) for l in lines if l]
        msg = next((m for m in reversed(msgs) if "result" in m or "error" in m), msgs[-1] if msgs else {})
    else:
        msg = json.loads(text)
    result = msg.get("result") or {}
    if msg.get("error") or result.get("isError"):
        return status, None, json.dumps(msg, ensure_ascii=False)[:300]
    payload = json.loads(result["content"][0]["text"])
    items = payload.get("retrievalResults") or payload.get("results") or (payload.get("result") or {}).get("results") or []
    return status, items, ""


def keys_with_tag(tok, tag, query="問い合わせ件数と売上") -> set:
    """タグで絞って Retrieve したときに返る元ファイルのキー。"""
    _, items, err = mcp_retrieve(tok, query, flt={"listContains": {"key": "tags", "value": tag}})
    if items is None:
        raise RuntimeError(err)
    return {r["metadata"].get("original_key") for r in items}


def settle(fn, ok, tries=8, delay=20):
    """取り込みのあと、検索に反映されるまで少し待つ。ok(fn()) になるか、回数を使い切ったら最後の値を返す。"""
    for i in range(tries):
        value = fn()
        if ok(value) or i == tries - 1:
            return value
        time.sleep(delay)
    return value


def departments_of(results) -> set:
    return {r.get("metadata", {}).get("department") for r in results}


def wait_ingested(keys: list[str], timeout=1200):
    s3 = boto3.client("s3", region_name=REGION)
    start = time.time()
    while time.time() - start < timeout:
        pending = []
        for key in keys:
            try:
                s3.head_object(Bucket=OUT["DocsBucket"], Key=text_key(key) + ".metadata.json")
                continue
            except ClientError:
                pass
            try:  # 変換に失敗したものは待たない（あとの確認で NG になる）
                s3.head_object(Bucket=OUT["DocsBucket"], Key=f"errors/{key.split('/', 1)[0]}/{doc_id(key)}.txt")
                print(f"  変換に失敗: {key}", flush=True)
            except ClientError:
                pending.append(key)
        jobs = bedrock_agent.list_ingestion_jobs(knowledgeBaseId=OUT["KnowledgeBaseId"], dataSourceId=OUT["DataSourceId"],
                                                 sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                                                 maxResults=1)["ingestionJobSummaries"]
        running = jobs and jobs[0]["status"] in ("STARTING", "IN_PROGRESS")
        flag = True
        try:
            s3.head_object(Bucket=OUT["DocsBucket"], Key="control/sync-pending")
        except ClientError:
            flag = False
        print(f"  待機 {int(time.time() - start)}秒: 書き起こし待ち {len(pending)} 件, 取り込み中 {bool(running)}, 同期待ち {flag}",
              flush=True)
        if not pending and not running and not flag:
            return jobs[0] if jobs else None
        if flag and not running:
            lam.invoke(FunctionName=OUT["SyncFunction"], InvocationType="Event", Payload=b"{}")
        time.sleep(30)
    raise TimeoutError("取り込みが終わらない")


def ingestion_jobs_since(since) -> list[dict]:
    jobs = bedrock_agent.list_ingestion_jobs(knowledgeBaseId=OUT["KnowledgeBaseId"], dataSourceId=OUT["DataSourceId"],
                                             sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                                             maxResults=50)["ingestionJobSummaries"]
    return [j for j in jobs if j["startedAt"] >= since]


def kb_document_states() -> dict:
    """KB の文書の状態（kb-source/ より下のキー → INDEXED など）。"""
    states, tok = {}, None
    while True:
        kw = {"knowledgeBaseId": OUT["KnowledgeBaseId"], "dataSourceId": OUT["DataSourceId"], "maxResults": 100}
        if tok:
            kw["nextToken"] = tok
        r = bedrock_agent.list_knowledge_base_documents(**kw)
        for d in r["documentDetails"]:
            uri = d["identifier"].get("s3", {}).get("uri", "")
            key = uri.split(f"{OUT['DocsBucket']}/", 1)[-1]
            if states.get(key) != "INDEXED":
                states[key.split("/", 1)[1] if key.startswith("kb-source/") else key] = d["status"]
        tok = r.get("nextToken")
        if not tok:
            return states


def set_fault(user_id: str):
    fn = lam.get_function_configuration(FunctionName=interceptor_name())
    env = fn.get("Environment", {}).get("Variables", {})
    if user_id:
        env["FAULT_INJECT_USER_ID"] = user_id
    else:
        env.pop("FAULT_INJECT_USER_ID", None)
    lam.update_function_configuration(FunctionName=fn["FunctionName"], Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn["FunctionName"])


def interceptor_name() -> str:
    group = OUT["InterceptorLogGroup"]
    for page in lam.get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            if f["FunctionName"].startswith("ManagedKbPrototype-Interceptor"):
                return f["FunctionName"]
    raise RuntimeError(f"Interceptor が見つからない ({group})")


def main():
    c = Checks()
    tok = {e: token(e) for e in (SALES, LEGAL, BOTH)}

    # ---- 画面の API の権限 ----
    status, me = api("GET", "/api/me", tok[BOTH])
    c.check("兼務の人は2部署", status == 200 and {d["id"] for d in me["departments"]} == {"sales", "legal"}, me)
    status, _ = api("GET", "/api/me", "not-a-token")
    c.check("トークンが無効なら 401", status == 401, status)
    try:  # API は CloudFront（OAC）からしか呼べない
        urllib.request.urlopen(urllib.request.Request(OUT["FunctionUrl"].rstrip("/") + "/api/me",
                               headers={"x-app-token": tok[SALES]}), timeout=30)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    c.check("関数 URL を直接呼ぶと拒否される", status == 403, status)
    cf = boto3.client("cloudfront")
    dist_id = next(d["Id"] for d in cf.list_distributions()["DistributionList"]["Items"]
                   if d["DomainName"] == OUT["WebUrl"].replace("https://", ""))
    c.check("CloudFront に WAF が付いている", bool(cf.get_distribution_config(Id=dist_id)["DistributionConfig"].get("WebACLId")))
    status, _ = api("GET", "/api/files?department=legal", tok[SALES])
    c.check("営業の人は法務部の一覧を見られない", status == 403, status)

    # ---- Storage Browser: 部署のフォルダだけ ----
    _, loc = api("GET", "/api/storage/locations", tok[SALES])
    c.check("営業の人に見えるフォルダは sales/ だけ", [l["prefix"] for l in loc["items"]] == ["sales/"], loc)
    _, loc = api("GET", "/api/storage/locations", tok[BOTH])
    c.check("兼務の人には2つのフォルダが見える", sorted(l["prefix"] for l in loc["items"]) == ["legal/", "sales/"])
    for scope in (f"s3://{OUT['FilesBucket']}/legal/*", f"s3://{OUT['FilesBucket']}/*",
                  f"s3://{OUT['DocsBucket']}/kb-source/sales/*"):
        status, _ = api("POST", "/api/storage/credentials", tok[SALES], {"scope": scope, "permissions": ["list"]})
        c.check(f"営業の人に認証情報を出さない: {scope.split('//')[1][:40]}", status in (400, 403), status)
    sales_s3 = storage(tok[SALES], "sales")
    fb = OUT["FilesBucket"]
    c.check("営業の認証情報で法務部のフォルダに置けない",
            denied(lambda: sales_s3.put_object(Bucket=fb, Key="legal/x.txt", Body=b"x")))
    c.check("営業の認証情報で法務部のフォルダを一覧できない",
            denied(lambda: sales_s3.list_objects_v2(Bucket=fb, Prefix="legal/")))
    c.check("営業の認証情報でバケット全体を一覧できない",
            denied(lambda: sales_s3.list_objects_v2(Bucket=fb, Prefix="")))
    c.check("営業の認証情報で内部の置き場所を読めない",
            denied(lambda: sales_s3.list_objects_v2(Bucket=OUT["DocsBucket"], Prefix="kb-source/")))

    # ---- 置く → 書き起こし → 取り込み ----
    if "--skip-upload" not in sys.argv:
        clients = {"sales": sales_s3, "legal": storage(tok[LEGAL], "legal")}
        # 前回置いたものは消してから置き直す
        for dept, client in clients.items():
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=fb, Prefix=f"{dept}/確認用/"):
                for obj in page.get("Contents", []):
                    client.delete_object(Bucket=fb, Key=obj["Key"])
        keys = [upload(clients[dept], dept, FIXTURES / n) for dept, names in DOCS.items() for n in names]
        keys += [upload(sales_s3, "sales", OFFICE_DIR / n, "確認用/Office") for n in OFFICE_EXPECTED]
        print(f"{len(keys)} 件置いた。書き起こしと取り込みを待つ", flush=True)
        since = datetime.now(UTC)
        wait_ingested(keys)
        # 最後の1回の取り込みだけを見ると、前の回の失敗を見逃す（実際に見逃した）。置いたあとの全部の回と、
        # 文書ごとの状態を見る
        failed = [j for j in ingestion_jobs_since(since) if j.get("statistics", {}).get("numberOfDocumentsFailed")]
        c.check("置いたあとの取り込みに失敗が無い", not failed, [j["statistics"] for j in failed][:2])
        states = kb_document_states()
        not_indexed = [k for k in keys if states.get(text_key(k).split("/", 1)[1]) != "INDEXED"]
        c.check("置いた文書がすべて KB に入った（INDEXED）", not not_indexed,
                {k.rsplit("/", 1)[-1]: states.get(text_key(k).split("/", 1)[1]) for k in not_indexed})
    for dept in DOCS:
        _, files = api("GET", f"/api/files?department={dept}", tok[BOTH])
        names = {f["fileName"]: f for f in files.get("files", [])}
        converted = [n for n in DOCS[dept] if names.get(n, {}).get("status") == "converted"]
        c.check(f"{dept}: 置いた書類がすべて書き起こされた", len(converted) == len(DOCS[dept]),
                {n: names.get(n, {}).get("status") for n in DOCS[dept]})
        c.check(f"{dept}: フォルダ名がタグになっている",
                all("確認用" in names[n]["folderTags"] for n in converted),
                {n: names[n]["folderTags"] for n in converted})
    # ---- Office の読み取り（XML から本文・表・テキストボックス・グラフ、画像は生成AI） ----
    s3 = boto3.client("s3", region_name=REGION)
    _, files = api("GET", "/api/files?department=sales", tok[SALES])
    for name, marks in OFFICE_EXPECTED.items():
        f = next((f for f in files["files"] if f["fileName"] == name and f["status"] == "converted"), None)
        if not f:
            c.check(f"Office: {name} が書き起こされた", False)
            continue
        md = s3.get_object(Bucket=OUT["DocsBucket"], Key=text_key(f["key"]))["Body"].read().decode()
        missing = [m for m in marks if m not in md]
        c.check(f"Office: {name} の目印がすべて入っている（{len(marks)} 件）", not missing, missing)

    meta = json.loads(s3.get_object(Bucket=OUT["DocsBucket"], Key=text_key(f["key"]) + ".metadata.json")["Body"].read())
    attrs = meta["metadataAttributes"]
    c.check("置いた人とフォルダがメタデータに残る",
            attrs.get("uploaded_by") == SALES and attrs.get("folder") == "確認用/Office", attrs)

    _, files = api("GET", "/api/files?department=legal", tok[LEGAL])
    legal_key = files["files"][0]["key"]
    status, _ = api("GET", f"/api/download-url?key={urllib.parse.quote(legal_key)}", tok[SALES])
    c.check("営業の人は法務部の元ファイルを開けない", status == 403, status)
    status, r = api("GET", f"/api/download-url?key={urllib.parse.quote(legal_key)}", tok[LEGAL])
    c.check("法務の人は法務部の元ファイルを開ける", status == 200 and http("GET", r["url"])[0] == 200, status)

    # ---- ①Interceptor + ③ACL: Gateway を直接叩く ----
    q = "契約書の反社会的勢力の排除条項と、見積書の合計金額"
    _, res, err = mcp_retrieve(tok[SALES], q)
    c.check("営業: 検索結果は営業部の文書だけ", res is not None and res and departments_of(res) == {"sales"},
            err or departments_of(res))
    _, res, err = mcp_retrieve(tok[LEGAL], q)
    c.check("法務: 検索結果は法務部の文書だけ", res is not None and res and departments_of(res) == {"legal"},
            err or departments_of(res))
    _, res, err = mcp_retrieve(tok[BOTH], q)
    c.check("兼務: 両方の部署の文書が出る", res is not None and departments_of(res) == {"sales", "legal"},
            err or departments_of(res))
    _, res, err = mcp_retrieve(tok[BOTH], q, extra_headers={"x-search-departments": "sales"})
    c.check("兼務: 部署を選ぶと、その部署の文書だけ（Interceptor が条件を足す）",
            res is not None and res and departments_of(res) == {"sales"}, err or departments_of(res))
    _, res, err = mcp_retrieve(tok[SALES], q, {"userId": LEGAL})
    c.check("営業が法務の人を名乗っても営業部の文書だけ（Interceptor が上書き）",
            res is not None and res and departments_of(res) == {"sales"}, err or departments_of(res))
    q2 = "契約書の反社会的勢力の排除条項と、見積書の合計金額を比べて教えて"
    _, res, err = mcp_retrieve(tok[SALES], q2, tool="AgenticRetrieveStream")
    c.check("エージェント型の検索: 営業には営業部の文書だけ", res is not None and res and departments_of(res) == {"sales"},
            err or departments_of(res))
    _, res, err = mcp_retrieve(tok[SALES], q2, {"userId": LEGAL}, tool="AgenticRetrieveStream")
    c.check("エージェント型の検索: 法務の人を名乗っても営業部の文書だけ",
            res is not None and res and departments_of(res) == {"sales"}, err or departments_of(res))
    _, res, err = mcp_retrieve(tok[SALES], q, None)
    orig = [r for r in (res or []) if r.get("metadata", {}).get("original_key", "").startswith("sales/")]
    c.check("検索結果から元ファイルの場所が分かる", bool(orig), len(orig))
    status, _, err = mcp_retrieve("not-a-token", q)
    c.check("トークンが無効なら Gateway が拒否", status in (401, 403), status)

    # ---- ②Cedar: Interceptor が壊れて別人を入れたとき ----
    try:
        set_fault(LEGAL)
        time.sleep(5)
        _, res, err = mcp_retrieve(tok[SALES], q)
        c.check("Interceptor が別人を入れると Cedar が拒否する", res is None, err or departments_of(res))
        _, res, err = mcp_retrieve(tok[SALES], q, tool="AgenticRetrieveStream")
        c.check("（エージェント型の検索も）Cedar が拒否する", res is None, err or departments_of(res))
        _, res, err = mcp_retrieve(tok[LEGAL], q)
        c.check("（同じ故障のまま）本人と一致すれば通る", res is not None and departments_of(res) == {"legal"},
                err or departments_of(res))
    finally:
        set_fault("")
        print("  Interceptor の故障を戻した", flush=True)
    time.sleep(5)
    _, res, err = mcp_retrieve(tok[SALES], q)
    c.check("故障を戻すと営業も検索できる", res is not None and departments_of(res) == {"sales"},
            err or departments_of(res))

    # ---- ④KB のリソースポリシー: Gateway 以外から直接 Retrieve ----
    for label, extra in (("", {}), ("（userContext 付き）", {"userContext": {"userId": LEGAL}})):
        try:
            agent_rt.retrieve(knowledgeBaseId=OUT["KnowledgeBaseId"], retrievalQuery={"text": q}, **extra)
            c.check(f"管理者の認証情報でも直接 Retrieve できない{label}", False, "通ってしまった")
        except ClientError as e:
            c.check(f"管理者の認証情報でも直接 Retrieve できない{label}",
                    e.response["Error"]["Code"] == "AccessDeniedException", e.response["Error"]["Code"])

    # ---- エージェント（画面のチャット） ----
    status, r = chat(tok[SALES], "反社会的勢力の排除条項がある契約書はありますか？")
    depts = {s["department"] for s in r.get("sources", [])}
    c.check("チャット: 営業の人に法務部の文書が出ない", status == 200 and "法務部" not in depts, (status, depts))
    status, r = chat(tok[LEGAL], "反社会的勢力の排除条項がある契約書はありますか？")
    c.check("チャット: 法務の人は契約書を元ファイル付きで答えられる",
            status == 200 and any(s["department"] == "法務部" and s["url"] for s in r.get("sources", [])),
            (status, [s["fileName"] for s in r.get("sources", [])]))
    print("  回答:", r.get("answer", "")[:200].replace("\n", " "))
    status, r = chat(tok[BOTH], "反社会的勢力の排除条項と、見積書の合計金額", departments=["sales"])
    depts = {s["department"] for s in r.get("sources", [])}
    c.check("チャット: 兼務の人が営業部だけを選ぶと、元ファイルは営業部だけ", status == 200 and depts == {"営業部"},
            (status, depts))
    status, r = chat(tok[SALES], "見積書の合計金額（税込）はいくら？")
    c.check("チャット: 営業の人は見積書を答えられる",
            status == 200 and any(s["department"] == "営業部" for s in r.get("sources", [])),
            (status, [s["fileName"] for s in r.get("sources", [])]))
    print("  回答:", r.get("answer", "")[:200].replace("\n", " "))

    # ---- 名前の変更（Storage Browser ではコピーと削除）: 古い書き起こしが消え、新しく作られる ----
    old_key = f"sales/確認用/{DOCS['sales'][1]}"
    new_key = f"sales/確認用/名前変更後-{DOCS['sales'][1]}"
    sales_s3 = storage(tok[SALES], "sales")
    sales_s3.copy_object(Bucket=fb, Key=new_key, CopySource={"Bucket": fb, "Key": old_key})
    sales_s3.delete_object(Bucket=fb, Key=old_key)
    for _ in range(20):
        time.sleep(6)
        old_gone = denied_or_missing(s3, text_key(old_key))
        new_made = not denied_or_missing(s3, text_key(new_key) + ".metadata.json")
        if old_gone and new_made:
            break
    c.check("名前を変えると、古い書き起こしが消えて新しく作られる", old_gone and new_made, (old_gone, new_made))
    sales_s3.copy_object(Bucket=fb, Key=old_key, CopySource={"Bucket": fb, "Key": new_key})  # 元に戻す
    sales_s3.delete_object(Bucket=fb, Key=new_key)

    sys.exit(c.summary())


def denied_or_missing(s3, key) -> bool:
    try:
        s3.head_object(Bucket=OUT["DocsBucket"], Key=key)
        return False
    except ClientError:
        return True


if __name__ == "__main__":
    main()
