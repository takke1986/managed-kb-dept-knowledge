"""要件3（違いの表示）の性質7-8を、生成した入力で確かめる。

文字の集合は test_matching_properties.py と同じ考え方で選んでいる。違いの位置を探す処理は
正規化した後の文字列の上で動くので、NFKC が合成や互換分解を起こす文字（濁点・半濁点、
半角カナ、全角英数、結合記号）と和文の空白を入れておかないと、正規化で長さが変わる場合を
試せない。
"""

from hypothesis import given
from hypothesis import strategies as st

from kbaudit.matching import Verdict, classify
from kbaudit.spans import MAX_SPAN, diff_spans

# 空白は代表的なものだけを使う。空白の網羅は性質2（test_normalise_properties.py）で
# 確かめているので、ここでは違いの取り出しのほうに試行回数を回す。
whitespace = st.sampled_from([" ", "\u3000", "\n", "\t", "\r"])

japanese_char = st.one_of(
    st.characters(min_codepoint=0x3041, max_codepoint=0x309F),  # ひらがな・濁点
    st.characters(min_codepoint=0x30A0, max_codepoint=0x30FF),  # カタカナ
    st.characters(min_codepoint=0xFF01, max_codepoint=0xFF9F),  # 全角英数・半角カナ
    st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),  # 漢字
    st.characters(min_codepoint=0x3000, max_codepoint=0x303F),  # 和文の記号
    st.characters(min_codepoint=0x0300, max_codepoint=0x036F),  # 結合記号
    st.characters(min_codepoint=0x20, max_codepoint=0x7E),  # ASCII
    whitespace,
)

# 40文字で切り詰める処理を通らせるには、40文字を超える違いが要る。既定の長さだけでは
# 長い文字列がまれにしか出ないので、長いものを別に混ぜる。
text = st.one_of(
    st.text(japanese_char),
    st.text(japanese_char, min_size=MAX_SPAN + 1, max_size=300),
    st.text(),
)

# limit は 0 も含める。0 のときは「最大 0 件」なので空でなければならない。
# 負の値は「最大で負の件数」という意味を持たないので性質の対象外にしている。
limit = st.integers(min_value=0, max_value=10)


@st.composite
def chunk_and_source(draw):
    """チャンクと原文の組を作る。

    まったく無関係な2つの文字列だけでは、違いが1か所の場合や、原文の一部そのものの場合が
    ほとんど出ない。そこで原文の切り出しをそのまま使う場合と、切り出しの中を書き換えた場合も
    混ぜる。書き換えは置き換え・挿入・削除を何か所かに入れ、違いの件数が limit を超える
    場合も出るようにする。
    """
    source = draw(text)
    kind = draw(st.sampled_from(["unrelated", "excerpt", "edited"]))
    if kind == "unrelated" or not source:
        return draw(text), source
    i = draw(st.integers(min_value=0, max_value=len(source)))
    j = draw(st.integers(min_value=i, max_value=len(source)))
    chunk = source[i:j]
    if kind == "excerpt":
        return chunk, source
    edits = draw(st.lists(st.tuples(st.integers(min_value=0), st.sampled_from(["replace", "insert", "delete"]), text), max_size=12))
    for position, op, piece in edits:
        k = position % (len(chunk) + 1)
        if op == "insert":
            chunk = chunk[:k] + piece + chunk[k:]
        elif op == "replace":
            chunk = chunk[:k] + piece + chunk[k + 1:]
        else:
            chunk = chunk[:k] + chunk[k + 1:]
    return chunk, source


# ---- 性質7 違いの件数と長さには上限がある（要件3.2, 3.3） ----


@given(chunk_and_source(), limit)
def test_spans_are_bounded(case, max_spans):
    """**Validates: Requirements 3.2, 3.3**"""
    chunk, source = case
    spans = diff_spans(chunk, source, limit=max_spans)
    assert len(spans) <= max_spans
    for span in spans:
        assert len(span.source) <= MAX_SPAN
        assert len(span.chunk) <= MAX_SPAN


# ---- 性質8 verbatim のチャンクには違いが無い（要件3.1） ----


@given(chunk_and_source(), limit)
def test_verbatim_chunk_has_no_spans(case, max_spans):
    """**Validates: Requirements 3.1**"""
    chunk, source = case
    # 判定と違いの取り出しが食い違うと、verbatim なのに違いが表示されて人を迷わせる。
    # 含意の形で書くのは、verbatim でない組では何を返してもこの性質に反しないからである。
    if classify(chunk, {}, source) is Verdict.VERBATIM:
        assert diff_spans(chunk, source, limit=max_spans) == []
