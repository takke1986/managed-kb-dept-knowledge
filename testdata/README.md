# 確かめに使う書類

| フォルダ | 中身 | 使うもの |
|---|---|---|
| `fixtures/` | 架空の見積書・契約書（スキャンの PDF、写真、Excel、Word） | `verify.py` ほか |
| `office/` | 図形・テキストボックス・グラフ・表を含む架空の Office 文書（`scripts/make_office_fixtures.py` が作る） | `verify.py`・`eval_rag.py` |
| `protected/` | パスワード付き・IRM 風の保護されたファイル（中身は架空） | `verify_ops.py` |
| `rules/` | 厚生労働省「モデル就業規則」（Word・PDF） | `eval_rag.py`・`verify_large_pdf.py` |

`rules/` の出典: 厚生労働省「モデル就業規則」（厚生労働省のウェブサイトで公開されているもの）。評価のためにそのまま使っている。
それ以外はすべてこの試作のために作った架空の書類で、実在の会社・人物とは関係ない。
