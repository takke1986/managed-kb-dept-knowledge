"""AgentCore Gateway の Request Interceptor。

KB の検索（Retrieve・AgenticRetrieveStream）の引数 userContext に、JWT から取り出したメールアドレスを入れる。
エージェント（モデル）が作った userContext は必ず上書きする。モデルに認可の値を作らせると、
プロンプトインジェクションやハルシネーションで他人になりすませるため。

JWT は Gateway が受け付けた時点で検証済みだが、ここでも検証する。検証できなければ
userContext を消す。その場合 Cedar のポリシーが拒否し、KB も ACL で何も返さない。
"""

import logging
import os

from auth import Unauthorized, bearer_token, verify

log = logging.getLogger()
log.setLevel(logging.INFO)

# userContext を入れる（＝KB を検索する）ツール
RETRIEVE_TOOL_SUFFIXES = ("___Retrieve", "___AgenticRetrieveStream")
# 確認用: Interceptor が壊れて別人を入れたときに Cedar が拒否することを確かめる。
# scripts/verify.py が一時的に設定し、終わったら必ず消す。普段は設定しない
FAULT_USER_ID = os.environ.get("FAULT_INJECT_USER_ID", "")


def handler(event, context):
    request = event.get("mcp", {}).get("gatewayRequest", {})
    body = request.get("body") or {}
    if body.get("method") == "tools/call":
        params = body.setdefault("params", {})
        arguments = params.setdefault("arguments", {})
        claimed = arguments.pop("userContext", None)
        if str(params.get("name", "")).endswith(RETRIEVE_TOOL_SUFFIXES):
            try:
                user = verify(bearer_token(request.get("headers", {})))
                arguments["userContext"] = {"userId": user.email}
                if FAULT_USER_ID:
                    log.warning("確認用の故障: userContext を %s にする", FAULT_USER_ID)
                    arguments["userContext"] = {"userId": FAULT_USER_ID}
                scope = search_scope(request.get("headers", {}))
                if scope and str(params.get("name", "")).endswith("___Retrieve"):
                    narrow_to_departments(arguments, scope)
                if claimed and claimed != arguments["userContext"]:
                    log.warning("エージェントが別の userContext を渡したので上書きした: %s", claimed)
                log.info("userContext を入れた: tool=%s", params.get("name"))
            except Unauthorized as e:
                log.warning("userContext を入れずに通す（拒否される）: %s", e)
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}


def search_scope(headers: dict) -> list[str]:
    """兼務の人が選んだ、検索する部署（x-search-departments）。使い勝手のためで、部署の分離は ACL が守る。"""
    value = next((v for k, v in (headers or {}).items() if k.lower() == "x-search-departments"), "")
    return [d for d in value.split(",") if d]


def narrow_to_departments(arguments: dict, departments: list[str]) -> None:
    """検索の条件に「部署がそのどれか」を足す。エージェントのタグでの絞り込みはそのまま残す。"""
    config = arguments.setdefault("retrievalConfiguration", {}).setdefault("managedSearchConfiguration", {})
    scope = {"in": {"key": "department", "value": departments}}
    config["filter"] = {"andAll": [scope, config["filter"]]} if config.get("filter") else scope
