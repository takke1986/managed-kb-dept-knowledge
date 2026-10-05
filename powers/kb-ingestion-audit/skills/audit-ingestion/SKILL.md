---
name: audit-ingestion
description: 部署ごとに KB のチャンクが原文と一字一句一致するかを監査し、変わった箇所を報告する。取り込み処理を変えた後や、KB を再同期した後に使う。
---

# KB 取り込み監査

Managed KB は取り込み時に文書を2回解析し、2回目の結果で上書きする。2回目は日本語の
固有名詞や数値を書き換えることがある（例: `膳所営業所` → `陸所営業所`、`○%` → `0%`）。
このスキルは、検索で返るチャンクが原文の Markdown にそのまま含まれるかを確かめる。

## 前提

- `managed-kb-dept-knowledge` リポジトリのルートで作業していること
- スタック `ManagedKbPrototype` がデプロイ済みで、`.state-outputs.json` があること
- AWS の認証が通っていること（`aws sts get-caller-identity`）

## 手順

1. `kb-audit` の `list_departments` で監査できる部署を確認する。
   定義に無い部署は監査しない（AWS に触れる前に断られる）。
2. 部署ごとに `audit_department` を呼ぶ。問い合わせは省略してよい
   （文書名と見出しから最大40件を作る）。
3. 結果を次の形で報告する。
   - 部署ごとの `audited` と `verbatim` の件数、verbatim 率
   - `altered` があれば、文書キーと差分スパン（`原文 -> チャンク`）を全件
   - `unmatched` の理由が `isolation` なら、部署分離の不具合として最優先で報告する
4. `altered` が1件でもあれば、終了コードは1になる。取り込みのやり直しで直るか、
   `functions/convert` の Markdown 変換で避けられるかを提案する。

## してはいけないこと

- チャンクの本文を丸ごと貼らない。差分スパンだけを示す（他部署の本文は監査結果にも出ない）。
- 期待値を正規化してから比べない（`.kiro/steering/audit-matching.md`）。
- 監査のために KB の再同期や S3 への書き込みをしない。監査は読み取りのみ。
