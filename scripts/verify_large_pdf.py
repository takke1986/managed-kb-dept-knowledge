"""大きな PDF（120ページ超）の確認。Step Functions でページを分けて並べて書き起こし、正しくつながること。

モデル就業規則（94ページ）を2回つないだ PDF の150ページ目に、目印のページを差し込んで置く。
書き起こしに目印が「<!-- page 150 -->」の後に入っていれば、分けた部分が順番どおりにつながっている。
生成AIで約190ページを読むので、数分かかり、費用もかかる（数百円程度）。終わったら置いたものを消す。

    .venv/bin/python scripts/verify_large_pdf.py
"""

import io
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.argv = sys.argv[:1] + ["--skip-upload"]
import verify as v  # noqa: E402

MARK = "大きなPDFの目印：この文書の保存期間は7年とする"
SRC = v.TESTDATA / "rules" / "モデル就業規則.pdf"
KEY = "legal/確認用/大きな規程集.pdf"


def build() -> bytes:
    import pypdfium2 as pdfium

    img = Image.new("RGB", (1240, 1754), "white")
    ImageDraw.Draw(img).text((120, 300), MARK, fill="black",
                             font=ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 44))
    mark_pdf = io.BytesIO()
    img.save(mark_pdf, "PDF", resolution=150)
    src = pdfium.PdfDocument(SRC.read_bytes())
    out = pdfium.PdfDocument.new()
    out.import_pages(src)
    out.import_pages(src, pages=list(range(55)))       # 95〜149 ページ
    out.import_pages(pdfium.PdfDocument(mark_pdf.getvalue()))  # 150 ページ目が目印
    out.import_pages(src, pages=list(range(55, len(src))))  # 151 ページ以降
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue(), len(out)


c = v.Checks()
body, pages = build()
print(f"{pages} ページの PDF を作った", flush=True)
tok = v.token(v.LEGAL)
legal = v.storage(tok, "legal")
legal.put_object(Bucket=v.OUT["FilesBucket"], Key=KEY, Body=body)
start = time.time()
try:
    status = ""
    while time.time() - start < 3600:
        _, r = v.api("GET", "/api/files?department=legal", tok)
        f = next((f for f in r["files"] if f["key"] == KEY), {})
        status = f.get("status", "")
        print(f"  {int(time.time() - start)}秒: {status} {f.get('reason', '')}", flush=True)
        if status in ("converted", "failed", "skipped", "blocked"):
            break
        time.sleep(30)
    c.check(f"{pages} ページの PDF が書き起こされた", status == "converted", status)
    sfn = v.boto3.client("stepfunctions", region_name=v.REGION)
    runs = sfn.list_executions(stateMachineArn=v.OUT["LargePdfStateMachine"], maxResults=1)["executions"]
    c.check("Step Functions で処理された", bool(runs) and runs[0]["status"] == "SUCCEEDED", runs[:1] and runs[0]["status"])
    s3 = v.boto3.client("s3", region_name=v.REGION)
    md = s3.get_object(Bucket=v.OUT["DocsBucket"], Key=v.text_key(KEY))["Body"].read().decode("utf-8")
    page150 = md.find("<!-- page 150 -->")
    page151 = md.find("<!-- page 151 -->")
    mark_at = md.find("7年")
    c.check("目印が150ページ目にある（分けた部分が順番どおりにつながっている）",
            page150 >= 0 and page150 < mark_at < (page151 if page151 >= 0 else len(md)), (page150, mark_at, page151))
    c.check(f"最後のページ（{pages}）まである", f"<!-- page {pages} -->" in md)
    c.check("かかった時間（参考）", True, f"{int(time.time() - start)}秒")
finally:
    legal.delete_object(Bucket=v.OUT["FilesBucket"], Key=KEY)
    print("  置いたものを消した", flush=True)
sys.exit(c.summary())
