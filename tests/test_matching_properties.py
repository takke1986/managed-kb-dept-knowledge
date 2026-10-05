"""要件2（判定）の性質4-6を、生成した入力で確かめる。

文字の集合は test_normalise_properties.py と同じ考え方で選んでいる。日本語の文書に
出てくる文字に加えて、NFKC が合成や互換分解を起こす文字（濁点・半濁点、半角カナ、
結合記号）を入れる。切り出し位置によって NFKC の結果が変わるのはこれらの文字なので、
外すと性質4が「たまたま」成り立ってしまう。
"""

from hypothesis import assume, given
from hypothesis import strategies as st

from kbaudit.matching import Verdict, classify
from kbaudit.normalise import normalise

# 空白は代表的なものだけを使う。空白の網羅は性質2（test_normalise_properties.py）で
# 確かめているので、ここでは判定のほうに試行回数を回す。
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

# 日本語に寄せた文字列と、Unicode 全体からの文字列の両方を試す。
text = st.one_of(st.text(japanese_char), st.text())


@st.composite
def source_and_slice(draw):
    """原文と、その中の切り出し位置 i <= j を作る。"""
    t = draw(text)
    i = draw(st.integers(min_value=0, max_value=len(t)))
    j = draw(st.integers(min_value=i, max_value=len(t)))
    return t, i, j


# ---- 性質4 原文の切り出しは必ず verbatim（要件2.1） ----


@given(source_and_slice())
def test_every_excerpt_is_verbatim(case):
    """**Validates: Requirements 2.1**"""
    t, i, j = case
    excerpt = t[i:j]
    # 正規化して空になる切り出しは SKIPPED が正しい（要件2.5）ので、性質の対象外である。
    assume(normalise(excerpt))
    # 文字と結合記号の間（「か」と結合濁点など）で切ると、切り出し側では合成も並べ替えも
    # 起きず、原文側とは別の文字列になる。KB のチャンク分割はそこでは切らないので、
    # 正規化が切れ目をまたいで結合しない位置で切ったものだけを対象にする。
    assume(_clean_cut(t, i) and _clean_cut(t, j))
    assert classify(excerpt, {}, t) is Verdict.VERBATIM


def _clean_cut(t: str, k: int) -> bool:
    """k で切っても、正規化の結果が前後の連結と同じになる位置か。"""
    return normalise(t[:k]) + normalise(t[k:]) == normalise(t)


# ---- 性質5 1文字でも変われば verbatim にならない（要件2.2） ----


@given(source_and_slice(), st.integers(min_value=0), st.one_of(japanese_char, st.characters()))
def test_one_changed_character_is_altered(case, position, replacement):
    """**Validates: Requirements 2.2**"""
    t, i, j = case
    excerpt = t[i:j]
    assume(excerpt)
    k = position % len(excerpt)
    assume(replacement != excerpt[k])
    changed = excerpt[:k] + replacement + excerpt[k + 1:]
    # 変えた結果が、たまたま原文のどこかにある場合（空白や全角への置き換えを含む）は
    # 書き換えではないので除く。空に寄る場合は SKIPPED が正しいので同じく除く。
    assume(normalise(changed))
    assume(normalise(changed) not in normalise(t))
    assert classify(changed, {}, t) is Verdict.ALTERED


# ---- 性質6 画像のチャンクは必ず skipped（要件2.3） ----


@given(text, st.one_of(st.none(), text), st.sampled_from(["image", "IMAGE", "Image"]))
def test_image_chunk_is_always_skipped(chunk, source, media_type):
    """**Validates: Requirements 2.3**"""
    # 原文が無い（None）場合も含める。画像の判定は「元が無い」より先に行う順番だからである。
    assert classify(chunk, {"_media_type": media_type}, source) is Verdict.SKIPPED
