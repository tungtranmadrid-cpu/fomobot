"""Tra profile lịch của user từ bảng user trên Supabase."""
import logging
from typing import Any, Optional, Tuple

from ..config import (
    GCAL_MASTER_EMAIL,
    GCAL_MASTER_REFRESH_TOKEN,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    SUPABASE_USER_TABLE,
)
from .auth import use_gcal_master_aggregator

logger = logging.getLogger(__name__)


def get_user_calendar_profile(sb: Any, chat_id: int) -> Tuple[Optional[str], Optional[str], str, Optional[str]]:
    """
    Tìm profile lịch theo telegram chat_id.
    Trả về: (email_congty — lọc lịch / thành phần họp, refresh_token cho API, display_name, error).

    Chế độ master (.env GCAL_MASTER_REFRESH_TOKEN): lịch đọc từ tài khoản GCAL_MASTER_EMAIL,
    lọc theo email_congty; không cần gcal_refresh_token trên từng dòng user.
    """
    try:
        r = (
            sb.table(SUPABASE_USER_TABLE)
            .select("useremail,gcal_refresh_token,Username,telegram_ID,email_congty")
            .eq("telegram_ID", str(chat_id))
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return (
                None,
                None,
                "",
                "Không thấy bạn trong bảng user (telegram_ID).\n\n"
                "Đăng ký tự động: trong chat riêng với bot gõ /dk (username + email công ty), chờ admin duyệt.\n"
                "Hoặc thêm tay trên Supabase:\n"
                "1) Gõ /id để xem Chat ID.\n"
                "2) Dashboard → Table Editor → bảng user → Insert row.\n"
                "3) Điền telegram_ID, email_congty (để lọc lịch theo email công ty).",
            )
        row = rows[0] or {}
        cal_email = (row.get("email_congty") or "").strip() or None
        refresh = (row.get("gcal_refresh_token") or "").strip() or None
        name = (row.get("Username") or "").strip()
        if not cal_email:
            return (
                None,
                refresh,
                name,
                "Thiếu email_congty trên bảng user — cần để lọc lịch theo email công ty.",
            )
        if use_gcal_master_aggregator():
            mrt = (GCAL_MASTER_REFRESH_TOKEN or "").strip()
            return cal_email, mrt or None, name, None
        if not refresh and not (GOOGLE_SERVICE_ACCOUNT_JSON and cal_email):
            return (
                cal_email,
                refresh,
                name,
                "Thiếu kết nối Google Calendar. Cần một trong hai:\n\n"
                "• Chế độ tập trung: trong .env đặt GCAL_MASTER_REFRESH_TOKEN (tài khoản "
                f"{GCAL_MASTER_EMAIL or 'master'}) + GOOGLE_OAUTH_CLIENT_ID/SECRET.\n\n"
                "• Workspace: GOOGLE_SERVICE_ACCOUNT_JSON + domain delegation; "
                "email_congty = user cần đọc lịch.\n\n"
                "• OAuth từng user: gcal_refresh_token trên Supabase.",
            )
        return cal_email, refresh, name, None
    except Exception as e:
        logger.exception("get_user_calendar_profile: %s", e)
        return None, None, "", str(e)
