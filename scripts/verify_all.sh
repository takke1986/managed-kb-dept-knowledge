#!/bin/bash
# すべての確かめを順に流す（1〜2時間かかる）。結果は final_*.log。scripts/verify.py が先頭（書類を置く）
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
for s in verify verify_tags verify_tag_admin verify_membership verify_malware verify_burst verify_ledger verify_ops verify_large_pdf; do
  echo "=== $s" 
  $PY scripts/$s.py > final_$s.log 2>&1; echo "exit=$?"; tail -1 final_$s.log
done
echo "=== eval"
$PY scripts/eval_rag.py setup > final_eval_setup.log 2>&1; echo "setup exit=$?"
$PY scripts/eval_rag.py > final_eval.log 2>&1; echo "exit=$?"; tail -2 final_eval.log
