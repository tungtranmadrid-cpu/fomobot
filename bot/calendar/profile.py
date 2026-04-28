"""Tra profile lịch của user từ bảng user trên Supabase."""
import logging
from typing import Any, Optional, Tuple

from ..config import SUPABASE_USER_TABLE

logger = logging.getLogger(__name__)


def get_user_db_profile(sb: Any, chat_id: int) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Trả (email_congty, display_name, error_msg).
    email_congty dùng để lọc sự kiện trong bảng event_attendees.
    """
    try:
        r = (
            sb.table(SUPABASE_USER_TABLE)
            .select("Username, email_congty")
            .eq("telegram_ID", str(chat_id))
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return (
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
        email = (row.get("email_congty") or "").strip() or None
        name  = (row.get("Username")     or "").strip()
        if not email:
            return (
                None,
                name,
                "Thiếu email_congty trên bảng user — cần để lọc lịch theo email công ty.",
            )
        return email, name, None
    except Exception as e:
        logger.exception("get_user_db_profile: %s", e)
        return None, "", str(e)
