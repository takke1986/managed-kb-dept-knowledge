"""Office の読み取りを確かめるための架空の書類を testdata/office/ に作る。

それぞれに、XML のどこから読むべきかが分かる目印の文言を入れる。目印は EXPECTED に並べ、
scripts/check_office.py が書き起こしに含まれるかを確かめる。

    .venv/bin/python scripts/make_office_fixtures.py
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "testdata" / "office"
FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"

EXPECTED = {
    "報告書-図とテキストボックス.docx": [
        "第3四半期の活動報告",          # 見出し
        "東日本エリア",                  # 表
        "テキストボックス内の注記：納期は10月15日",  # 浮動のテキストボックス
        "社外秘・ヘッダーの文言",        # ヘッダー
        "画像の中の文字：受注金額 482万円",  # 画像（生成AIが読む）
    ],
    "提案書-グラフとフロー.pptx": [
        "新サービス導入のご提案",        # タイトル
        "4月", "135",                    # グラフの実データ
        "申請",  "承認",                 # コネクタでつながる図形
        "申請 → 承認",                   # つながり
        "グループ内の文字：担当は企画部",  # グループ化した図形
        "ノートの文言：価格は税抜で説明する",  # ノート
        "スライド画像の文字：導入費用 30万円",  # 画像（生成AIが読む）
    ],
    "売上集計-グラフ付き.xlsx": [
        "売上集計表",                    # 結合セル
        "大阪支店", "2,450",            # 表の値
        "=SUM(B3:B5)",                  # キャッシュ値の無い数式
        "支店別売上",                    # グラフのタイトル
        "名古屋支店",                    # グラフの系列（セル参照から解決）
        "集計方法は経理部の定義に従う",  # 2枚目のシート
        "シート画像の文字：締め日は25日",  # シートの画像（生成AIが読む）
    ],
}


def text_image(text: str, size=(900, 220)) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="navy", width=4)
    draw.text((40, 80), text, fill="black", font=ImageFont.truetype(FONT, 48))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_docx(path: Path):
    from docx import Document
    from docx.shared import Cm
    from lxml import etree

    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "社外秘・ヘッダーの文言"
    doc.add_heading("第3四半期の活動報告", level=1)
    doc.add_paragraph("本報告では、各エリアの受注状況をまとめる。")
    table = doc.add_table(rows=3, cols=2)
    for r, row in enumerate([("エリア", "受注件数"), ("東日本エリア", "42"), ("西日本エリア", "37")]):
        for c, v in enumerate(row):
            table.cell(r, c).text = v
    doc.add_paragraph("受注金額の推移は次の図のとおり。")
    doc.add_picture(io.BytesIO(text_image("画像の中の文字：受注金額 482万円")), width=Cm(12))

    # 浮動のテキストボックス（python-docx には作る API が無いので XML を差し込む）
    p = doc.add_paragraph("続いて、納期の注意点を示す。")
    anchor = """
<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
     xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
     xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
     xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
 <mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>
  <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" behindDoc="0"
     locked="0" layoutInCell="1" allowOverlap="1">
   <wp:simplePos x="0" y="0"/>
   <wp:positionH relativeFrom="column"><wp:posOffset>3000000</wp:posOffset></wp:positionH>
   <wp:positionV relativeFrom="paragraph"><wp:posOffset>100000</wp:posOffset></wp:positionV>
   <wp:extent cx="2500000" cy="600000"/><wp:wrapSquare wrapText="bothSides"/>
   <wp:docPr id="10" name="TextBox 1"/>
   <a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
    <wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="2500000" cy="600000"/></a:xfrm>
     <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>
     <wps:txbx><w:txbxContent><w:p><w:r><w:t>テキストボックス内の注記：納期は10月15日</w:t></w:r></w:p></w:txbxContent></wps:txbx>
     <wps:bodyPr/></wps:wsp>
   </a:graphicData></a:graphic>
  </wp:anchor></w:drawing></mc:Choice>
  <mc:Fallback><w:pict><w:t>（古い Word 向けの代わりの表示）</w:t></w:pict></mc:Fallback>
 </mc:AlternateContent>
</w:r>"""
    p._p.append(etree.fromstring(anchor))
    doc.add_paragraph("以上。")
    doc.save(path)


def make_pptx(path: Path):
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Cm

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "新サービス導入のご提案"
    s1.placeholders[1].text = "2026年度 企画部"
    s1.notes_slide.notes_text_frame.text = "ノートの文言：価格は税抜で説明する"

    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "月別の問い合わせ件数"
    data = CategoryChartData()
    data.categories = ["4月", "5月", "6月"]
    data.add_series("問い合わせ件数", (120, 135, 150))
    s2.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Cm(2), Cm(4), Cm(16), Cm(9), data)

    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "承認の流れ"
    a = s3.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Cm(2), Cm(6), Cm(5), Cm(2))
    a.text = "申請"
    b = s3.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Cm(14), Cm(6), Cm(5), Cm(2))
    b.text = "承認"
    conn = s3.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Cm(7), Cm(7), Cm(14), Cm(7))
    conn.begin_connect(a, 3)
    conn.end_connect(b, 1)
    group = s3.shapes.add_group_shape()
    box = group.shapes.add_textbox(Cm(2), Cm(11), Cm(10), Cm(1.5))
    box.text_frame.text = "グループ内の文字：担当は企画部"
    group.shapes.add_shape(MSO_SHAPE.OVAL, Cm(13), Cm(11), Cm(1.5), Cm(1.5))

    s4 = prs.slides.add_slide(prs.slide_layouts[5])
    s4.shapes.title.text = "費用"
    s4.shapes.add_picture(io.BytesIO(text_image("スライド画像の文字：導入費用 30万円")), Cm(2), Cm(5), Cm(20))
    prs.save(path)


def make_xlsx(path: Path):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.drawing.image import Image as XlImage

    wb = Workbook()
    ws = wb.active
    ws.title = "集計"
    ws["A1"] = "売上集計表"
    ws.merge_cells("A1:C1")
    ws.append(["支店", "売上（千円）", "前年比"])
    for row in [("東京支店", 3120, "104%"), ("大阪支店", 2450, "98%"), ("名古屋支店", 1870, "101%")]:
        ws.append(row)
    ws["A6"] = "合計"
    ws["B6"] = "=SUM(B3:B5)"  # openpyxl で作るとキャッシュ値が無い
    chart = BarChart()
    chart.title = "支店別売上"
    chart.add_data(Reference(ws, min_col=2, min_row=2, max_row=5), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=3, max_row=5))
    ws.add_chart(chart, "E2")
    ws2 = wb.create_sheet("メモ")
    ws2["A1"] = "集計方法は経理部の定義に従う"
    img = XlImage(io.BytesIO(text_image("シート画像の文字：締め日は25日")))
    ws2.add_image(img, "A3")
    wb.save(path)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    make_docx(OUT / "報告書-図とテキストボックス.docx")
    make_pptx(OUT / "提案書-グラフとフロー.pptx")
    make_xlsx(OUT / "売上集計-グラフ付き.xlsx")
    print("作成:", *sorted(p.name for p in OUT.iterdir()))
