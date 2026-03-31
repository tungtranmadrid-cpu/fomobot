"""
Lấy OAuth refresh token cho Google Calendar (readonly) — chạy một lần trên máy bạn.

Mặc định dùng OAuth client kiểu Web application (Google Cloud → Credentials → Web client).

Trước đó:
  - Bật Google Calendar API; OAuth consent screen (Test user nếu internal).
  - Tạo OAuth client kiểu "Web application".
  - Authorized redirect URIs: thêm ĐÚNG URI trong .env GOOGLE_OAUTH_REDIRECT_URI
    (mặc định http://127.0.0.1:8085/ — phải trùng ký tự với Console).

Chạy:  python get_gcal_refresh_token.py

Dán refresh token in ra vào:
  - .env → GCAL_MASTER_REFRESH_TOKEN (chế độ lịch tập trung / master), hoặc
  - Supabase bảng user, cột gcal_refresh_token (dòng đúng telegram_ID, chế độ OAuth từng user).

Tuỳ chọn: GOOGLE_OAUTH_CLIENT_KIND=installed — dùng luồng "Desktop" cũ (redirect động, port ngẫu nhiên).
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

DEFAULT_WEB_REDIRECT = "http://127.0.0.1:8085/"


def main() -> None:
    cid = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    csec = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    if not cid or not csec:
        print("Thiếu GOOGLE_OAUTH_CLIENT_ID hoặc GOOGLE_OAUTH_CLIENT_SECRET trong .env", file=sys.stderr)
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Cài: pip install google-auth-oauthlib", file=sys.stderr)
        sys.exit(1)

    kind = (os.getenv("GOOGLE_OAUTH_CLIENT_KIND") or "web").strip().lower()

    if kind == "installed":
        client_config = {
            "installed": {
                "client_id": cid,
                "client_secret": csec,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost", "http://127.0.0.1"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    else:
        redirect_uri = (os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or DEFAULT_WEB_REDIRECT).strip()
        if not redirect_uri.endswith("/"):
            redirect_uri = redirect_uri + "/"
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http":
            print(
                "GOOGLE_OAUTH_REDIRECT_URI phải là http:// (ví dụ http://127.0.0.1:8085/) "
                "để script mở server local. Web app production (https) cần endpoint callback riêng.",
                file=sys.stderr,
            )
            sys.exit(1)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        if port is None:
            print("Thiếu cổng trong URL (ví dụ http://127.0.0.1:8085/).", file=sys.stderr)
            sys.exit(1)

        client_config = {
            "web": {
                "client_id": cid,
                "client_secret": csec,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(
            host=host,
            port=port,
            prompt="consent",
            authorization_prompt_message="Đang mở trình duyệt để đăng nhập Google...",
        )

    rt = getattr(creds, "refresh_token", None)
    if not rt:
        print("Không nhận được refresh_token. Thử lại hoặc kiểm tra OAuth consent / redirect URI khớp Console.", file=sys.stderr)
        sys.exit(1)
    print("\nRefresh token (dán vào .env GCAL_MASTER_REFRESH_TOKEN hoặc Supabase → user → gcal_refresh_token):\n")
    print(rt)


if __name__ == "__main__":
    main()
