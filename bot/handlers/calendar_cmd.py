"""Handlers Google Calendar: /lich + trả lời câu hỏi lịch / chi tiết cuộc họp."""
import logging
from typing import Any, Dict, List, Optional

from telegram import Update
from telegram.ext import ContextTypes

from ..async_utils import run_blocking
from ..calendar.auth import calendar_oauth_revoked_hint
from ..calendar.fetch import (
    fetch_calendar_event_by_id,
    fetch_calendar_events_for_day,
    gcalendar_ready,
)
from ..calendar.format import (
    fetch_members_by_emails,
    format_day_schedule,
    format_meeting_details_text,
    is_hidden_meeting_report_email,
)
from ..calendar.intent import (
    resolve_day_for_meeting_query,
    resolve_day_keyword,
    select_event_index_by_ai,
)
from ..calendar.profile import get_user_calendar_profile
from ..clients import get_supabase_client
from ..config import GCALENDAR_TZ

logger = logging.getLogger(__name__)


async def answer_calendar_question(
    update: Update,
    day_raw: str,
    custom_question: Optional[str] = None,
) -> bool:
    """Trả lời lịch theo ngày. Trả True nếu đã xử lý xong."""
    if not gcalendar_ready():
        await update.message.reply_text("Google Calendar chưa sẵn sàng. Kiểm tra GOOGLE_* trong .env.")
        return True
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return True

    target_day, day_err = resolve_day_keyword(day_raw)
    if day_err or not target_day:
        await update.message.reply_text(day_err or "Tham số ngày không hợp lệ.")
        return True

    chat_id = update.effective_chat.id
    email, refresh, name, profile_err = get_user_calendar_profile(sb, chat_id)
    if profile_err:
        await update.message.reply_text(profile_err)
        return True

    await update.message.chat.send_action("typing")
    try:
        events, err = await run_blocking(
            fetch_calendar_events_for_day,
            email or "",
            target_day,
            GCALENDAR_TZ,
            refresh,
        )
    except Exception as e:
        logger.exception("answer_calendar_question fetch_calendar: %s", e)
        await update.message.reply_text(f"Lấy lịch lỗi: {e}{calendar_oauth_revoked_hint(e)}")
        return True
    if err:
        await update.message.reply_text(f"Lấy lịch lỗi: {err}{calendar_oauth_revoked_hint(err)}")
        return True

    base_schedule = format_day_schedule(events or [], target_day, GCALENDAR_TZ)
    answer = base_schedule

    if name:
        answer = f"Chào {name},\n\n{answer}"
    if len(answer) > 4000:
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i : i + 4000])
    else:
        await update.message.reply_text(answer)
    return True


async def answer_meeting_detail_question(update: Update, user_text: str) -> bool:
    """Trả lời chi tiết cuộc họp (thành viên, tài liệu/link) từ Google Calendar."""
    if not gcalendar_ready():
        await update.message.reply_text("Google Calendar chưa sẵn sàng. Kiểm tra GOOGLE_* trong .env.")
        return True
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return True

    chat_id = update.effective_chat.id
    email, refresh, name, profile_err = get_user_calendar_profile(sb, chat_id)
    if profile_err:
        await update.message.reply_text(profile_err)
        return True

    target_day = resolve_day_for_meeting_query(user_text)
    await update.message.chat.send_action("typing")

    try:
        events, err = await run_blocking(
            fetch_calendar_events_for_day,
            email or "",
            target_day,
            GCALENDAR_TZ,
            refresh,
        )
    except Exception as e:
        logger.exception("answer_meeting_detail_question fetch: %s", e)
        await update.message.reply_text(f"Lấy lịch lỗi: {e}{calendar_oauth_revoked_hint(e)}")
        return True
    if err:
        await update.message.reply_text(f"Lấy lịch lỗi: {err}{calendar_oauth_revoked_hint(err)}")
        return True

    ev_list = events or []
    if not ev_list:
        await update.message.reply_text(
            f"Không có sự kiện nào trên Google Calendar ngày {target_day.strftime('%d/%m/%Y')} "
            "để tra chi tiết."
        )
        return True

    idx = select_event_index_by_ai(user_text, ev_list)
    if idx < 0:
        await update.message.reply_text(
            "Không xác định được cuộc họp nào trong danh sách. "
            "Hãy ghi rõ hơn tên cuộc họp hoặc thử trong ngày chỉ có một sự kiện."
        )
        return True

    raw_ev = ev_list[idx]
    eid = (raw_ev.get("id") or "").strip()
    if not eid:
        await update.message.reply_text("Sự kiện không có mã id — không lấy được chi tiết.")
        return True

    full_ev, gerr = await run_blocking(
        fetch_calendar_event_by_id,
        email or "",
        refresh,
        eid,
    )
    if gerr or not full_ev:
        await update.message.reply_text(f"Không đọc chi tiết sự kiện: {gerr or 'unknown'}")
        return True

    attendee_emails: List[str] = []
    for a in full_ev.get("attendees") or []:
        em = (a.get("email") or "").strip()
        if em and not is_hidden_meeting_report_email(em):
            attendee_emails.append(em)
    members_by_email: Dict[str, Dict[str, Any]] = {}
    if attendee_emails:
        members_by_email = await run_blocking(fetch_members_by_emails, sb, attendee_emails)

    body = format_meeting_details_text(full_ev, GCALENDAR_TZ, members_by_email)
    if name:
        body = f"Chào {name},\n\n{body}"
    if len(body) > 4000:
        for i in range(0, len(body), 4000):
            await update.message.reply_text(body[i : i + 4000])
    else:
        await update.message.reply_text(body)
    return True


async def cmd_lich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tra lịch theo cách nói tự nhiên: nay/mai/dd/mm/thứ N..."""
    day_raw = " ".join(context.args or []).strip() if context.args else "nay"
    await answer_calendar_question(update, day_raw)
