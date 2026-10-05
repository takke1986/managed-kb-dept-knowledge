"""ファイル置き場のファイルを書き起こし直す（モデル・プロンプト・読み方を変えたとき）。

各ファイルのマルウェア検査の結果（GuardDuty のタグ）を読み、「検査が終わった」イベントを
変換の列（SQS）に入れ直す。ファイルを置き直さないので、マルウェアの検査はやり直さない。
脅威が見つかったもの・まだ検査が終わっていないものは入れない。人が直したタグは残る。

    .venv/bin/python scripts/reconvert.py                 すべての部署
    .venv/bin/python scripts/reconvert.py sales           部署を指定
    .venv/bin/python scripts/reconvert.py sales/規程/     フォルダを指定
    .venv/bin/python scripts/reconvert.py --dry-run ...   入れ直すものを数えるだけ
    .venv/bin/python scripts/reconvert.py --discard-edits ...   人が直した書き起こしも捨てて、やり直す
    .venv/bin/python scripts/reconvert.py --include-unscanned ...
                                                          マルウェア検査を入れる前から置いてあるもの（未検査）も入れる
"""

import json
import sys

import boto3

from project import CONFIG, REGION, outputs

OUT = outputs()

s3 = boto3.client("s3", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)


def queue_url() -> str:
    for url in sqs.list_queues(QueueNamePrefix="ManagedKbPrototype-ConvertQueue").get("QueueUrls", []):
        return url
    raise RuntimeError("変換の列が見つからない")


def main(args: list[str]) -> None:
    dry = "--dry-run" in args
    unscanned = "--include-unscanned" in args
    discard = "--discard-edits" in args
    targets = [a for a in args if not a.startswith("--")] or [d["id"] + "/" for d in CONFIG["departments"]]
    targets = [t if "/" in t else t + "/" for t in targets]
    url = None if dry else queue_url()
    counts = {"queued": 0, "threats": 0, "not_scanned": 0, "folders": 0}
    for prefix in targets:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=OUT["FilesBucket"], Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    counts["folders"] += 1
                    continue
                head = s3.head_object(Bucket=OUT["FilesBucket"], Key=key)
                tags = {t["Key"]: t["Value"] for t in
                        s3.get_object_tagging(Bucket=OUT["FilesBucket"], Key=key)["TagSet"]}
                status = tags.get("GuardDutyMalwareScanStatus", "")
                if status == "THREATS_FOUND":
                    counts["threats"] += 1
                    continue
                if not status and not unscanned:
                    counts["not_scanned"] += 1
                    continue
                if not dry:
                    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({
                        "detail-type": "GuardDuty Malware Protection Object Scan Result",
                        "detail": {"s3ObjectDetails": {"bucketName": OUT["FilesBucket"], "objectKey": key,
                                                       "versionId": head.get("VersionId")},
                                   "scanResultDetails": {"scanResultStatus": status}},
                        "reconvert": True, "discardEdits": discard,
                    }, ensure_ascii=False))
                counts["queued"] += 1
    label = "入れ直す（予定）" if dry else "入れ直した"
    print(f"{label}: {counts['queued']} 件 / 脅威あり（入れない）: {counts['threats']} 件 / "
          f"未検査（入れない）: {counts['not_scanned']} 件")


if __name__ == "__main__":
    main(sys.argv[1:])
