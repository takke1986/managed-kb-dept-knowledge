"""タグ（フォルダ名＋一括で足したタグ）の確認。scripts/verify.py のあとに流す。

- フォルダ名がタグになる（部署より下のすべての階層）
- 選んだファイルに、部署のタグの一覧から一括で足す・外すことができ、足したタグで検索を絞り込める
- フォルダを移す（コピーと削除）と、フォルダ名のタグは新しい場所のものになり、一括で足したタグは引き継がれ、
  書き起こしは使い回す（生成AIを呼ばない）
- 一覧に無いタグは付けられない。他部署の人は付けられない。管理者でない人は一覧を変えられない
- 足す・移す・外すのあと、KB に取り込まれるのを待ち、変更後のタグで検索に出て変更前のタグでは出ないことを確かめる
終わったら置いたものを消し、タグの一覧を元に戻す。

    .venv/bin/python scripts/verify_tags.py
"""

import json
from urllib.parse import quote
import sys
import time

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

c = v.Checks()
fb = v.OUT["FilesBucket"]
s3 = v.boto3.client("s3", region_name=v.REGION)
logs = v.boto3.client("logs", region_name=v.REGION)
sales_tok, admin_tok = v.token(v.SALES), v.token(v.BOTH)
sales = v.storage(sales_tok, "sales")
src = v.OFFICE_DIR / "提案書-グラフとフロー.pptx"
key_a = "sales/確認用/タグ/契約/2026/提案書A.pptx"
key_b = "sales/確認用/タグ/契約/2026/見積B.xlsx"
key_moved = "sales/確認用/タグ/規程/提案書A.pptx"


def metadata(key):
    try:
        return json.loads(s3.get_object(Bucket=v.OUT["DocsBucket"], Key=v.text_key(key) + ".metadata.json")["Body"].read())["metadataAttributes"]
    except s3.exceptions.NoSuchKey:
        return None


def wait_meta(key, check=lambda m: True, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        m = metadata(key)
        if m and check(m):
            return m
        time.sleep(5)
    return metadata(key)


def retrieve_names(tag):
    return v.keys_with_tag(sales_tok, tag)


TAG = "重要"  # この確かめのために足し、最後に文書から外して消す（部署の本物の一覧は変えない）


def drop_tag():
    _, lst = v.api("GET", "/api/tags?department=sales", admin_tok)
    for t in lst.get("tags", []):
        if t["name"] == TAG:
            v.api("DELETE", f"/api/tags?department=sales&name={quote(TAG)}&version={t['version']}&detach=1", admin_tok)


drop_tag()  # 前の回の残り
try:
    status, _ = v.api("POST", "/api/tags", sales_tok, {"department": "sales", "name": TAG})
    c.check("管理者でない人はタグを足せない", status == 403, status)
    status, _ = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "color": "red"})
    c.check("管理者はタグを足せる", status == 200, status)

    sales.put_object(Bucket=fb, Key=key_a, Body=src.read_bytes())
    sales.put_object(Bucket=fb, Key=key_b, Body=(v.OFFICE_DIR / "売上集計-グラフ付き.xlsx").read_bytes())
    ma, _ = wait_meta(key_a), wait_meta(key_b)
    c.check("フォルダ名がタグになる（部署より下のすべての階層）",
            ma and ma.get("folder_tags") == ["確認用", "タグ", "契約", "2026"], ma and ma.get("folder_tags"))

    status, _ = v.api("PUT", "/api/files/tags", sales_tok, {"keys": [key_a], "add": ["存在しないタグ"]})
    c.check("一覧に無いタグは付けられない", status == 400, status)
    status, _ = v.api("PUT", "/api/files/tags", v.token(v.LEGAL), {"keys": [key_a], "add": ["重要"]})
    c.check("他部署の人は付けられない", status == 403, status)
    status, r = v.api("PUT", "/api/files/tags", sales_tok, {"keys": [key_a, key_b], "add": ["重要"]})
    c.check("選んだ2件に一括で足せる", status == 200 and len(r.get("updated", [])) == 2, (status, r))
    ma = metadata(key_a)
    c.check("足したタグとフォルダ名のタグが合わさる",
            ma["tags"] == ["確認用", "タグ", "契約", "2026", "重要"] and ma["extra_tags"] == ["重要"], ma["tags"])

    v.wait_ingested([])
    names = v.settle(lambda: retrieve_names("重要"), lambda n: n == {key_a, key_b})
    c.check("足したタグで検索を絞り込める", names == {key_a, key_b}, names)

    # フォルダを移す（Storage Browser の移動と同じく、コピーと削除）
    moved_at = int(time.time() * 1000)
    sales.copy_object(Bucket=fb, Key=key_moved, CopySource={"Bucket": fb, "Key": key_a})
    sales.delete_object(Bucket=fb, Key=key_a)
    mm = wait_meta(key_moved)
    c.check("移すと、フォルダ名のタグは新しい場所のものになる",
            mm and mm.get("folder_tags") == ["確認用", "タグ", "規程"], mm and mm.get("folder_tags"))
    c.check("移しても、一括で足したタグは引き継がれる", mm and mm.get("extra_tags") == ["重要"], mm and mm.get("extra_tags"))
    time.sleep(10)
    events = logs.filter_log_events(logGroupName=v.OUT["ConvertLogGroup"], startTime=moved_at,
                                    filterPattern='"書き起こしを使い回した"')["events"]
    c.check("移しても書き起こしは使い回す（生成AIを呼ばない）", any("提案書A.pptx" in e["message"] for e in events),
            len(events))
    old_gone = wait_meta(key_a, timeout=5) is None
    c.check("移す前の場所の書き起こしは消える", old_gone)
    v.wait_ingested([key_moved])
    moved = v.settle(lambda: (retrieve_names("規程"), retrieve_names("契約")),
                     lambda r: key_moved in r[0] and key_moved not in r[1] and key_a not in r[1])
    c.check("移したあと: 新しいフォルダ名のタグで検索に出る", key_moved in moved[0], moved[0])
    c.check("移したあと: 前のフォルダ名のタグでは出ない（前の場所の文書も出ない）",
            key_moved not in moved[1] and key_a not in moved[1], moved[1])
    names = v.settle(lambda: retrieve_names("重要"), lambda n: n == {key_moved, key_b})
    c.check("移したあと: 引き継いだタグで検索に出る", names == {key_moved, key_b}, names)

    status, r = v.api("PUT", "/api/files/tags", sales_tok, {"keys": [key_moved, key_b], "remove": ["重要"]})
    c.check("一括で外せる", status == 200 and metadata(key_b).get("extra_tags") is None, metadata(key_b).get("tags"))
    v.wait_ingested([])
    names = v.settle(lambda: retrieve_names("重要"), lambda n: not n & {key_moved, key_b})
    c.check("外したあと: 外したタグでは検索に出ない", not names & {key_moved, key_b}, names)
    names = v.settle(lambda: retrieve_names("タグ"), lambda n: {key_moved, key_b} <= n)
    c.check("外したあと: ほかのタグ（フォルダ名）では出る（文書が落ちていない）", {key_moved, key_b} <= names, names)
finally:
    for k in (key_a, key_b, key_moved):
        sales.delete_object(Bucket=fb, Key=k)
    drop_tag()
    print("  置いたものと、足したタグを消した", flush=True)
sys.exit(c.summary())
