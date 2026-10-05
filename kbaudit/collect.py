"""検索で返ったチャンクを集め、元の Markdown のキーを決める（要件4・5）。"""

import hashlib
from collections.abc import Callable, Iterable
from urllib.parse import unquote, urlparse

from kbaudit.normalise import normalise

KB_SOURCE = "kb-source/"


def source_key_for(chunk: dict) -> str | None:
    """チャンクの元になった Markdown の S3 キー（kb-source/<部署>/<文書ID>/<名前>.md）。

    KB が索引したキーそのものを使う。Gateway の応答では documentId が
    s3://<バケット>/<キー>、location.s3Location.uri が URL エンコードされた
    https の形で入っている。どちらからでも同じキーになる。
    """
    candidates = [
        chunk.get("documentId"),
        (chunk.get("location") or {}).get("s3Location", {}).get("uri"),
        (chunk.get("metadata") or {}).get("_source_uri"),
    ]
    for value in candidates:
        if not value:
            continue
        path = unquote(urlparse(str(value)).path).lstrip("/")
        # s3://bucket/key は path がキー、https://bucket.s3.../key も path がキー
        index = path.find(KB_SOURCE)
        if index >= 0:
            return path[index:]
    return None


def department_of(source_key: str) -> str | None:
    """kb-source/<部署>/... の <部署>。形が違えば None。"""
    parts = source_key.split("/")
    if len(parts) < 4 or parts[0] != KB_SOURCE.rstrip("/"):
        return None
    return parts[1]


def chunk_text(chunk: dict) -> str:
    return str((chunk.get("content") or {}).get("text", ""))


def chunk_id(source_key: str | None, text: str) -> str:
    """元のキーと、正規化した本文のハッシュ。実行をまたいで同じになる。"""
    digest = hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()[:8]
    return f"{source_key or '?'}#{digest}"


def collect_chunks(retrieve: Callable[[str], Iterable[dict]], queries: Iterable[str]) -> list[dict]:
    """問い合わせごとに検索し、同じチャンクは1回だけ残す。

    同じチャンクが複数の問い合わせで返るのはよくあること。元のキーと本文の組で
    重複を除く。並びは最初に返った順を保つ。
    """
    seen: set[tuple[str | None, str]] = set()
    collected: list[dict] = []
    for query in queries:
        for chunk in retrieve(query):
            key = (source_key_for(chunk), chunk_text(chunk))
            if key in seen:
                continue
            seen.add(key)
            collected.append(chunk)
    return collected
