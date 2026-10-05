"""チャンクを判定して結果をまとめる。AWS には触れず、取得の関数を受け取る。"""

from collections import Counter
from collections.abc import Callable, Iterable

from kbaudit.collect import chunk_id, chunk_text, collect_chunks, department_of, source_key_for
from kbaudit.matching import Result, Verdict, classify
from kbaudit.spans import diff_spans

ISOLATION = "isolation"


def audit_chunks(
    department: str,
    chunks: Iterable[dict],
    read_source: Callable[[str], str | None],
) -> list[Result]:
    """チャンクを1つずつ判定する。

    他部署の Markdown から来たチャンクは、元を読みにも行かない（要件5.1・5.2）。
    監査のために他部署の本文を手元へ持ち込むこと自体を避ける。
    """
    cache: dict[str, str | None] = {}
    results: list[Result] = []
    for chunk in chunks:
        text = chunk_text(chunk)
        key = source_key_for(chunk)
        cid = chunk_id(key, text)
        metadata = chunk.get("metadata") or {}

        if key is not None and department_of(key) != department:
            results.append(Result(cid, Verdict.UNMATCHED, key, reason=ISOLATION))
            continue

        if key is None:
            source = None
            reason = "元の Markdown の場所が分からない"
        else:
            if key not in cache:
                cache[key] = read_source(key)
            source = cache[key]
            reason = "" if source is not None else "元の Markdown を読めない"

        verdict = classify(text, metadata, source)
        spans = tuple(diff_spans(text, source)) if verdict is Verdict.ALTERED and source else ()
        results.append(Result(cid, verdict, key, reason=reason if verdict is Verdict.UNMATCHED else "", spans=spans))
    return results


def run_audit(
    department: str,
    retrieve: Callable[[str], Iterable[dict]],
    read_source: Callable[[str], str | None],
    queries: Iterable[str],
) -> list[Result]:
    return audit_chunks(department, collect_chunks(retrieve, queries), read_source)


def summarise(results: list[Result]) -> dict[str, int]:
    counts = Counter(r.verdict.value for r in results)
    return {"audited": len(results), **{v.value: counts.get(v.value, 0) for v in Verdict}}


def failed(results: list[Result]) -> bool:
    """書き換えか、照合できなかったものが1つでもあれば失敗（要件4.5）。"""
    return any(r.verdict in (Verdict.ALTERED, Verdict.UNMATCHED) for r in results)
