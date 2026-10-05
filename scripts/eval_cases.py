"""答えの質を測る評価セット（scripts/eval_rag.py が使う）。

各ケース:
    user     誰として聞くか（sales・legal・both）
    q        質問
    expect   答えに必ず入るべき文字列（全角・半角・漢数字・空白の差は吸収して比べる）
    source   検索で届くべき元ファイルの名前（どれか1つに届けばよい）
    kind     answer（答えられる）・abstain（文書に無いので控えるべき）・no_leak（他部署の中身を出さない）
    filter   タグで絞り込むべき問題で、絞り込みの条件に入っているべきタグ（どれか1つ）
    no_filter  絞り込むべきでない問題で、絞り込みに使ってはいけないタグ（質問の文字に引っ張られた行き過ぎを見る）
    forbid   答えに出てはいけない文字列（no_leak で使う）

abstain と no_leak は、答えに控える言い回し（「見つかりませんでした」など）があることも見る。

文書は、厚生労働省のモデル就業規則（公開文書。docx と PDF の両方を法務部に置く）と、
確認用の架空の書類（scripts/verify.py と make_office_fixtures.py が置くもの）。
就業規則の8問は、Managed KB のパースを検証したときに使ったものと同じ。
"""

RULES = ("モデル就業規則.docx", "モデル就業規則.pdf")
# タグで絞り込む問題のために、setup で就業規則を legal/規程/人事/ にも置き、見積に追加のタグ「2026年度」を付ける

CASES = [
    # ---- モデル就業規則（実在の日本語の長い規程） ----
    {"id": "rules-hours", "user": "legal", "kind": "answer", "source": RULES,
     "q": "1週間および1日の労働時間は、それぞれ何時間と定められていますか。", "expect": ["40", "8"]},
    {"id": "rules-leave", "user": "legal", "kind": "answer", "source": RULES,
     "q": "入社から6か月間継続勤務し、所定労働日の8割以上出勤した労働者に与えられる年次有給休暇は何日ですか。",
     "expect": ["10"]},
    {"id": "rules-reduction-once", "user": "legal", "kind": "answer", "source": RULES,
     "q": "減給の制裁について、1回の額の上限はどのように定められていますか。", "expect": ["平均賃金", "1日分", "5割"]},
    {"id": "rules-reduction-total", "user": "legal", "kind": "answer", "source": RULES,
     "q": "減給の総額は、1賃金支払期における賃金総額の何割を超えないと定められていますか。", "expect": ["1割"]},
    {"id": "rules-dismissal-allowance", "user": "legal", "kind": "answer", "source": RULES,
     "q": "懲戒解雇の場合に支給される解雇予告手当は、平均賃金の何日分ですか。", "expect": ["30"]},
    {"id": "rules-discipline-kinds", "user": "legal", "kind": "answer", "source": RULES,
     "q": "懲戒の区分として定められている4つの種類を挙げてください。",
     "expect": ["けん責", "減給", "出勤停止", "懲戒解雇"]},
    {"id": "rules-calendar-day", "user": "legal", "kind": "answer", "source": RULES,
     "q": "休日の起算について、暦日とは何時から何時までの継続何時間を指しますか。", "expect": ["0", "24"]},
    {"id": "rules-flex-month", "user": "legal", "kind": "answer", "source": RULES,
     "q": "1か月単位の変形労働時間制の例では、1日の所定労働時間は何時間何分ですか。", "expect": ["7時間15分"]},

    # ---- 架空の書類（Office の XML・画像・スキャン PDF の経路） ----
    {"id": "office-textbox", "user": "sales", "kind": "answer", "source": ("報告書-図とテキストボックス.docx",),
     "q": "第3四半期の活動報告で、納期はいつと注記されていますか。", "expect": ["10月15日"]},
    {"id": "office-image-in-docx", "user": "sales", "kind": "answer", "source": ("報告書-図とテキストボックス.docx",),
     "q": "第3四半期の活動報告の図に示されている受注金額はいくらですか。", "expect": ["482万"]},
    {"id": "office-table-docx", "user": "sales", "kind": "answer", "source": ("報告書-図とテキストボックス.docx",),
     "q": "第3四半期の東日本エリアの受注件数は何件ですか。", "expect": ["42"]},
    {"id": "office-chart-pptx", "user": "sales", "kind": "answer", "source": ("提案書-グラフとフロー.pptx",),
     "q": "新サービスの提案書で、5月の問い合わせ件数は何件ですか。", "expect": ["135"]},
    {"id": "office-flow-pptx", "user": "sales", "kind": "answer", "source": ("提案書-グラフとフロー.pptx",),
     "q": "提案書の承認の流れでは、申請の次は何ですか。", "expect": ["承認"]},
    {"id": "office-notes-pptx", "user": "sales", "kind": "answer", "source": ("提案書-グラフとフロー.pptx",),
     "q": "新サービスの提案で、価格はどのように説明することになっていますか。", "expect": ["税抜"]},
    {"id": "office-xlsx-value", "user": "sales", "kind": "answer", "source": ("売上集計-グラフ付き.xlsx",),
     "q": "売上集計表で、大阪支店の売上はいくらですか。", "expect": ["2,450"]},
    {"id": "office-xlsx-image", "user": "sales", "kind": "answer", "source": ("売上集計-グラフ付き.xlsx",),
     "q": "売上集計の締め日は何日ですか。", "expect": ["25日"]},
    {"id": "scan-estimate", "user": "sales", "kind": "answer",
     "source": ("スキャン-見積書.pdf", "見積-整合.xlsx", "見積書の写真.png"),
     "q": "見積番号 Q-2026-0901 の合計金額（税込）はいくらですか。", "expect": ["110,000"], "no_filter": ["2026年度"]},
    {"id": "scan-estimate-expiry", "user": "sales", "kind": "answer",
     "source": ("スキャン-見積書.pdf", "見積-整合.xlsx", "見積書の写真.png"),
     "q": "見積番号 Q-2026-0901 の有効期限はいつですか。", "expect": ["10月31日"], "no_filter": ["2026年度"]},
    {"id": "contract-anti", "user": "legal", "kind": "answer", "source": ("契約書-反社あり.docx",),
     "q": "業務委託契約書で、反社会的勢力に該当した場合はどうなると定められていますか。", "expect": ["解除"]},

    # ---- タグで絞り込む（フォルダ名のタグ・追加のタグ） ----
    {"id": "tag-folder", "user": "legal", "kind": "answer", "source": RULES, "filter": ["人事", "規程"],
     "q": "人事の規程フォルダにある就業規則で、懲戒の種類を4つ挙げてください。",
     "expect": ["けん責", "減給", "出勤停止", "懲戒解雇"]},
    {"id": "tag-extra", "user": "sales", "kind": "answer", "source": ("見積-整合.xlsx",), "filter": ["2026年度"],
     "q": "「2026年度」のタグが付いた見積の、合計金額（税込）はいくらですか。", "expect": ["110,000"]},

    # ---- 文書に無いこと（作り話をしない） ----
    {"id": "abstain-parking", "user": "legal", "kind": "abstain", "source": (),
     "q": "社員用の駐車場の月額料金はいくらですか。", "expect": []},
    {"id": "abstain-q4", "user": "sales", "kind": "abstain", "source": (),
     "q": "第4四半期の西日本エリアの受注件数は何件ですか。", "expect": []},

    # ---- 他部署の中身を出さない ----
    {"id": "no-leak-contract", "user": "sales", "kind": "no_leak", "source": (),
     "q": "業務委託契約書の反社会的勢力の排除条項には、何と書かれていますか。", "expect": [],
     "forbid": ["催告なく", "暴力団"]},
    {"id": "no-leak-rules", "user": "sales", "kind": "no_leak", "source": (),
     "q": "就業規則で、懲戒解雇の場合の解雇予告手当は平均賃金の何日分ですか。", "expect": [],
     "forbid": ["30日分"]},
]
