"""Lấy sự kiện lịch từ bảng calendar_events (Supabase).
Bot đọc từ DB thay vì gọi Google Calendar / MS Graph API trực tiếp.
Dữ liệu được push vào DB bởi webhook Edge Functions khi có thay đổi.
"""
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from ..config import GCALENDAR_TZ

logger = logging.getLogger(__name__)


def db_calendar_ready(sb: Any) -> bool:
    """Kiểm tra có thể dùng DB calendar không (chỉ cần Supabase kết nối)."""
    return sb is not None


def _row_to_gcal_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DB row → Google Calendar event format để tương thích format.py / tasks.py."""
    start_time = row.get("start_time") or ""
    end_time   = row.get("end_time")   or ""
    is_all_day = bool(row.get("is_all_day"))
    tz_str     = row.get("timezone") or GCALENDAR_TZ

    if is_all_day:
        start = {"date": start_time[:10]} if start_time else {}
        end   = {"date": end_time[:10]}   if end_time   else {}
    else:
        start = {"dateTime": start_time, "timeZone": tz_str}
        end   = {"dateTime": end_time,   "timeZone": tz_str}

    attendees = []
    for a in (row.get("attendees") or []):
        if not (a.get("email") or "").strip():
            continue
        attendees.append({
            "email":          (a.get("email") or "").strip(),
            "displayName":    (a.get("name")  or "").strip(),
            "responseStatus": a.get("response_status") or "none",
        })

    organizer: Dict[str, str] = {}
    if row.get("organizer_email"):
        organizer = {
            "email":       row["organizer_email"],
            "displayName": row.get("organizer_name") or "",
        }

    meeting_url = (row.get("meeting_url") or "").strip()

    ev: Dict[str, Any] = {
        "id":       str(row.get("id") or ""),
        "summary":  (row.get("title")       or "").strip(),
        "description": (row.get("description") or "").strip(),
        "location": (row.get("location")    or "").strip(),
        "start":    start,
        "end":      end,
        "attendees":  attendees,
        "organizer":  organizer,
        # Trường riêng để format.py hiển thị meeting URL
        "_meeting_url":      meeting_url,
        "_meeting_platform": row.get("meeting_platform") or "",
        "_source":           row.get("source") or "",
    }

    # Đưa meeting_url vào conferenceData để format_meeting_details_text nhận diện
    if meeting_url:
        ev["conferenceData"] = {
            "entryPoints": [{"entryPointType": "video", "uri": meeting_url}]
        }

    return ev


def fetch_events_for_user_date(
    sb: Any,
    email: str,
    target_date: date,
    tz_str: str = GCALENDAR_TZ,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Lấy danh sách sự kiện của user trong ngày từ DB (RPC)."""
    if not sb:
        return [], "Chưa cấu hình Supabase."
    if not email:
        return [], "Thiếu email để lọc lịch."
    try:
        r = sb.rpc("get_events_for_user_date", {
            "p_email": email,
            "p_date":  target_date.isoformat(),
            "p_tz":    tz_str,
        }).execute()
        rows = r.data or []
        return [_row_to_gcal_event(row) for row in rows], None
    except Exception as e:
        logger.exception("fetch_events_for_user_date: %s", e)
        return [], str(e)


def fetch_event_by_id(
    sb: Any,
    event_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Lấy chi tiết 1 sự kiện theo UUID từ DB (dùng cho /tomtat và chi tiết cuộc họp)."""
    if not sb:
        return None, "Chưa cấu hình Supabase."
    if not event_id:
        return None, "Thiếu event_id."
    try:
        r = (
            sb.table("calendar_events")
            .select("*")
            .eq("id", event_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return None, "Không tìm thấy sự kiện trong DB."
        row = dict(rows[0])

        # Gắn danh sách attendees
        ra = (
            sb.table("event_attendees")
            .select("name, email, response_status, attendee_type")
            .eq("event_id", event_id)
            .execute()
        )
        row["attendees"] = ra.data or []
        return _row_to_gcal_event(row), None
    except Exception as e:
        logger.exception("fetch_event_by_id: %s", e)
        return None, str(e)
