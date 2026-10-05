"""KB 取り込み監査。部署のメンバーとして KB を検索し、返ったチャンクが渡した Markdown のままかを確かめる。

    uv run python scripts/audit_ingestion.py <部署> [--query 文 ...] [--json 出力先] [--user メールアドレス]

    <部署>    config/app.json の部署 ID（sales・legal など）
    --query   検索に使う文。省略すると、その部署の Markdown のファイル名と見出しから作る
    --json    チャンクごとの結果を書き出す
    --user    検索するメンバー。省略すると、確認用アカウントのうちその部署の人

終了コード: 0 すべて原文どおり / 1 書き換えか照合できないものがある / 2 設定の誤り

AWS には読み取りしかしない（Cognito のサインイン、Gateway の Retrieve、S3 の GetObject・List）。
仕様は .kiro/specs/kb-ingestion-audit/。
"""

import argparse
import json
import sys
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kbaudit.matching import Verdict  # noqa: E402
from kbaudit.report import ISOLATION, failed, run_audit, summarise  # noqa: E402
from project import CONFIG  # noqa: E402

DEPARTMENTS = {d["id"] for d in CONFIG["departments"]}
MAX_DEFAULT_QUERIES = 40


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KB 取り込み監査")
    p.add_argument("department")
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--json", dest="json_path")
    p.add_argument("--user")
    return p.parse_args(argv)


# ---- AWS（読み取りだけ） -------------------------------------------------------------


def _aws():
    import boto3

    from project import REGION, accounts, outputs

    out = outputs()
    return boto3, REGION, out, accounts


def sign_in(boto3, region, out, email: str, password: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=region)
    r = cognito.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=out["UserPoolClientId"],
                              AuthParameters={"USERNAME": email, "PASSWORD": password})
    return r["AuthenticationResult"]["AccessToken"]


def pick_member(boto3, region, out, department: str, known: dict[str, str]) -> str:
    """確認用アカウントのうち、その部署のグループに入っている人。兼務の人は避ける。"""
    cognito = boto3.client("cognito-idp", region_name=region)
    members = []
    for page in cognito.get_paginator("list_users_in_group").paginate(UserPoolId=out["UserPoolId"], GroupName=department):
        for user in page["Users"]:
            email = next((a["Value"] for a in user["Attributes"] if a["Name"] == "email"), user["Username"])
            if email in known:
                members.append(email)
    if not members:
        raise SystemExit(f"{department} のメンバーが確認用アカウントにいません。--user で指定してください")

    def group_count(email: str) -> int:
        return len(cognito.admin_list_groups_for_user(UserPoolId=out["UserPoolId"], Username=email)["Groups"])

    return min(members, key=group_count)


def make_retrieve(out, token: str, department: str) -> Callable[[str], list[dict]]:
    """Gateway の kb___Retrieve を部署で絞って呼ぶ。KB は Gateway 以外からの Retrieve を拒否する。"""

    def retrieve(query: str) -> list[dict]:
        args = {"retrievalQuery": {"text": query},
                "retrievalConfiguration": {"managedSearchConfiguration": {
                    "filter": {"equals": {"key": "department", "value": department}}}}}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "kb___Retrieve", "arguments": args}}).encode("utf-8")
        req = urllib.request.Request(out["GatewayUrl"], data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
        if "data:" in text and not text.lstrip().startswith("{"):
            text = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")][-1]
        msg = json.loads(text)
        result = msg.get("result") or {}
        if msg.get("error") or result.get("isError"):
            raise RuntimeError(json.dumps(msg, ensure_ascii=False)[:300])
        payload = json.loads(result["content"][0]["text"])
        return payload.get("retrievalResults") or []

    return retrieve


def make_read_source(s3, bucket: str) -> Callable[[str], str | None]:
    def read_source(key: str) -> str | None:
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        except Exception:  # noqa: BLE001  読めなければ UNMATCHED として報告する
            return None

    return read_source


def default_queries(s3, bucket: str, department: str) -> list[str]:
    """部署の Markdown のファイル名と見出しを問い合わせにする。どの文書にも検索が届くように。"""
    queries: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"kb-source/{department}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".md"):
                continue
            queries.append(key.rsplit("/", 1)[-1].removesuffix(".md").rsplit(".", 1)[0])
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            queries.extend(line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#"))
    unique = list(dict.fromkeys(q for q in queries if q))
    return unique[:MAX_DEFAULT_QUERIES]


# ---- 表示 -------------------------------------------------------------------------


def report(results, out=sys.stdout) -> None:
    for r in results:
        if r.verdict is Verdict.ALTERED:
            print(f"ALTERED   {r.source_key}", file=out)
            for span in r.spans:
                print(f"          {span.source!r} -> {span.chunk!r}", file=out)
        elif r.verdict is Verdict.UNMATCHED:
            # 他部署のチャンクは本文も差分も出さない（要件5.2）
            label = "ISOLATION" if r.reason == ISOLATION else "UNMATCHED"
            print(f"{label:<9} {r.source_key or '?'}  {r.reason}", file=out)
    s = summarise(results)
    print(f"\naudited {s['audited']}  verbatim {s['verbatim']}  altered {s['altered']}  "
          f"skipped {s['skipped']}  unmatched {s['unmatched']}", file=out)


def write_json(results, path: str) -> None:
    rows = [{**asdict(r), "verdict": r.verdict.value, "spans": [asdict(s) for s in r.spans]} for r in results]
    Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None, *, aws: Callable | None = None,
         retrieve=None, read_source=None, queries: Iterable[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # 部署の確認は AWS に触れる前に済ませる（要件5.3）
    if args.department not in DEPARTMENTS:
        print(f"未定義の部署: {args.department}（config/app.json: {', '.join(sorted(DEPARTMENTS))}）", file=sys.stderr)
        return 2

    if retrieve is None or read_source is None:
        boto3, region, out, accounts = (aws or _aws)()
        known = accounts()
        email = args.user or pick_member(boto3, region, out, args.department, known)
        token = sign_in(boto3, region, out, email, known[email])
        s3 = boto3.client("s3", region_name=region)
        retrieve = make_retrieve(out, token, args.department)
        read_source = make_read_source(s3, out["DocsBucket"])
        if queries is None:
            queries = args.query or default_queries(s3, out["DocsBucket"], args.department)
        print(f"{args.department} を {email} として監査（問い合わせ {len(list(queries))} 件）")
    queries = list(queries if queries is not None else args.query)

    errors: list[str] = []

    def safe_retrieve(q: str) -> list[dict]:
        try:
            return list(retrieve(q))
        except Exception as e:  # noqa: BLE001  1件の失敗で全体を止めない
            errors.append(f"{q}: {e}")
            return []

    results = run_audit(args.department, safe_retrieve, read_source, queries)
    report(results)
    for e in errors:
        print(f"RETRIEVE ERROR {e}", file=sys.stderr)
    if args.json_path:
        write_json(results, args.json_path)
    return 1 if failed(results) or errors else 0


if __name__ == "__main__":
    sys.exit(main())
