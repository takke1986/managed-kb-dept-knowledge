"""アクセストークンにメールアドレスを入れる（Cognito の Pre Token Generation V2）。

Gateway の Interceptor と Cedar のポリシーは、トークンの email クレームで利用者を識別する。
Cognito のアクセストークンには既定で email が入らないため、ここで足す。
"""


def handler(event, context):
    email = event["request"]["userAttributes"].get("email", "").lower()
    if email:
        event["response"]["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": {"claimsToAddOrOverride": {"email": email}},
        }
    return event
