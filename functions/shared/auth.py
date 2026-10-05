"""Cognito のアクセストークンを検証して、利用者のメールアドレスと部署を取り出す。"""

import os
from dataclasses import dataclass

import jwt

from common import DEPARTMENTS

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{os.environ.get('USER_POOL_ID', '')}"
CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
_jwks = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json", cache_keys=True)


class Unauthorized(Exception):
    pass


@dataclass(frozen=True)
class User:
    sub: str
    username: str
    email: str
    departments: tuple[str, ...]
    admin: bool = False  # タグの一覧を変えられる（Cognito の admins グループ）


def app_token(headers: dict) -> str:
    """画面の API に渡されたトークン。CloudFront（OAC）の後ろにあるので、Authorization ヘッダーは
    OAC の署名に使われる。利用者のトークンは x-app-token で受け取る。"""
    value = next((v for k, v in (headers or {}).items() if k.lower() == "x-app-token"), "")
    if not value:
        raise Unauthorized("x-app-token ヘッダーが無い")
    return value.strip()


def bearer_token(headers: dict) -> str:
    value = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), "")
    if not value.lower().startswith("bearer "):
        raise Unauthorized("Authorization ヘッダーが無い")
    return value[7:].strip()


def verify(token: str) -> User:
    """署名・発行元・有効期限・種類（access）・クライアントを確かめる。"""
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER,
                            options={"require": ["exp", "iss", "sub", "token_use", "client_id"]})
    except jwt.PyJWTError as e:
        raise Unauthorized(f"トークンが正しくない: {e}") from e
    if claims["token_use"] != "access" or claims["client_id"] != CLIENT_ID:
        raise Unauthorized("このアプリのアクセストークンではない")
    email = str(claims.get("email", "")).lower()
    if not email:
        raise Unauthorized("トークンに email が無い")
    departments = tuple(g for g in claims.get("cognito:groups", []) if g in DEPARTMENTS)
    return User(claims["sub"], str(claims.get("username", claims["sub"])), email, departments)
