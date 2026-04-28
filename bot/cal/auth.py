"""Tạo Google Calendar service (Service Account / OAuth / master aggregator)."""
import json
import logging
from typing import Any, Dict, Optional, Tuple

from ..config import (
    GCAL_MASTER_EMAIL,
    GCAL_MASTER_REFRESH_TOKEN,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_SERVICE_ACCOUNT_JSON,
)
from . import GOOGLE_CALENDAR_SCOPES

logger = logging.getLogger(__name__)

_cached_sa: Optional[Dict[str, Any]] = None


def _load_service_account_dict() -> Optional[Dict[str, Any]]:
    global _cached_sa
    if _cached_sa is not None:
        return _cached_sa
    raw = (GOOGLE_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            _cached_sa = json.loads(raw)
        else:
            with open(raw, encoding="utf-8") as f:
                _cached_sa = json.load(f)
    except Exception as e:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON không đọc được: %s", e)
        return None
    return _cached_sa


def build_google_calendar_service_account(user_email: str) -> Tuple[Any, Optional[str]]:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = _load_service_account_dict()
    if not info:
        return None, "Thiếu GOOGLE_SERVICE_ACCOUNT_JSON (đường dẫn file hoặc chuỗi JSON)."

    creds = service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_CALENDAR_SCOPES)
    creds = creds.with_subject(user_email.strip())
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return svc, None


def build_google_calendar_oauth(refresh_token: str) -> Tuple[Any, Optional[str]]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    cid = (GOOGLE_OAUTH_CLIENT_ID or "").strip()
    csec = (GOOGLE_OAUTH_CLIENT_SECRET or "").strip()
    if not cid or not csec:
        return None, "Thiếu GOOGLE_OAUTH_CLIENT_ID hoặc GOOGLE_OAUTH_CLIENT_SECRET."

    creds = Credentials(
        token=None,
        refresh_token=refresh_token.strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csec,
        scopes=GOOGLE_CALENDAR_SCOPES,
    )
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return svc, None


def use_gcal_master_aggregator() -> bool:
    """Đọc một calendar tập trung (GCAL_MASTER_*) rồi lọc theo email_congty."""
    return bool(
        (GCAL_MASTER_REFRESH_TOKEN or "").strip()
        and GCAL_MASTER_EMAIL
        and GOOGLE_OAUTH_CLIENT_ID
        and GOOGLE_OAUTH_CLIENT_SECRET
    )


def calendar_oauth_revoked_hint(err: Any) -> str:
    """Gợi ý khi Google trả invalid_grant (refresh token hết hạn / bị thu hồi)."""
    s = str(err).lower()
    if "invalid_grant" not in s and not ("revoked" in s and "token" in s):
        return ""
    return (
        "\n\nRefresh token Google đã hết hạn hoặc bị thu hồi. "
        "Chạy: python get_gcal_refresh_token.py (đăng nhập đúng tài khoản lịch master, ví dụ "
        f"{GCAL_MASTER_EMAIL or 'master'}). Cập nhật GCAL_MASTER_REFRESH_TOKEN trong .env hoặc cột gcal_refresh_token trên Supabase."
    )


def calendar_id_for_list(
    _calendar_owner_email: str,
    _oauth_refresh_token: Optional[str],
) -> str:
    """
    Luôn dùng calendarId="primary" cho cả OAuth và Service Account.

    - Service Account: đã gọi with_subject(email_congty) → primary = lịch chính user đó.
    - OAuth: primary = lịch chính của **tài khoản Google đã cấp refresh token** (thường trùng
      useremail). Không dùng email_congty làm calendarId — Google trả 404 nếu token không
      có quyền truy cập calendar đó như một resource riêng. Cần lấy token từ đúng tài khoản
      @medlatec.com (hoặc calendar được chia sẻ đủ quyền).
    """
    return "primary"
