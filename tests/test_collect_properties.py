"""要件4（収集）の性質9を、生成した入力で確かめる。

同じチャンクは何度返っても1回だけ監査する（要件4.2）。KB は問い合わせが違っても同じ
チャンクを返すことがよくあるので、重複を除けないと件数の集計が問い合わせの数に比例して
膨らんでしまう。

チャンクの形は test_audit_cli.py の偽の検索と同じにしている。元のキーは documentId、
location.s3Location.uri（URL エンコードされた https）、metadata._source_uri のどれか
から読まれるので、どの形で来ても同じ扱いになることも合わせて試す。部署名とバケット名は
設定ファイルの値を使わず生成する。収集の処理は部署を区別しないので、実在の値に頼る理由が
無いからである。
"""

from urllib.parse import quote

from hypothesis import given
from hypothesis import strategies as st

from kbaudit.collect import collect_chunks

# 本文には日本語の文書に出てくる文字を中心に、Unicode 全体からの文字列も混ぜる。
# 重複の判定は本文をそのまま比べるので、正規化で同じになる別の文字列が別のチャンクとして
# 残ることもここで確かめられる。
japanese_char = st.one_of(
    st.characters(min_codepoint=0x3041, max_codepoint=0x309F),  # ひらがな・濁点
    st.characters(min_codepoint=0x30A0, max_codepoint=0x30FF),  # カタカナ
    st.characters(min_codepoint=0xFF01, max_codepoint=0xFF9F),  # 全角英数・半角カナ
    st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),  # 漢字
    st.characters(min_codepoint=0x3000, max_codepoint=0x303F),  # 和文の記号
    st.characters(min_codepoint=0x20, max_codepoint=0x7E),  # ASCII
)
text = st.one_of(st.text(japanese_char), st.text())

# キーの部品は S3 のキーとして実際に現れる形に寄せる。ファイル名は日本語にして、
# https の形では URL エンコードが外れることも通らせる。「/」「?」「#」は URL の区切りに
# なってキーの形そのものが変わるので、ファイル名には入れない。
segment = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6)
file_name = st.text(
    st.one_of(
        st.characters(min_codepoint=0x3041, max_codepoint=0x3096),  # ひらがな
        st.characters(min_codepoint=0x30A1, max_codepoint=0x30FA),  # カタカナ
        st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),  # 漢字
    ),
    min_size=1,
    max_size=6,
)
bucket = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=12)


@st.composite
def chunk_with_identity(draw):
    """チャンクと、重複の判定に使われるべき（元のキー, 本文）の組を作る。"""
    key = f"kb-source/{draw(segment)}/{draw(segment)}/{draw(file_name)}.md"
    body = draw(text)
    b = draw(bucket)
    # Gateway の応答で実際に見る3つの書き方から、少なくとも1つを入れる。
    forms = draw(st.sets(st.sampled_from(["documentId", "location", "_source_uri"]), min_size=1))
    chunk: dict = {"content": {"text": body, "type": "TEXT"}, "metadata": {}}
    if "documentId" in forms:
        chunk["documentId"] = f"s3://{b}/{key}"
    if "location" in forms:
        chunk["location"] = {"s3Location": {"uri": f"https://{b}.s3.ap-northeast-1.amazonaws.com/{quote(key)}"}}
    if "_source_uri" in forms:
        chunk["metadata"]["_source_uri"] = f"s3://{b}/{key}"
    return (key, body), chunk


# 「その一覧を1回だけ返す」が一意に決まるよう、一覧の中のチャンクは（元のキー, 本文）で
# 互いに異なるものにする。重複を含む一覧では、どれを残すかが性質の外の話になるからである。
distinct_chunks = st.lists(chunk_with_identity(), max_size=8, unique_by=lambda pair: pair[0]).map(
    lambda pairs: [c for _, c in pairs]
)


# ---- 性質9 同じチャンクが何度返っても1回だけ数える（要件4.2） ----
@given(distinct_chunks, st.integers(min_value=1, max_value=6))
def test_same_list_from_every_query_is_collected_once(chunks, query_count):
    """**Validates: Requirements 4.2**"""
    queries = [f"問い合わせ{i}" for i in range(query_count)]
    # 問い合わせの数によらず、元の一覧がそのままの順で1回だけ返ることを確かめる。
    # 1回目の結果との比較だけでは、1回目から取りこぼしている場合を見逃すので、元の一覧と比べる。
    assert collect_chunks(lambda q: chunks, queries) == chunks
