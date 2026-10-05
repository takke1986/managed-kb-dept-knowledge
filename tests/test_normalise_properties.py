"""要件1（正規化）の性質1-3を、生成した入力で確かめる。

文字の集合は日本語の文書を主に想定している。ひらがな・カタカナ・半角カナ・漢字・
全角英数字に加えて、濁点・半濁点（結合文字 U+3099/U+309A と単独の U+309B/U+309C）と
ラテン文字の結合記号も入れている。NFKC が文字を合成したり、互換分解で空白を
生み出したりするのはこれらの文字なので、外すと性質が「たまたま」成り立ってしまう。
"""
import sys
import unicodedata

from hypothesis import example, given
from hypothesis import strategies as st

from kbaudit.normalise import normalise

# str.isspace が真になる文字をすべて集める。NFKC をかけても空白のままであることを
# 確かめてから使う。もし空白以外に寄る空白があれば、性質2の前提が崩れるからである。
WHITESPACE = [chr(i) for i in range(sys.maxunicode + 1) if chr(i).isspace()]
assert all(
    all(ch.isspace() for ch in unicodedata.normalize("NFKC", w)) for w in WHITESPACE
)

whitespace = st.sampled_from(WHITESPACE)

# 日本語の文書に出てくる文字の範囲。範囲で与えるのは、個々の文字を手で選ぶと
# 性質を破る文字を無意識に避けてしまうからである。
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
# 設計の性質は「どんな文字列でも」と書いているので、範囲を日本語に限ることはしない。
text = st.one_of(st.text(japanese_char), st.text())


# ---- 性質1 冪等性（要件1.2） ----

# U+309B（単独の濁点）は NFKC で「空白 + 結合濁点 U+3099」に分解される。
# 1回目の正規化でその空白を除くと結合濁点が「か」に直接続き、2回目の NFKC で「が」に
# 合成される。実在する日本語の文字で性質が破れることを、例として固定しておく。
@example("か゛")
@given(text)
def test_normalise_is_idempotent(s):
    """**Validates: Requirements 1.2**"""
    once = normalise(s)
    assert normalise(once) == once


# ---- 性質2 空白を挟んでも変わらない（要件1.1, 1.3） ----

# 「か」と結合濁点の間に改行が入ると、NFKC は両者を合成できなくなる。チャンク分割で
# 行が組み直されたときに起こり得る形なので、例として固定しておく。
@example("か\u3099", 1, "\n")
@given(text, st.integers(min_value=0), whitespace)
def test_inserting_whitespace_does_not_change_normalised_text(s, position, ws):
    """**Validates: Requirements 1.1, 1.3**"""
    i = position % (len(s) + 1)
    assert normalise(s[:i] + ws + s[i:]) == normalise(s)


# ---- 性質3 全角と半角で変わらない（要件1.3） ----

ASCII_ALNUM = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def to_full_width(ch: str) -> str:
    # 全角英数字は ASCII の符号位置に 0xFEE0 を足した位置にある（Ａ = U+FF21）。
    return chr(ord(ch) + 0xFEE0)


@given(text, st.lists(st.booleans()))
def test_full_width_ascii_does_not_change_normalised_text(s, flags):
    """**Validates: Requirements 1.3**"""
    # 置き換える文字を一部だけにするのは、全角と半角が混ざった実際の文書を真似るためである。
    widened = "".join(
        to_full_width(ch) if ch in ASCII_ALNUM and (flags[i] if i < len(flags) else True) else ch
        for i, ch in enumerate(s)
    )
    assert normalise(widened) == normalise(s)
