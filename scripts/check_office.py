"""Office の書き起こしに、試験用の書類の目印がすべて入っているかを手元で確かめる。

画像の説明は生成AIを呼ばずに「（画像 N）」と置くので、画像の中の文字の目印は数に入れない。
生成AIまで含めた確認は、デプロイ後に scripts/verify.py で行う。

    .venv/bin/python scripts/check_office.py [--show]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "functions" / "convert"), str(ROOT / "scripts")]
import office  # noqa: E402
from make_office_fixtures import EXPECTED, OUT  # noqa: E402

failed = 0
for name, marks in EXPECTED.items():
    md, images = office.to_markdown(name.rsplit(".", 1)[-1], (OUT / name).read_bytes())
    md = office.IMAGE_MARKER.sub(lambda m: f"（画像 {m.group(1)}）", md)
    if "--show" in sys.argv:
        print(f"===== {name}\n{md}")
    for mark in marks:
        if "画像の文字" in mark or "画像の中の文字" in mark:
            continue
        ok = mark in md
        failed += not ok
        print(f"{'OK' if ok else 'NG'}  {name}: {mark}")
    print(f"    画像 {len(images)} 枚を生成AIに回す")
sys.exit(1 if failed else 0)
