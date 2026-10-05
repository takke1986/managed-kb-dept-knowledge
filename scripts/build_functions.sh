#!/usr/bin/env bash
# Lambda の配布物を build/<関数名>/ に作る。依存は Lambda（arm64・Python 3.13）向けの wheel を入れる。
# Docker を使わないため、uv でプラットフォームを指定して入れる。
set -euo pipefail
cd "$(dirname "$0")/.."

# macOS の bash 3.2 には連想配列が無いので case で書く
deps() {
  case "$1" in
    convert) echo "boto3>=1.43.36 pypdfium2 pillow python-pptx python-docx openpyxl lxml" ;;
    sync) echo "boto3>=1.43.36" ;;
    interceptor) echo "pyjwt[crypto]" ;;
    api) echo "boto3>=1.43.36 pyjwt[crypto] strands-agents mcp" ;;
    *) echo "" ;;
  esac
}

rm -rf build
for fn in convert sync acl_sync pretoken interceptor api; do
  out="build/$fn"
  mkdir -p "$out"
  cp functions/"$fn"/*.py "$out"/
  cp functions/shared/*.py config/app.json "$out"/
  if [ -n "$(deps "$fn")" ]; then
    # shellcheck disable=SC2086
    uv pip install --quiet --target "$out" --python-version 3.13 \
      --python-platform aarch64-manylinux2014 --only-binary :all: $(deps "$fn")
  fi
  find "$out" -name "__pycache__" -type d -prune -exec rm -rf {} +
  echo "$fn: $(du -sh "$out" | cut -f1)"
done
