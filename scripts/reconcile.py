"""台帳・ファイル置き場・書き起こしの突き合わせを今すぐ動かす（普段は毎日 3:00 に動く）。
台帳を入れる前からある文書は、これで台帳に登録される（移行）。

    uv run --no-project --with 'boto3>=1.43.36' python scripts/reconcile.py
"""

import boto3

from project import REGION, outputs

OUT = outputs()
res = boto3.client("lambda", region_name=REGION).invoke(FunctionName=OUT["ReconcileFunction"], Payload=b"{}")
print(res["Payload"].read().decode())
