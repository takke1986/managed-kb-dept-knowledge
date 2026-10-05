"""MCP サーバー（scripts/mcp_audit_server.py）のプロトコル処理を確かめる。AWS には触れない。"""

import json

import mcp_audit_server as server


def call(method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_initialize_advertises_tools():
    r = call("initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["capabilities"] == {"tools": {}}
    assert r["result"]["serverInfo"]["name"] == "kb-audit"


def test_notifications_get_no_reply():
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_names():
    names = [t["name"] for t in call("tools/list")["result"]["tools"]]
    assert names == ["list_departments", "audit_department"]


def test_unknown_department_is_refused_before_any_aws_call(monkeypatch):
    # 要件5.3: 未定義の部署は子プロセス（＝AWS 呼び出し）を起動する前に断る
    def boom(*a, **k):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(server.subprocess, "run", boom)
    r = call("tools/call", {"name": "audit_department", "arguments": {"department": "accounting"}})
    assert r["result"]["isError"] is True
    assert "未定義の部署" in json.loads(r["result"]["content"][0]["text"])["error"]


def test_unknown_method_is_an_error():
    assert call("nope")["error"]["code"] == -32601
