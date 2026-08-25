"""首次授权脚本:用 OAuth2 + PKCE 方式登录 Xero,把 refresh token 保存到本地。

只需要在第一次使用、或者超过 60 天没运行过导致 refresh token 过期时运行一次:
    python xero_auth.py

之后 main.py 每次运行都会用保存的 refresh token 自动续期 access token,不用再
重复这一步、也不用付费开 Custom Connection。
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv

from xero_client import TOKEN_FILE, save_tokens

load_dotenv()

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
# offline_access 是拿 refresh token 的关键 scope。
# 2026-04-29 起 Xero 把粗粒度的 accounting.transactions 拆成了更细的 scope,
# 新建的 App 只能用细分后的名字,这里用 accounting.invoices(建/改发票)+
# accounting.contacts(建/查客户联系人)。
SCOPES = "openid profile email offline_access accounting.invoices accounting.contacts"


def _make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def main() -> None:
    client_id = os.environ.get("XERO_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "没有找到 XERO_CLIENT_ID。请先复制 .env.example 为 .env,"
            "按 README.md 的步骤在 Xero Developer 后台建一个 App 并填入 Client ID。"
        )
    redirect_uri = os.environ.get("XERO_REDIRECT_URI", "http://localhost:8765/callback")
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 8765

    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = qs.get("code", [""])[0]
            result["state"] = qs.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>授权完成,可以关闭这个页面回到终端了。</h2>".encode())

        def log_message(self, *args):  # 静默默认的访问日志
            return

    server = http.server.HTTPServer(("localhost", port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"正在打开浏览器进行 Xero 授权,如果没有自动打开,请手动访问:\n{auth_url}\n")
    webbrowser.open(auth_url)
    thread.join(timeout=300)

    if not result.get("code"):
        raise SystemExit("没有收到授权码(超时或被取消了),请重新运行")
    if result.get("state") != state:
        raise SystemExit("state 不匹配,可能存在异常跳转,已中止授权")

    token_resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    conn_resp = requests.get(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=30,
    )
    conn_resp.raise_for_status()
    connections = conn_resp.json()
    if not connections:
        raise SystemExit("这个 Xero 账号下没有可用的组织(tenant),请先在 Xero 里创建/选择一个组织再试")

    tenant_id = connections[0]["tenantId"]
    tenant_name = connections[0].get("tenantName", "")
    save_tokens(tokens, tenant_id)

    print(f"✅ 授权成功,已连接到 Xero 组织:{tenant_name} ({tenant_id})")
    print(f"   token 已保存到 {TOKEN_FILE.name}(已加入 .gitignore,不会被提交)。")
    if len(connections) > 1:
        print("⚠️  这个账号下有多个 Xero 组织,默认用了第一个。如果不是你要的那个,")
        print("    去 .xero_tokens.json 里手动改 tenant_id,或联系我调整选择逻辑。")


if __name__ == "__main__":
    main()
