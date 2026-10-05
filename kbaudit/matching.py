"""チャンクごとの判定（要件2）。"""

from dataclasses import dataclass, field
from enum import StrEnum

from kbaudit.normalise import normalise


class Verdict(StrEnum):
    VERBATIM = "verbatim"    # 渡した Markdown の一部そのもの
    ALTERED = "altered"      # 一部ではない。書き換えられている
    SKIPPED = "skipped"      # 画像の説明、または中身が空
    UNMATCHED = "unmatched"  # 元の Markdown が無い・読めない・他部署のもの


@dataclass(frozen=True)
class Span:
    """違っていた所。両側とも40文字まで（要件3）。"""

    source: str
    chunk: str


@dataclass(frozen=True)
class Result:
    chunk_id: str
    verdict: Verdict
    source_key: str | None
    reason: str = ""
    spans: tuple[Span, ...] = field(default=())


def is_image_chunk(metadata: dict) -> bool:
    """図の説明として生成されたチャンク。原文に無いのが当たり前なので照合しない。"""
    return str((metadata or {}).get("_media_type", "")).lower() == "image"


def classify(chunk_text: str, metadata: dict, source_text: str | None) -> Verdict:
    """判定する。順番は 画像 → 空 → 元が無い → 部分文字列。

    VERBATIM を返せるのは部分文字列の判定だけ。似ている度合いでは判定しない。
    95% 似ていても、肝心の数字1つが変わっていることがあるため。
    """
    if is_image_chunk(metadata):
        return Verdict.SKIPPED
    chunk = normalise(chunk_text)
    if not chunk:
        return Verdict.SKIPPED
    if source_text is None:
        return Verdict.UNMATCHED
    return Verdict.VERBATIM if chunk in normalise(source_text) else Verdict.ALTERED
