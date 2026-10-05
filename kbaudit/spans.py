"""書き換えられたチャンクの、違っている所だけを取り出す（要件3）。"""

from difflib import SequenceMatcher

from kbaudit.matching import Span
from kbaudit.normalise import normalise

MAX_SPAN = 40
MAX_SPANS = 5


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SPAN else text[: MAX_SPAN - 1] + "…"


def diff_spans(chunk_text: str, source_text: str, limit: int = MAX_SPANS) -> list[Span]:
    """原文の中でチャンクにいちばん近い箇所を探し、そことの違いを返す。

    800文字のチャンクを読ませるより「膳所営業所 → 陸所営業所」と見せる方が、
    人が判断するのに早い。部分文字列として含まれていれば違いは無いので空を返す。
    """
    chunk = normalise(chunk_text)
    source = normalise(source_text)
    if not chunk or chunk in source or limit <= 0:
        return []

    # いちばん長く一致する所を手がかりに、原文側の窓をチャンクの長さで取る
    anchor = SequenceMatcher(None, source, chunk, autojunk=False).find_longest_match(
        0, len(source), 0, len(chunk)
    )
    start = max(0, anchor.a - anchor.b)
    window = source[start : start + len(chunk)]

    spans: list[Span] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, window, chunk, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        spans.append(Span(source=_clip(window[i1:i2]), chunk=_clip(chunk[j1:j2])))
        if len(spans) >= limit:
            break
    return spans
