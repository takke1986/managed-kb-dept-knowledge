"""スクリプトが共通で使う、この環境の設定（リージョン・デプロイの出力・確認用アカウント・テストデータの場所）。

    config/app.json          リージョンなど（CDK と同じもの）
    .state-outputs.json      cdk deploy --outputs-file で書き出したスタックの出力（リポジトリには入れない）
    MKB_ACCOUNTS_FILE        確認用アカウントのパスワードのファイル（既定 ~/managed-kb-test-accounts.txt、600）
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))
REGION = CONFIG["region"]
STACK = "ManagedKbPrototype"
ACCOUNTS_FILE = Path(os.environ.get("MKB_ACCOUNTS_FILE", Path.home() / "managed-kb-test-accounts.txt"))
TESTDATA = ROOT / "testdata"


def outputs() -> dict:
    path = ROOT / ".state-outputs.json"
    if not path.exists():
        raise SystemExit(f"{path} がありません。cdk deploy --outputs-file ../.state-outputs.json で作ってください")
    return json.loads(path.read_text())[STACK]


def accounts() -> dict[str, str]:
    """確認用アカウントのメールアドレスとパスワード（1行に「メールアドレス パスワード」）。"""
    return dict(line.split(" ", 1) for line in ACCOUNTS_FILE.read_text().splitlines() if line.strip())
