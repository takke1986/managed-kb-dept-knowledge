"""KB 取り込み監査。KB が索引した内容が、こちらが渡した Markdown のままかを確かめる。

仕様は .kiro/specs/kb-ingestion-audit/、照合の決まりは .kiro/steering/audit-matching.md。
AWS に触れるのは scripts/audit_ingestion.py だけで、このパッケージは純粋な関数だけを持つ。
"""
