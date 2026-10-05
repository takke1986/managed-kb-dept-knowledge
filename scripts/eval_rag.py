"""答えの質を測る。scripts/eval_cases.py のケースを、画面と同じ API（/api/chat）で聞いて判定する。

    .venv/bin/python scripts/eval_rag.py setup   # 評価用の文書（モデル就業規則）を法務部に置いて取り込む
    .venv/bin/python scripts/eval_rag.py         # 評価する。結果は eval/results/<日時>.json

判定:
    到達   検索で返った元ファイルに、期待する文書が入っているか（答えるべきケースだけ）
    正答   答えに期待する文字列がすべて入っているか
    控え   文書に無いこと・他部署のことを聞かれて、控える言い回しで答えたか
    漏れ   他部署の中身（forbid）が答えに出たか
生成AIを呼ぶので費用がかかる（全ケースで数十円程度）。
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

args = sys.argv[1:]
sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402
from eval_cases import CASES  # noqa: E402

RULES_SRC = v.TESTDATA / "rules"  # 厚生労働省のモデル就業規則（testdata/README.md）
RULES = {"モデル就業規則.docx": "legal/評価/モデル就業規則.docx",
         "モデル就業規則.pdf": "legal/評価/モデル就業規則.pdf"}
FOLDER_COPY = "legal/規程/人事/モデル就業規則.docx"  # フォルダのタグで絞る問題のため（同じ中身なので書き起こしは使い回す）
EXTRA_TAG = ("sales/確認用/見積-整合.xlsx", "2026年度")  # 追加のタグで絞る問題のため
USERS = {"sales": v.SALES, "legal": v.LEGAL, "both": v.BOTH}
ABSTAIN = re.compile(r"見つかりませんでした|見つかりません|記載がありません|記載はありません|含まれていません|"
                     r"確認できません|確認できませんでした|情報がありません|ありませんでした")

_ZEN = "０１２３４５６７８９"
_KAN = {"〇": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def fold(s: str) -> str:
    """表記を寄せる。全角・漢数字の数字、空白、全角のカンマを揃える。"""
    out = []
    for ch in s:
        if ch in _ZEN:
            out.append(str(_ZEN.index(ch)))
        elif ch in _KAN:
            out.append(_KAN[ch])
        elif ch in "，":
            out.append(",")
        elif not ch.isspace():
            out.append(ch)
    return "".join(out)


def setup():
    tok = v.token(v.LEGAL)
    legal = v.storage(tok, "legal")
    for src, key in RULES.items():
        legal.put_object(Bucket=v.OUT["FilesBucket"], Key=key, Body=(RULES_SRC / src).read_bytes())
    legal.put_object(Bucket=v.OUT["FilesBucket"], Key=FOLDER_COPY, Body=(RULES_SRC / "モデル就業規則.docx").read_bytes())
    print("置いた。書き起こしと取り込みを待つ（PDF は約100ページを生成AIが読むので数分かかる）", flush=True)
    v.wait_ingested(list(RULES.values()) + [FOLDER_COPY], timeout=2400)
    status, r = v.api("PUT", "/api/files/tags", v.token(v.SALES), {"keys": [EXTRA_TAG[0]], "add": [EXTRA_TAG[1]]})
    print(f"  追加のタグ「{EXTRA_TAG[1]}」を付けた: {status} {r}")
    v.wait_ingested([])
    _, files = v.api("GET", "/api/files?department=legal", tok)
    for f in files["files"]:
        if f["key"] in RULES.values():
            print(f"  {f['fileName']}: {f['status']} {f.get('reason', '')}")


def run_case(case, tokens):
    status, r = v.chat(tokens[case["user"]], case["q"])
    answer = r.get("answer", "") if status == 200 else f"（エラー {status}: {r}）"
    filters = json.dumps([x.get("filter") for x in r.get("searches", []) if x.get("filter")], ensure_ascii=False)
    sources = [s["fileName"] for s in r.get("sources", [])] if status == 200 else []
    folded = fold(answer)
    res = {"id": case["id"], "kind": case["kind"], "user": case["user"], "q": case["q"],
           "answer": answer, "sources": sources, "searches": r.get("searches", []), "filtered": filters != "[]"}
    if case["kind"] == "answer":
        res["reached"] = any(s in sources for s in case["source"])
        res["missing"] = [e for e in case["expect"] if fold(e) not in folded]
        res["ok"] = res["reached"] and not res["missing"]
        if case.get("filter"):  # タグで絞り込むべき問題は、期待するタグで絞ったかも見る
            res["filter_used"] = any(t in filters for t in case["filter"])
            res["ok"] = res["ok"] and res["filter_used"]
        if case.get("no_filter"):  # 絞り込むべきでない問題は、そのタグで絞っていないかを見る（行き過ぎ）
            res["over_filtered"] = [t for t in case["no_filter"] if t in filters]
            res["ok"] = res["ok"] and not res["over_filtered"]
    else:
        res["abstained"] = bool(ABSTAIN.search(answer))
        res["leaked"] = [f for f in case.get("forbid", []) if fold(f) in folded]
        res["ok"] = res["abstained"] and not res["leaked"]
    return res


def evaluate():
    tokens = {u: v.token(email) for u, email in USERS.items()}
    started = time.time()
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda c: run_case(c, tokens), CASES))
    for r in results:
        mark = "OK" if r["ok"] else "NG"
        detail = (f"到達={'○' if r['reached'] else '×'} 欠け={r['missing']}" if r["kind"] == "answer"
                  else f"控え={'○' if r['abstained'] else '×'} 漏れ={r['leaked']}")
        if "filter_used" in r:
            detail += f" 絞り込み={'○' if r['filter_used'] else '×'}"
        elif r.get("over_filtered"):
            detail += f" 行き過ぎた絞り込み={r['over_filtered']}"
        elif r["filtered"]:
            detail += " （絞り込みあり）"
        print(f"{mark}  {r['id']:28} {detail}")
        if not r["ok"]:
            print(f"      答え: {r['answer'][:160].replace(chr(10), ' ')}")
            print(f"      元ファイル: {r['sources']}")
    answer = [r for r in results if r["kind"] == "answer"]
    other = [r for r in results if r["kind"] != "answer"]
    summary = {
        "正答": f"{sum(r['ok'] for r in answer)}/{len(answer)}",
        "到達": f"{sum(r['reached'] for r in answer)}/{len(answer)}",
        "控え・漏れなし": f"{sum(r['ok'] for r in other)}/{len(other)}",
        "絞り込みを使った問題": f"{sum(r['filtered'] for r in results)}/{len(results)}",
        "秒": round(time.time() - started),
    }
    print("\n", summary)
    out = v.ROOT / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    print("結果:", path.relative_to(v.ROOT))
    return all(r["ok"] for r in results)


if __name__ == "__main__":
    if args[:1] == ["setup"]:
        setup()
    else:
        sys.exit(0 if evaluate() else 1)
