"""照合の前に、レイアウトの違いだけを消す（要件1）。"""

import unicodedata


def normalise(text: str) -> str:
    """NFKC をかけてから、空白（str.isspace が真の文字）をすべて除く。

    NFKC は全角の英数字（１２ → 12）や互換文字を寄せる。空白を除くのは、
    チャンク分割で行が組み直されるため。どちらも見た目の違いで、内容の違いではない。

    NFKC のあとに空白を除く順番には意味がある。NFKC は全角の空白を半角に寄せる
    ことがあるので、先に除くと取りこぼす。
    """
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if not ch.isspace())
