"""要件3（差分）と要件4（収集）の性質7-9を、生成した入力で確かめる。"""

from hypothesis import given
from hypothesis import strategies as st

from kbaudit.collect import collect_chunks
from kbaudit.matching import Verdict, classify
from kbaudit.spans import MAX_SPAN, diff_spans

japanese_char = st.one_of(
    st.characters(min_codepoint=0x3041, max_codepoint=0x309F),
    st.characters(min_codepoint=0x30A0, max_codepoint=0x30FF),
    st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
    st.characters(min_codepoint=0x20, max_codepoint=0x7E),
)
text = st.one_of(st.text(japanese_char), st.text())


# ---- 性質7 差分は件数も長さも上限を超えない（要件3.2・3.3） ----


@given(text, text, st.integers(min_value=0, max_value=10))
def test_spans_never_exceed_their_bounds(chunk, source, limit):
    """**Validates: Requirements 3.2, 3.3**"""
    spans = diff_spans(chunk, source, limit=limit)
    assert len(spans) <= limit
    assert all(len(s.source) <= MAX_SPAN and len(s.chunk) <= MAX_SPAN for s in spans)


# ---- 性質8 verbatim のチャンクには差分が無い（要件3.1） ----


@given(text, text)
def test_verbatim_chunk_has_no_spans(chunk, source):
    """**Validates: Requirements 3.1**"""
    if classify(chunk, {}, source) is Verdict.VERBATIM:
        assert diff_spans(chunk, source) == []


# ---- 性質9 同じチャンクが何度返っても1回だけ数える（要件4.2） ----


chunk_strategy = st.builds(
    lambda key, body: {"documentId": f"s3://bucket/kb-source/sales/{key}/f.md",
                       "content": {"text": body}},
    st.text(alphabet="abcdef0123456789", min_size=1, max_size=4),
    text,
)


@given(st.lists(chunk_strategy, max_size=8), st.integers(min_value=1, max_value=6))
def test_duplicates_across_queries_are_collected_once(chunks, query_count):
    """**Validates: Requirements 4.2**"""
    once = collect_chunks(lambda q: chunks, ["q"])
    many = collect_chunks(lambda q: chunks, [f"q{i}" for i in range(query_count)])
    assert many == once
