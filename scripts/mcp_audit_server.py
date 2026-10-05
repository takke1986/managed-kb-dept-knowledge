"""KB 取り込み監査を MCP（stdio）のツールとして公開する。

Kiro のエージェントが「営業部の取り込みを監査して」と頼まれたときに、シェルを
自由に使わせずに監査だけを実行できるようにするためのもの。依存を増やさないよう、
MCP の JSON-RPC を標準ライブラリだけで処理する。

公開するツール:
  list_departments  config/app.json に定義された部署を返す
  audit_department  scripts/audit_ingestion.py を実行し、結果の要約を返す

監査は読み取りのみ（要件4.4）。チャンク本文は返さず、差分スパンだけを返す。
他部署のチャンクの本文は監査側でも出さない（要件5.2）。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from project import CONFIG  # noqa: E402

# 監査 CLI と同じく config/app.json を正とする
DEPARTMENTS: dict[str, str] = {d["id"]: d["name"] for d in CONFIG["departments"]}

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "list_departments",
        "description": "監査できる部署（config/app.json の定義）を返す。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "audit_department",
        "description": (
            "指定した部署の KB チャンクが原文の Markdown と一字一句一致するかを監査する。"
            "読み取りのみ。verbatim/altered/skipped/unmatched の件数と、altered の差分を返す。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "department": {"type": "string", "description": "部署キー（例: sales）"},
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "検索に使う問い合わせ。省略時は文書名と見出しから作る。",
                },
            },
            "required": ["department"],
        },
    },
]


def audit(department: str, queries: list[str] | None = None) -> dict:
    """監査 CLI を子プロセスで動かす。CLI と同じ経路（Gateway・部署メンバー）を通すため。"""
    if department not in DEPARTMENTS:
        # AWS に触れる前に断る（要件5.3）
        return {"error": f"未定義の部署: {department}", "departments": sorted(DEPARTMENTS)}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "audit.json"
        cmd = [sys.executable, str(ROOT / "scripts" / "audit_ingestion.py"), department, "--json", str(out)]
        for q in queries or []:
            cmd += ["--query", q]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        results = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {
        "department": department,
        "exit_code": proc.returncode,
        "audited": len(results),
        "counts": counts,
        # 本文は返さない。altered の差分スパンと文書キーだけを返す
        "altered": [
            {"source_key": r.get("source_key"), "spans": r.get("spans", [])}
            for r in results
            if r["verdict"] == "altered"
        ],
        "stderr": proc.stderr[-2000:],
    }


def call_tool(name: str, args: dict) -> dict:
    if name == "list_departments":
        payload = DEPARTMENTS
    elif name == "audit_department":
        payload = audit(args["department"], args.get("queries"))
    else:
        raise ValueError(f"unknown tool: {name}")
    is_error = "error" in payload or payload.get("exit_code", 0) not in (0, None)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # 通知には応答しない
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kb-audit", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params", {})
            result = call_tool(p.get("name"), p.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # ツールの失敗はエージェントに返して判断させる
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
