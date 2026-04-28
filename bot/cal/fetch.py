"""Fetch sự kiện từ Google Calendar (OAuth / SA / master aggregator)."""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from ..config import GCAL_MASTER_REFRESH_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON
from .auth import (
    build_google_calendar_oauth,
    build_google_calendar_service_account,
    calendar_id_for_list,
    use_gcal_master_aggregator,
)

logger = logging.getLogger(__name__)


def _event_email_norm(e: Optional[str]) -> str:
    return (e or "").strip().lower()


def filter_calendar_events_for_user_email(
    events: List[Dict[str, Any]],
    user_email: str,
) -> List[Dict[str, Any]]:
    """
    Giữ sự kiện mà user (email_congty) là người tổ chức, người tạo, hoặc có trong attendees.
    Dùng khi toàn bộ lịch công ty được add vào một tài khoản Google (master).
    """
    target = _event_email_norm(user_email)
    if not target or not events:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for ev in events:
        eid = ev.get("id")
        if eid and eid in seen:
            continue
        org = ev.get("organizer") or {}
        if _event_email_norm(org.get("email")) == target:
            if eid:
                seen.add(eid)
            out.append(ev)
            continue
        cr = ev.get("creator") or {}
        if _event_email_norm(cr.get("email")) == target:
            if eid:
                seen.add(eid)
            out.append(ev)
            continue
        for att in ev.get("attendees") or []:
            if _event_email_norm(att.get("email")) == target:
                if eid:
                    seen.add(eid)
                out.append(ev)
                break
    return out


def fetch_master_calendar_raw(
    day: date,
    display_tz: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Lấy toàn bộ sự kiện trong ngày trên calendar primary của tài khoản master (không lọc)."""
    rt = (GCAL_MASTER_REFRESH_TOKEN or "").strip()
    if not rt:
        return None, "Thiếu GCAL_MASTER_REFRESH_TOKEN trong .env."
    tz = ZoneInfo(display_tz)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    time_min = start_local.isoformat()
    time_max = end_local.isoformat()
    try:
        service, err = build_google_calendar_oauth(rt)
        if err or not service:
            return None, err or "Không tạo được Google Calendar client (master)."
        ev = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                timeZone=display_tz,
            )
            .execute()
        )
        return list(ev.get("items") or []), None
    except Exception as e:
        logger.exception("fetch_master_calendar_raw: %s", e)
        return None, str(e)


def fetch_calendar_events_for_day(
    calendar_owner_email: str,
    day: date,
    display_tz: str = "Asia/Ho_Chi_Minh",
    oauth_refresh_token: Optional[str] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """
    Chế độ master (GCAL_MASTER_REFRESH_TOKEN): đọc lịch tài khoản tập trung rồi lọc theo email_congty.
    Chế độ cũ: OAuth per-user / Service Account impersonate email_congty.
    """
    if use_gcal_master_aggregator():
        raw, err = fetch_master_calendar_raw(day, display_tz)
        if err or raw is None:
            return None, err
        return filter_calendar_events_for_user_email(raw, calendar_owner_email), None

    tz = ZoneInfo(display_tz)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    time_min = start_local.isoformat()
    time_max = end_local.isoformat()

    try:
        service: Any = None
        err: Optional[str] = None
        rt = (oauth_refresh_token or "").strip()
        if rt:
            service, err = build_google_calendar_oauth(rt)
        else:
            if not (calendar_owner_email or "").strip():
                return None, "Thiếu email_congty khi dùng Service Account (Google Workspace)."
            service, err = build_google_calendar_service_account(calendar_owner_email)

        if err or not service:
            return None, err or "Không tạo được Google Calendar client."

        cal_id = calendar_id_for_list(calendar_owner_email, oauth_refresh_token)
        ev = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
                timeZone=display_tz,
            )
            .execute()
        )
        return list(ev.get("items") or []), None
    except Exception as e:
        logger.exception("fetch_calendar_events_for_day: %s", e)
        return None, str(e)


def get_google_calendar_service(
    calendar_owner_email: str,
    oauth_refresh_token: Optional[str] = None,
) -> Tuple[Any, Optional[str]]:
    """Tạo client Calendar (OAuth hoặc Service Account) — dùng chung cho list/get sự kiện."""
    if use_gcal_master_aggregator():
        rt = (GCAL_MASTER_REFRESH_TOKEN or "").strip()
        if rt:
            return build_google_calendar_oauth(rt)
        return None, "Thiếu GCAL_MASTER_REFRESH_TOKEN."
    rt = (oauth_refresh_token or "").strip()
    if rt:
        return build_google_calendar_oauth(rt)
    if not (calendar_owner_email or "").strip():
        return None, "Thiếu email_congty khi dùng Service Account (Google Workspace)."
    return build_google_calendar_service_account(calendar_owner_email)


def fetch_calendar_event_by_id(
    calendar_owner_email: str,
    oauth_refresh_token: Optional[str],
    event_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Lấy một sự kiện đầy đủ (attendees, attachments, description, conferenceData)."""
    service, err = get_google_calendar_service(calendar_owner_email, oauth_refresh_token)
    if err or not service:
        return None, err or "Không tạo được Google Calendar client."
    eid = (event_id or "").strip()
    if not eid:
        return None, "Thiếu event id."
    try:
        cal_id = calendar_id_for_list(calendar_owner_email, oauth_refresh_token)
        ev = service.events().get(calendarId=cal_id, eventId=eid).execute()
        return ev, None
    except Exception as e:
        logger.exception("fetch_calendar_event_by_id: %s", e)
        return None, str(e)


def gcalendar_ready() -> bool:
    """Service Account, OAuth per-user, hoặc chế độ master (GCAL_MASTER_REFRESH_TOKEN + OAuth client)."""
    if use_gcal_master_aggregator():
        return True
    sa = bool(GOOGLE_SERVICE_ACCOUNT_JSON)
    from ..config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
    oauth = bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET)
    return sa or oauth
