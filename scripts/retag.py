"""既存の文書のタグを、フォルダ名＋一括で足したタグで付け直す（生成AIは使わない）。
タグの付け方を変えたときに一度流す。書き起こしはやり直さない。

    uv run --no-project --with 'boto3>=1.43.36' python scripts/retag.py
"""

import boto3

from project import REGION, outputs

OUT = outputs()
lam = boto3.client("lambda", region_name=REGION)
res = lam.invoke(FunctionName=OUT["RetagFunction"], Payload=b"{}")
print(res["Payload"].read().decode())
