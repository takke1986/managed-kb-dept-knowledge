"""Office ファイル（docx・pptx・xlsx）を XML から Markdown にする。

本文・表・テキストボックス・グラフの実データ・図形のつながりは XML から決まった手順で取り出す。
生成AIには通さないので、書き写し間違いや出力の途中切れが起きない。
XML に中身が無いもの（貼り付けた画像・スクリーンショット）だけを画像として取り出し、
呼び出し側が生成AIに説明させて差し込む。

    markdown, images = to_markdown("docx", body)
    # markdown の中の image_marker(i) を、images[i] の説明に置き換える

python-docx・python-pptx が拾わないもの（テキストボックス・グラフ・SmartArt・ヘッダー・
コメントなど）は XML を直接たどる。
"""

import hashlib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
W = "{%s}" % NS["w"]
R_EMBED = "{%s}embed" % NS["r"]
R_ID = "{%s}id" % NS["r"]
R_DM = "{%s}dm" % NS["r"]
READABLE_IMAGES = {"png", "jpeg", "jpg", "gif", "webp", "bmp", "tif", "tiff"}
MIN_IMAGE_BYTES = 2000        # これより小さい画像はアイコンや線とみなして読まない
MAX_IMAGES = 60               # 1ファイルで生成AIに読ませる画像の上限（費用の歯止め）
SHEET_BLOCK_ROWS = 50         # 大きな表は、見出し行を繰り返しながらこの行数ごとに区切る
SHEET_MAX_ROWS = 20000


def image_marker(i: int) -> str:
    return f"\u0000IMG{i}\u0000"


IMAGE_MARKER = re.compile("\u0000IMG(\\d+)\u0000")


@dataclass
class Output:
    lines: list[str] = field(default_factory=list)
    images: list[tuple[bytes, str]] = field(default_factory=list)
    _seen: dict[str, int] = field(default_factory=dict)

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def image(self, blob: bytes, name: str) -> str:
        """画像を登録し、本文に置く印を返す。同じ画像は1回だけ読む。"""
        ext = name.rsplit(".", 1)[-1].lower()
        if ext not in READABLE_IMAGES:
            return f"[図: {ext.upper()} 形式の図のため読み取れず]"
        if len(blob) < MIN_IMAGE_BYTES:
            return ""
        digest = hashlib.sha1(blob).hexdigest()
        if digest in self._seen:
            return image_marker(self._seen[digest])
        if len(self.images) >= MAX_IMAGES:
            return "[図: 画像が多いため読み取りを省略]"
        self._seen[digest] = len(self.images)
        self.images.append((blob, ext))
        return image_marker(len(self.images) - 1)

    def markdown(self) -> str:
        text = "\n".join(self.lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def to_markdown(ext: str, body: bytes) -> tuple[str, list[tuple[bytes, str]]]:
    out = Output()
    {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx}[ext](body, out)
    return out.markdown(), out.images


# ---------------------------------------------------------------- 共通

def _cell_text(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def _table(rows: list[list[str]]) -> list[str]:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [[_cell_text(c) for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return lines


def _has_ancestor(el, tags: set[str], stop) -> bool:
    parent = el.getparent()
    while parent is not None and parent is not stop:
        if parent.tag in tags:
            return True
        parent = parent.getparent()
    return False


FALLBACK = "{%s}Fallback" % NS["mc"]


def _xml_texts(el) -> list[str]:
    """a:t（DrawingML の文字）を段落ごとにまとめて返す。"""
    paras = []
    for p in el.iter("{%s}p" % NS["a"]):
        text = "".join(t.text or "" for t in p.iter("{%s}t" % NS["a"]))
        if text.strip():
            paras.append(text)
    return paras


def chart_markdown(xml: bytes, resolve=None) -> list[str]:
    """グラフの XML から、種類・タイトル・系列ごとの値を表にする。

    値は XML に残っているキャッシュ（c:numCache・c:strCache）から取る。
    キャッシュが無いときは、resolve（セル範囲 → 値の一覧）で参照先のセルから取る。"""
    root = etree.fromstring(xml)
    title = " ".join(_xml_texts(root.find(".//c:title", NS))) if root.find(".//c:title", NS) is not None else ""
    plot = root.find(".//c:plotArea", NS)
    kinds = [etree.QName(ch).localname for ch in plot if etree.QName(ch).localname.endswith("Chart")] if plot is not None else []

    def values(ref_parent) -> list[str]:
        if ref_parent is None:
            return []
        pts = ref_parent.findall(".//c:pt", NS)
        if pts:
            ordered = sorted(pts, key=lambda p: int(p.get("idx", 0)))
            return [(p.findtext("c:v", default="", namespaces=NS)) for p in ordered]
        formula = ref_parent.findtext(".//c:f", default="", namespaces=NS)
        if formula and resolve:
            return [str(v) for v in resolve(formula)]
        literal = ref_parent.findtext(".//c:v", default="", namespaces=NS)
        return [literal] if literal else []

    series = []
    for ser in root.iter("{%s}ser" % NS["c"]):
        name = values(ser.find("c:tx", NS))
        series.append({
            "name": name[0] if name else f"系列{len(series) + 1}",
            "cat": values(ser.find("c:cat", NS)) or values(ser.find("c:xVal", NS)),
            "val": values(ser.find("c:val", NS)) or values(ser.find("c:yVal", NS)),
        })
    lines = [f"［グラフ: {title or '（タイトルなし）'}（{'・'.join(kinds) or '種類不明'}）］"]
    if series:
        cats = max((s["cat"] for s in series), key=len)
        n = max([len(cats)] + [len(s["val"]) for s in series])
        rows = [["項目"] + [s["name"] for s in series]]
        for i in range(n):
            rows.append([cats[i] if i < len(cats) else str(i + 1)]
                        + [s["val"][i] if i < len(s["val"]) else "" for s in series])
        lines += _table(rows)
    return lines


def _related(part, rid):
    """関係 ID の先の部品。python-docx と python-pptx の両方で使える。"""
    rel = part.rels.get(rid) if rid else None
    return None if rel is None or rel.is_external else rel.target_part


def _smartart_texts(data_xml: bytes) -> list[str]:
    root = etree.fromstring(data_xml)
    texts = []
    for pt in root.iter("{%s}pt" % NS["dgm"]):
        if pt.get("type") in ("parTrans", "sibTrans", "pres", "doc"):
            continue
        texts += _xml_texts(pt)
    return texts


# ---------------------------------------------------------------- docx

def _docx(body: bytes, out: Output) -> None:
    from docx import Document

    doc = Document(io.BytesIO(body))
    style_names = {s.style_id: (s.name or "") for s in doc.styles}

    seen_parts = set()
    for rel in doc.part.rels.values():
        kind = rel.reltype.rsplit("/", 1)[-1]
        if kind in ("header", "footer") and not rel.is_external and rel.target_part.partname not in seen_parts:
            seen_parts.add(rel.target_part.partname)
            part = rel.target_part
            root = etree.fromstring(part.blob)
            text = " ".join(t for t in (_w_paragraph_text(p, part, out) for p in root.iter(W + "p")) if t)
            if text.strip():
                out.add(f"{'ヘッダー' if kind == 'header' else 'フッター'}: {text.strip()}")
    if out.lines:
        out.add()

    _w_block(doc.element.body, doc.part, out, style_names)

    for kind, label in (("comments", "コメント"), ("footnotes", "脚注"), ("endnotes", "文末脚注")):
        for rel in doc.part.rels.values():
            if rel.reltype.endswith("/" + kind) and not rel.is_external:
                root = etree.fromstring(rel.target_part.blob)
                for item in root:
                    if item.get(W + "id") in ("-1", "0"):  # 区切り線などの既定の項目
                        continue
                    text = " ".join(_w_paragraph_text(p, rel.target_part, out) for p in item.iter(W + "p")).strip()
                    if text:
                        out.add(f"{label}: {text}")


def _w_block(container, part, out: Output, style_names: dict, quote: str = "") -> None:
    for el in container:
        tag = el.tag
        if tag == W + "p":
            _w_paragraph(el, part, out, style_names, quote)
        elif tag == W + "tbl":
            out.add()
            for line in _table(_w_table_rows(el, part, out)):
                out.add(quote + line)
            out.add()
        elif tag == W + "sdt":
            content = el.find(W + "sdtContent")
            if content is not None:
                _w_block(content, part, out, style_names, quote)


def _w_paragraph(p, part, out: Output, style_names: dict, quote: str) -> None:
    style_id = p.find(f"{W}pPr/{W}pStyle")
    style = style_names.get(style_id.get(W + "val"), "") if style_id is not None else ""
    text = _w_paragraph_text(p, part, out)
    prefix = ""
    m = re.match(r"(?:Heading|見出し)\s*(\d)", style)
    if m:
        prefix = "#" * min(int(m.group(1)) + 1, 6) + " "
    elif style == "Title":
        prefix = "# "
    elif p.find(f"{W}pPr/{W}numPr") is not None or style.startswith("List"):
        prefix = "- "
    if text.strip():
        out.add(quote + prefix + text.strip())

    # 段落に付いた図形（テキストボックス・画像・グラフ・SmartArt）。古い Word 向けの代わり（Fallback）は読まない
    for box in p.iter(W + "txbxContent"):
        if _has_ancestor(box, {FALLBACK}, p) or _has_ancestor(box, {W + "txbxContent"}, p):
            continue
        out.add(quote + "> ［テキストボックス］")
        _w_block(box, part, out, style_names, quote + "> ")
    for el in p.iter():
        if _has_ancestor(el, {FALLBACK}, p):
            continue
        if el.tag == "{%s}blip" % NS["a"] and el.get(R_EMBED):
            _w_image(part, el.get(R_EMBED), out, quote)
        elif el.tag == "{%s}imagedata" % NS["v"] and el.get(R_ID):
            _w_image(part, el.get(R_ID), out, quote)
        elif el.tag == "{%s}chart" % NS["c"] and el.get(R_ID):
            target = _related(part, el.get(R_ID))
            if target is not None:
                out.add()
                for line in chart_markdown(target.blob):
                    out.add(quote + line)
                out.add()
        elif el.tag == "{%s}relIds" % NS["dgm"] and el.get(R_DM):
            target = _related(part, el.get(R_DM))
            if target is not None:
                texts = _smartart_texts(target.blob)
                if texts:
                    out.add(quote + "［図解（SmartArt）］ " + " / ".join(texts))


def _w_paragraph_text(p, part, out: Output) -> str:
    """段落の文字。テキストボックスの中と Fallback は除く（別に読む）。"""
    parts = []
    for el in p.iter(W + "t", W + "tab", W + "br", W + "cr"):
        if _has_ancestor(el, {W + "txbxContent", FALLBACK}, p):
            continue
        parts.append(el.text or "" if el.tag == W + "t" else ("\t" if el.tag == W + "tab" else "\n"))
    return "".join(parts)


def _w_table_rows(tbl, part, out: Output) -> list[list[str]]:
    rows = []
    for tr in tbl.findall(W + "tr"):
        row = []
        for tc in tr.findall(W + "tc"):
            merge = tc.find(f"{W}tcPr/{W}vMerge")
            if merge is not None and merge.get(W + "val") in (None, "continue"):
                row.append("")  # 縦に結合された続きのセル
                continue
            texts = [_w_paragraph_text(p, part, out) for p in tc.iter(W + "p")]
            row.append("\n".join(t for t in texts if t.strip()))
            span = tc.find(f"{W}tcPr/{W}gridSpan")
            if span is not None:
                row += [""] * (int(span.get(W + "val", "1")) - 1)
        rows.append(row)
    return rows


def _w_image(part, rid: str, out: Output, quote: str) -> None:
    target = _related(part, rid)
    if target is None:
        return
    marker = out.image(target.blob, str(target.partname))
    if marker:
        out.add(quote + marker)


# ---------------------------------------------------------------- pptx

EMU_ROW = 300000  # 約0.8cm。上下の位置がこれ以内なら同じ行とみなして左から並べる


def _pptx(body: bytes, out: Output) -> None:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(body))
    for n, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
        out.add(f"## スライド {n}" + (f": {title}" if title else ""))
        shapes = sorted(_flatten(slide.shapes), key=lambda s: (round(s[1] / EMU_ROW), s[2]))
        names = {}
        for shape, _, _ in shapes:
            label = shape.text_frame.text.strip() if shape.has_text_frame and shape.text_frame.text.strip() else shape.name
            names[shape.shape_id] = label.replace("\n", " ")
        edges = []
        for shape, _, _ in shapes:
            if slide.shapes.title is not None and shape.shape_id == slide.shapes.title.shape_id:
                continue
            _p_shape(shape, slide.part, out, names, edges)
        if edges:
            out.add("図形のつながり:")
            for a, b in edges:
                out.add(f"- {a} → {b}")
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                out.add(f"ノート: {notes}")
        out.add()


def _flatten(shapes, transform=lambda x, y: (x, y)):
    """グループの中の図形も含めて、スライド上の位置（上端・左端）と一緒に返す。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        left, top = shape.left or 0, shape.top or 0
        x, y = transform(left, top)
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            xfrm = shape._element.find(".//{%s}xfrm" % NS["a"])
            off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
            ch_off, ch_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
            if None not in (off, ext, ch_off, ch_ext):
                ox, oy, ex, ey = (int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")))
                cx, cy, cex, cey = (int(ch_off.get("x")), int(ch_off.get("y")),
                                    int(ch_ext.get("cx")) or 1, int(ch_ext.get("cy")) or 1)

                def inner(px, py, outer=transform, g=(ox, oy, ex, ey, cx, cy, cex, cey)):
                    gox, goy, gex, gey, gcx, gcy, gcex, gcey = g
                    return outer(gox + (px - gcx) * gex / gcex, goy + (py - gcy) * gey / gcey)
            else:
                inner = transform
            yield from _flatten(shape.shapes, inner)
        else:
            yield shape, y, x


def _p_shape(shape, part, out: Output, names: dict, edges: list) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    el = shape._element
    if el.tag == "{%s}cxnSp" % NS["p"]:  # コネクタ（矢印・線）
        st = el.find(".//a:stCxn", NS)
        end = el.find(".//a:endCxn", NS)
        if st is not None and end is not None:
            edges.append((names.get(int(st.get("id")), "?"), names.get(int(end.get("id")), "?")))
        return
    if shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            text = "".join(r.text for r in para.runs) or para.text
            if text.strip():
                out.add(("  " * para.level) + ("- " if para.level else "") + text.strip())
    if getattr(shape, "has_table", False) and shape.has_table:
        out.add()
        for line in _table([[c.text for c in row.cells] for row in shape.table.rows]):
            out.add(line)
        out.add()
    if getattr(shape, "has_chart", False) and shape.has_chart:
        out.add()
        for line in chart_markdown(shape.chart.part.blob):
            out.add(line)
        out.add()
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or el.find(".//a:blip", NS) is not None:
        blip = el.find(".//a:blip", NS)
        target = _related(part, blip.get(R_EMBED)) if blip is not None else None
        if target is not None:
            marker = out.image(target.blob, str(target.partname))
            if marker:
                out.add(marker)
    rel_ids = el.find(".//dgm:relIds", NS)
    data = _related(part, rel_ids.get(R_DM)) if rel_ids is not None else None
    if data is not None:
        texts = _smartart_texts(data.blob)
        if texts:
            out.add("［図解（SmartArt）］ " + " / ".join(texts))


# ---------------------------------------------------------------- xlsx

def _xlsx(body: bytes, out: Output) -> None:
    from openpyxl import load_workbook

    formulas = load_workbook(io.BytesIO(body), data_only=False)
    values = load_workbook(io.BytesIO(body), data_only=True)
    zf = zipfile.ZipFile(io.BytesIO(body))
    sheet_parts = _xlsx_sheet_parts(zf)

    def resolve(ref: str) -> list:
        """グラフのセル参照（'集計'!$B$3:$B$5）を値にする。"""
        m = re.match(r"^'?(.*?)'?!(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$", ref.strip())
        if not m or m.group(1) not in values.sheetnames:
            return []
        cells = values[m.group(1)][m.group(2).replace("$", "")]
        if not isinstance(cells, tuple):  # 1つのセル
            cells = ((cells,),)
        flat = [c for row in cells for c in (row if isinstance(row, tuple) else (row,))]
        return [_x_display(c, formulas[m.group(1)][c.coordinate]) for c in flat]

    for name in values.sheetnames:
        ws, wf = values[name], formulas[name]
        hidden = "（非表示のシート）" if ws.sheet_state != "visible" else ""
        out.add(f"## シート: {name}{hidden}")
        rows = [[_x_display(c, wf[c.coordinate]) for c in row] for row in ws.iter_rows()]
        rows = _trim(rows)
        if len(rows) > SHEET_MAX_ROWS:
            out.add(f"（{len(rows)} 行のうち先頭 {SHEET_MAX_ROWS} 行だけを載せる）")
            rows = rows[:SHEET_MAX_ROWS]
        if rows:
            # 見出し行は、2つ以上のセルが埋まった最初の行（上にある表題の行は表の前に出す）
            h = next((i for i, r in enumerate(rows) if sum(1 for c in r if c.strip()) >= 2), 0)
            for r in rows[:h]:
                out.add(" ".join(c for c in r if c.strip()))
            header, data = rows[h], rows[h + 1:]
            for start in range(0, max(len(data), 1), SHEET_BLOCK_ROWS):
                out.add()
                for line in _table([header] + data[start:start + SHEET_BLOCK_ROWS]):
                    out.add(line)
            out.add()
        _xlsx_drawings(zf, sheet_parts.get(name), out, resolve)
        out.add()


def _x_display(cell, formula_cell) -> str:
    import datetime

    v = cell.value
    if v is None:
        f = formula_cell.value
        return f if isinstance(f, str) and f.startswith("=") else ""
    fmt = cell.number_format or ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if "%" in fmt:
            return f"{v * 100:g}%"
        if isinstance(v, float) and not v.is_integer():
            return f"{v:,.6g}" if abs(v) < 1e6 else f"{v:,.2f}"
        return f"{int(v):,}"
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    return str(v)


def _trim(rows: list[list[str]]) -> list[list[str]]:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    used = [i for i in range(max(len(r) for r in rows)) if any(i < len(r) and r[i].strip() for r in rows)]
    return [[r[i] if i < len(r) else "" for i in range(used[0], used[-1] + 1)] for r in rows]


def _rels(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    folder, name = posixpath.split(part)
    path = f"{folder}/_rels/{name}.rels"
    if path not in zf.namelist():
        return {}
    out = {}
    for rel in etree.fromstring(zf.read(path)).findall("pr:Relationship", NS):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target")
        out[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
    return out


def _xlsx_sheet_parts(zf: zipfile.ZipFile) -> dict[str, str]:
    rels = _rels(zf, "xl/workbook.xml")
    root = etree.fromstring(zf.read("xl/workbook.xml"))
    return {s.get("name"): rels.get(s.get(R_ID)) for s in root.iter("{%s}sheet" % NS["s"])}


def _xlsx_drawings(zf: zipfile.ZipFile, sheet_part: str | None, out: Output, resolve) -> None:
    """シートに載った図形・画像・グラフ。どのセルの位置にあるかも添える。"""
    if not sheet_part:
        return
    for drawing in [t for t in _rels(zf, sheet_part).values() if "/drawings/" in t and t.endswith(".xml")]:
        rels = _rels(zf, drawing)
        root = etree.fromstring(zf.read(drawing))
        for anchor in root:
            frm = anchor.find("xdr:from", NS)
            where = ""
            if frm is not None:
                from openpyxl.utils import get_column_letter
                col = int(frm.findtext("xdr:col", default="0", namespaces=NS)) + 1
                row = int(frm.findtext("xdr:row", default="0", namespaces=NS)) + 1
                where = f"（{get_column_letter(col)}{row} 付近）"
            for chart in anchor.iter("{%s}chart" % NS["c"]):
                target = rels.get(chart.get(R_ID))
                if target in zf.namelist():
                    out.add()
                    lines = chart_markdown(zf.read(target), resolve)
                    out.add(lines[0] + where)
                    for line in lines[1:]:
                        out.add(line)
            for blip in anchor.iter("{%s}blip" % NS["a"]):
                target = rels.get(blip.get(R_EMBED))
                if target in zf.namelist():
                    marker = out.image(zf.read(target), target)
                    if marker:
                        out.add(f"［画像{where}］")
                        out.add(marker)
            for sp in anchor.iter("{%s}sp" % NS["xdr"]):
                texts = _xml_texts(sp)
                if texts:
                    out.add(f"［図形の文字{where}］ " + " ".join(texts))
