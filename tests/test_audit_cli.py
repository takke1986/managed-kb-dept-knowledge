"""要件4・5。AWS の代わりに偽の検索と読み出しを渡す。"""

import io
import json

import pytest

import audit_ingestion as cli
from kbaudit.collect import collect_chunks, source_key_for
from kbaudit.matching import Verdict
from kbaudit.report import ISOLATION, run_audit

BUCKET = "docs-bucket"
SALES_KEY = "kb-source/sales/abc/見積書.pdf.md"
LEGAL_KEY = "kb-source/legal/def/契約書.docx.md"
SOURCES = {SALES_KEY: "見積書\n合計金額は 1,200,000 円（税込）とする。", LEGAL_KEY: "秘密保持の条項"}


def chunk(key: str, text: str, **meta) -> dict:
    return {"content": {"text": text, "type": "TEXT"},
            "documentId": f"s3://{BUCKET}/{key}",
            "location": {"s3Location": {"uri": f"https://{BUCKET}.s3.ap-northeast-1.amazonaws.com/{key}"}},
            "metadata": meta}


def test_source_key_comes_from_document_id():
    assert source_key_for(chunk(SALES_KEY, "x")) == SALES_KEY


def test_source_key_decodes_the_https_uri():
    c = {"location": {"s3Location": {"uri": f"https://{BUCKET}.s3.ap-northeast-1.amazonaws.com/"
                                            "kb-source/sales/abc/%E8%A6%8B%E7%A9%8D%E6%9B%B8.pdf.md"}}}
    assert source_key_for(c) == SALES_KEY


def test_same_chunk_from_several_queries_is_audited_once():
    same = [chunk(SALES_KEY, "合計金額は 1,200,000 円")]
    assert len(collect_chunks(lambda q: same, ["a", "b", "c"])) == 1


def test_chunk_from_another_department_is_an_isolation_failure_and_its_source_is_not_read():
    read = []

    def read_source(key):
        read.append(key)
        return SOURCES.get(key)

    results = run_audit("sales", lambda q: [chunk(LEGAL_KEY, "秘密保持の条項")], read_source, ["q"])
    assert [(r.verdict, r.reason) for r in results] == [(Verdict.UNMATCHED, ISOLATION)]
    assert read == []


def test_unknown_department_exits_2_before_any_aws_call():
    def no_aws():
        raise AssertionError("AWS に触れてはいけない")

    assert cli.main(["accounting"], aws=no_aws) == 2


def run_cli(chunks, tmp_path, department="sales"):
    out = tmp_path / "result.json"
    status = cli.main([department, "--json", str(out)], retrieve=lambda q: chunks,
                      read_source=SOURCES.get, queries=["q"])
    return status, json.loads(out.read_text(encoding="utf-8"))


def test_all_verbatim_exits_0(tmp_path):
    status, rows = run_cli([chunk(SALES_KEY, "合計金額は 1,200,000 円（税込）")], tmp_path)
    assert status == 0
    assert [r["verdict"] for r in rows] == ["verbatim"]


def test_one_altered_chunk_exits_1_and_reports_the_span(tmp_path):
    status, rows = run_cli([chunk(SALES_KEY, "合計金額は 1,300,000 円（税込）")], tmp_path)
    assert status == 1
    assert rows[0]["verdict"] == "altered"
    assert rows[0]["spans"] == [{"source": "2", "chunk": "3"}]


def test_isolation_failure_never_prints_the_chunk_text(capsys):
    cli.main(["sales"], retrieve=lambda q: [chunk(LEGAL_KEY, "秘密保持の条項")],
             read_source=SOURCES.get, queries=["q"])
    assert "秘密保持" not in capsys.readouterr().out


@pytest.mark.parametrize("meta,expected", [({"_media_type": "image"}, "skipped"), ({}, "altered")])
def test_image_chunks_do_not_fail_the_audit(tmp_path, meta, expected):
    _, rows = run_cli([chunk(SALES_KEY, "The diagram shows totals", **meta)], tmp_path)
    assert rows[0]["verdict"] == expected


def test_report_summary_line():
    buf = io.StringIO()
    cli.report(run_audit("sales", lambda q: [chunk(SALES_KEY, "見積書")], SOURCES.get, ["q"]), out=buf)
    assert "audited 1  verbatim 1  altered 0  skipped 0  unmatched 0" in buf.getvalue()
