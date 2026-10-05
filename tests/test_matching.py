"""要件1・2・3。実際に測った書き換えをそのまま例にしている。"""

from kbaudit.matching import Verdict, classify
from kbaudit.normalise import normalise
from kbaudit.spans import MAX_SPAN, diff_spans

SOURCE = (
    "（年次有給休暇）\n第２３条　採用日から６か月間継続勤務し、所定労働日の８割以上出勤した"
    "労働者に対しては、１０日の年次有給休暇を与える。\n"
    "納品先は 膳所営業所、製品は 彩雲シリーズ とする。\n"
    "（２）過去○年間の出勤率が○％以上の者"
)


# ---- 要件1 正規化 ----


def test_full_width_digits_and_letters_fold_to_ascii():
    assert normalise("第２３条 ＡＢＣ") == "第23条ABC"


def test_all_whitespace_is_removed():
    assert normalise("一\u3000二\n三\t四 五") == "一二三四五"


def test_normalising_twice_changes_nothing():
    once = normalise(SOURCE)
    assert normalise(once) == once


# ---- 要件2 判定 ----


def test_excerpt_reflowed_onto_new_lines_is_verbatim():
    chunk = "採用日から6か月間継続勤務し、\n所定労働日の8割以上出勤した労働者に対しては、10日の"
    assert classify(chunk, {}, SOURCE) is Verdict.VERBATIM


def test_proper_noun_substitution_is_altered():
    # 2回目の解析で実際に起きた置き換え
    assert classify("納品先は 陸所営業所、製品は 彩雲シリーズ とする。", {}, SOURCE) is Verdict.ALTERED


def test_blank_becoming_zero_is_altered():
    assert classify("過去○年間の出勤率が0％以上の者", {}, SOURCE) is Verdict.ALTERED


def test_one_changed_digit_is_altered_despite_high_similarity():
    assert classify("１１日の年次有給休暇を与える。", {}, SOURCE) is Verdict.ALTERED


def test_image_chunk_is_skipped_even_though_text_is_not_in_source():
    meta = {"_media_type": "image"}
    assert classify("The diagram uses color coding", meta, SOURCE) is Verdict.SKIPPED


def test_empty_chunk_is_skipped():
    assert classify(" \n\u3000", {}, SOURCE) is Verdict.SKIPPED


def test_missing_source_is_unmatched():
    assert classify("何か", {}, None) is Verdict.UNMATCHED


# ---- 要件3 差分 ----


def test_substitution_is_reported_as_the_changed_pair():
    spans = diff_spans("納品先は 陸所営業所、製品は 彩雲シリーズ とする。", SOURCE)
    assert [(s.source, s.chunk) for s in spans] == [("膳", "陸")]


def test_verbatim_chunk_has_no_spans():
    assert diff_spans("製品は 彩雲シリーズ", SOURCE) == []


def test_spans_are_bounded():
    chunk = "あ" * 300 + "い" * 300
    spans = diff_spans(chunk, SOURCE, limit=5)
    assert len(spans) <= 5
    assert all(len(s.source) <= MAX_SPAN and len(s.chunk) <= MAX_SPAN for s in spans)
