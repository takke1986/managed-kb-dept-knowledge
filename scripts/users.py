"""利用者を作る・部署を付け外しする。変えたあとは ACL を書き直す（acl_sync を呼ぶ）。

    python scripts/users.py create <email> <部署>...   作る（パスワードは自動で作り、ファイルに残す）
    python scripts/users.py join <email> <部署>        部署に入れる
    python scripts/users.py leave <email> <部署>       部署から外す
    python scripts/users.py disable <email>            無効にする（退職など）
    python scripts/users.py admin <email>              管理者にする（タグの一覧を変えられる）
    python scripts/users.py test-accounts              確認用の3人（営業・法務・兼務）を作る

パスワードは画面に出さず、~/managed-kb-test-accounts.txt（600。MKB_ACCOUNTS_FILE で変えられる）に書く。
"""

import json
import os
import secrets
import string
import sys

import boto3

from project import ACCOUNTS_FILE, REGION, outputs

OUTPUTS = outputs()
POOL = OUTPUTS["UserPoolId"]
TEST_ACCOUNTS = {
    "mkb-sales@example.com": ["sales"],
    "mkb-legal@example.com": ["legal"],
    "mkb-both@example.com": ["sales", "legal"],
}

cognito = boto3.client("cognito-idp", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)


def password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20)) + "a1A!"


def save_password(email: str, pw: str) -> None:
    lines = [l for l in (ACCOUNTS_FILE.read_text().splitlines() if ACCOUNTS_FILE.exists() else [])
             if not l.startswith(email + " ")]
    lines.append(f"{email} {pw}")
    ACCOUNTS_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ACCOUNTS_FILE, 0o600)


def create(email: str, departments: list[str]) -> None:
    try:
        cognito.admin_create_user(UserPoolId=POOL, Username=email, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": email},
                                                  {"Name": "email_verified", "Value": "true"}])
    except cognito.exceptions.UsernameExistsException:
        print(f"{email}: 既にある")
    pw = password()
    cognito.admin_set_user_password(UserPoolId=POOL, Username=email, Password=pw, Permanent=True)
    save_password(email, pw)
    for d in departments:
        cognito.admin_add_user_to_group(UserPoolId=POOL, Username=email, GroupName=d)
    print(f"{email}: 作成（部署 {', '.join(departments)}、パスワードは {ACCOUNTS_FILE}）")


def sync_acl(departments: list[str] | None = None) -> None:
    res = lam.invoke(FunctionName=OUTPUTS["AclSyncFunction"],
                     Payload=json.dumps({"departments": departments} if departments else {}).encode())
    print("ACL を書き直した:", res["Payload"].read().decode())


def main(argv: list[str]) -> None:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "create" and len(args) >= 2:
        create(args[0], args[1:])
        sync_acl(args[1:])
    elif cmd == "join" and len(args) == 2:
        cognito.admin_add_user_to_group(UserPoolId=POOL, Username=args[0], GroupName=args[1])
        sync_acl([args[1]])
    elif cmd == "leave" and len(args) == 2:
        cognito.admin_remove_user_from_group(UserPoolId=POOL, Username=args[0], GroupName=args[1])
        sync_acl([args[1]])
    elif cmd == "disable" and len(args) == 1:
        cognito.admin_disable_user(UserPoolId=POOL, Username=args[0])
        cognito.admin_user_global_sign_out(UserPoolId=POOL, Username=args[0])
        sync_acl()
    elif cmd == "admin" and len(args) == 1:
        cognito.admin_add_user_to_group(UserPoolId=POOL, Username=args[0], GroupName="admins")
        print(f"{args[0]}: 管理者にした")
    elif cmd == "test-accounts":
        for email, depts in TEST_ACCOUNTS.items():
            create(email, depts)
        cognito.admin_add_user_to_group(UserPoolId=POOL, Username="mkb-both@example.com", GroupName="admins")
        sync_acl()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
