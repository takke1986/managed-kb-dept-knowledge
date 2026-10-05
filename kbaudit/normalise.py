"""照合の前に、レイアウトの違いだけを消す（要件1）。"""

import unicodedata


def _strip_whitespace(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())


def normalise(text: str) -> str:
    """NFKC をかけ、空白（str.isspace が真の文字）をすべて除く。結果が変わらなくなるまで繰り返す。

    NFKC は全角の英数字（１２ → 12）や互換文字を寄せる。空白を除くのは、
    チャンク分割で行が組み直されるため。どちらも見た目の違いで、内容の違いではない。

    NFKC を1回かけてから空白を除くだけでは、結果が安定しない。単独の濁点 U+309B は
    NFKC で「空白 + 結合濁点 U+3099」に分解されるので、空白を除いたあとに結合濁点が
    前の文字に直接続き、もう一度 NFKC をかけると「か゛」が「が」に合成される。
    同じように、文字と結合記号の間に改行があると NFKC は合成できないので、
    改行の有無で結果が変わってしまう。

    そこで、まず空白を除いてから NFKC をかける。こうすると挟まった空白が合成を妨げない。
    NFKC が新たに空白を生むことがある（U+309B の分解など）ので、そのあとでも空白を除き、
    結果が変わらなくなるまで繰り返す。2回目以降の NFKC は除いた空白の両側を合成する
    だけで文字列は短くなる一方なので、繰り返しは必ず止まる。
    """
    current = _strip_whitespace(text)
    while True:
        folded = _strip_whitespace(unicodedata.normalize("NFKC", current))
        if folded == current:
            return current
        current = folded
