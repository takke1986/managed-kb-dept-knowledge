"""タグの一覧の管理（DynamoDB）の確認。scripts/verify.py のあとに流す。

- 最初の一覧（app.json の tags とすでに付いているタグ）が入っていて、一覧に無いタグが文書に残っていない
- 管理者だけが足す・直す・並べ替える・消すことができる。部署の人は見られる、他部署の人は見られない
- 名前の決まり（長さ・重複）、同時に直したときは後の保存を断る（version）
- アーカイブしたタグは一括で足せない（付いている分は残る）。戻せば足せる
- 付いている文書があるタグは、detach=1 のときだけ文書から外して消す（KB のメタデータからも消える）
- 説明がエージェントのタグの一覧に入る（質問の記録に残る絞り込みと、説明にだけ書いた言葉を答えられるかで確かめる）
- アーカイブしたタグでも検索で絞れる。消したタグでは出ず、ほかのタグでは出る（KB に取り込まれるのを待って確かめる）
終わったら足したタグを消す（部署の本物の一覧は変えない）。

    .venv/bin/python scripts/verify_tag_admin.py
"""

import json
import sys
import time
import uuid
from urllib.parse import quote

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

c = v.Checks()
s3 = v.boto3.client("s3", region_name=v.REGION)
ddb = v.boto3.resource("dynamodb", region_name=v.REGION).Table(v.OUT["TagsTable"])
docs_table = v.boto3.resource("dynamodb", region_name=v.REGION).Table(v.OUT["DocumentsTable"])
sales_tok, legal_tok, admin_tok = v.token(v.SALES), v.token(v.LEGAL), v.token(v.BOTH)
RUN = uuid.uuid4().hex[:6]
TAG = f"確認{RUN}"
TARGET = "sales/確認用/見積-整合.xlsx"  # verify.py が置く、取り込み済みの文書
WORD = f"合言葉{RUN[::-1]}"  # 説明にだけ書く言葉。エージェントがこれを言えれば、説明が渡っている
DESCRIPTION = f"確かめのためのタグ。見積の整合を見たもの（{WORD}）"


def tags(tok=admin_tok, dept="sales"):
    return v.api("GET", f"/api/tags?department={dept}", tok)


def find(name):
    return next((t for t in tags()[1]["tags"] if t["name"] == name), None)


def delete(name, detach=False):
    t = find(name)
    if not t:
        return 404, {}
    return v.api("DELETE", f"/api/tags?department=sales&name={quote(name)}&version={t['version']}"
                           + ("&detach=1" if detach else ""), admin_tok)


def extra_tags_in_kb(key):
    item = docs_table.get_item(Key={"department": "sales", "key": key}).get("Item") or {}
    meta = json.loads(s3.get_object(Bucket=v.OUT["DocsBucket"], Key=v.text_key(key) + ".metadata.json")["Body"].read())
    return item.get("extraTags", []), meta["metadataAttributes"].get("extra_tags", [])


try:
    # 最初の一覧
    status, lst = tags()
    names = [t["name"] for t in lst.get("tags", [])]
    c.check("部署のタグの一覧が読める", status == 200 and bool(names), (status, names))
    c.check("すでに付いているタグは一覧に無いものとして出ない", lst.get("unlisted") == [], lst.get("unlisted"))
    seeded = ddb.get_item(Key={"department": "sales", "name": "#seeded"}).get("Item")
    c.check("最初の一覧を入れたしるしが残る（全部消しても入れ直さない）", bool(seeded), seeded)

    # 見られる人
    c.check("部署の人は一覧を見られる", tags(sales_tok)[0] == 200, tags(sales_tok)[0])
    c.check("他部署の人は見られない", tags(legal_tok)[0] == 403, tags(legal_tok)[0])

    # 足す
    status, _ = v.api("POST", "/api/tags", sales_tok, {"department": "sales", "name": TAG})
    c.check("管理者でない人は足せない", status == 403, status)
    status, t = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "color": "orange",
                                                       "description": DESCRIPTION})
    c.check("管理者は説明と色つきで足せる", status == 200 and t.get("color") == "orange" and t.get("version") == 1, (status, t))
    status, _ = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": TAG})
    c.check("同じ名前は足せない（409）", status == 409, status)
    status, _ = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": "あ" * 21})
    c.check("21文字の名前は断る", status == 400, status)
    status, _ = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": "a/b"})
    c.check("/ を含む名前は断る", status == 400, status)

    # 直す
    status, t2 = v.api("PUT", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "version": 1, "color": "blue"})
    c.check("色を直せる（版が進む）", status == 200 and t2.get("color") == "blue" and t2.get("version") == 2, (status, t2))
    status, _ = v.api("PUT", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "version": 1, "color": "red"})
    c.check("古い版での保存は断る（409）", status == 409, status)
    status, _ = v.api("PUT", "/api/tags", sales_tok, {"department": "sales", "name": TAG, "version": 2, "color": "red"})
    c.check("管理者でない人は直せない", status == 403, status)

    # 並べ替える
    order = [TAG] + [n for n in names if n != TAG]
    status, _ = v.api("PUT", "/api/tags/order", admin_tok, {"department": "sales", "names": order})
    c.check("並べ替えられる", status == 200 and tags()[1]["tags"][0]["name"] == TAG, [x["name"] for x in tags()[1]["tags"]][:3])

    # 付ける・アーカイブ
    status, r = v.api("PUT", "/api/files/tags", sales_tok, {"keys": [TARGET], "add": [TAG]})
    c.check("足したタグを文書に付けられる", status == 200 and r.get("updated") == [TARGET], (status, r))
    c.check("付いている件数が数えられる", find(TAG)["count"] == 1, find(TAG))
    t = find(TAG)
    status, _ = v.api("PUT", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "version": t["version"], "archived": True})
    status2, _ = v.api("PUT", "/api/files/tags", sales_tok, {"keys": [TARGET], "add": [TAG]})
    c.check("アーカイブしたタグは一括で足せない", status == 200 and status2 == 400, (status, status2))
    c.check("アーカイブしても付いている分は残る", TAG in extra_tags_in_kb(TARGET)[1], extra_tags_in_kb(TARGET))
    v.wait_ingested([])
    found = v.settle(lambda: v.keys_with_tag(sales_tok, TAG, "見積番号"), lambda k: TARGET in k)
    c.check("アーカイブしたタグでも検索で絞り込める（KB に入っている）", TARGET in found, found)
    _, me = v.api("GET", "/api/me", sales_tok)
    sales_me = next(d for d in me["departments"] if d["id"] == "sales")
    c.check("アーカイブしたタグは候補に出ないが、色と説明は画面に渡る",
            TAG not in sales_me["tags"] and any(d["name"] == TAG and d["archived"] for d in sales_me["tagDefs"]),
            sales_me["tags"])
    t = find(TAG)
    status, _ = v.api("PUT", "/api/tags", admin_tok, {"department": "sales", "name": TAG, "version": t["version"], "archived": False})
    c.check("アーカイブから戻せる", status == 200 and not find(TAG)["archived"], status)

    # 説明がエージェントに渡る
    for _ in range(4):  # タグの一覧は API が1分控えるので、足した直後は入っていないことがある
        _, r = v.chat(sales_tok, f"「{TAG}」のタグが付いた文書の見積番号は？", departments=["sales"])
        used = json.dumps(r.get("searches", []), ensure_ascii=False)
        if TAG in used:
            break
        time.sleep(30)
    c.check("エージェントが足したタグで絞り込める（タグの一覧に入っている）", TAG in used, used[:300])
    for _ in range(3):
        _, r = v.chat(sales_tok, f"「{TAG}」というタグには、どんな説明が付いていますか。文書を検索せず、タグの一覧に書かれた説明をそのまま答えてください。",
                      departments=["sales"])
        if WORD in r.get("answer", ""):
            break
        time.sleep(30)
    c.check("タグの説明がエージェントに渡っている", WORD in r.get("answer", ""), r.get("answer", "")[:200])

    # 消す
    status, r = delete(TAG)
    c.check("付いている文書があれば、そのままでは消せない（409・件数つき）", status == 409 and "1件" in r.get("message", ""), (status, r))
    status, r = delete(TAG, detach=True)
    c.check("detach=1 なら文書から外して消せる", status == 200 and r.get("detached") == 1 and find(TAG) is None, (status, r))
    ledger_tags, kb_tags = extra_tags_in_kb(TARGET)
    c.check("文書の台帳と KB のメタデータからも外れる", TAG not in ledger_tags and TAG not in kb_tags, (ledger_tags, kb_tags))
    v.wait_ingested([])
    found = v.settle(lambda: v.keys_with_tag(sales_tok, TAG, "見積番号"), lambda k: TARGET not in k)
    c.check("消したあと: 消したタグでは検索に出ない", TARGET not in found, found)
    found = v.settle(lambda: v.keys_with_tag(sales_tok, "2026年度", "見積番号"), lambda k: TARGET in k)
    c.check("消したあと: ほかのタグでは出る（文書が落ちていない）", TARGET in found, found)
    status, _ = v.api("POST", "/api/tags", admin_tok, {"department": "sales", "name": TAG})
    status2, _ = delete(TAG)
    c.check("付いていないタグはそのまま消せる", status == 200 and status2 == 200, (status, status2))
finally:
    if find(TAG):
        delete(TAG, detach=True)
    v.api("PUT", "/api/tags/order", admin_tok, {"department": "sales", "names": [n for n in names if n != TAG]})
    v.wait_ingested([])
    print("  足したタグを消し、並び順を元に戻した", flush=True)
sys.exit(c.summary())
