"""Handlers Google Calendar: /lich + trả lời câu hỏi lịch / chi tiết cuộc họp."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

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

# Callback prefix cho inline keyboard chọn cuộc họp khi ambiguous.
MEET_PICK_PREFIX = "meetpick:"


def _short_label_for_event(ev: Dict[str, Any], idx: int, display_tz: str) -> str:
    """Label ngắn cho nút inline: '(1) 09:00 MedCEO'. Giới hạn ~45 ký tự cho Telegram."""
    title = (ev.get("summary") or "Không tiêu đề").strip()
    start = ev.get("start") or {}
    dt_s = str(start.get("dateTime") or "")
    time_str = ""
    if dt_s:
        try:
            if dt_s.endswith("Z"):
                dt_s = dt_s[:-1] + "+00:00"
            st = datetime.fromisoformat(dt_s).astimezone(ZoneInfo(display_tz))
            time_str = st.strftime("%H:%M")
        except Exception:
            pass
    elif "date" in start:
        time_str = "cả ngày"
    head = f"({idx + 1})"
    if time_str:
        head += f" {time_str}"
    rest = 45 - len(head) - 1
    if rest > 0 and len(title) > rest:
        title = title[: rest - 1] + "…"
    return f"{head} {title}"


def _build_pick_keyboard(events: List[Dict[str, Any]], display_tz: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i, ev in enumerate(events):
        eid = (ev.get("id") or "").strip()
        if not eid:
            continue
        label = _short_label_for_event(ev, i, display_tz)
        # callback_data tối đa 64 byte; event_id Google ~26-32 chars → an toàn.
        rows.append([InlineKeyboardButton(label, callback_data=f"{MEET_PICK_PREFIX}{eid}")])
    return InlineKeyboardMarkup(rows)


async def _send_meeting_detail_for_event(
    chat: Any,
    event_id: str,
    email: str,
    refresh: Optional[str],
    name: Optional[str],
    sb: Any,
) -> None:
    """Fetch chi tiết 1 event + format + send. Dùng chung cho initial flow & callback pick."""
    full_ev, gerr = await run_blocking(fetch_calendar_event_by_id, email or "", refresh, event_id)
    if gerr or not full_ev:
        await chat.send_message(f"Không đọc chi tiết sự kiện: {gerr or 'unknown'}")
        return

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
            await chat.send_message(body[i : i + 4000])
    else:
        await chat.send_message(body)


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

    idx, reason = select_event_index_by_ai(user_text, ev_list, GCALENDAR_TZ)

    if reason == "matched" and 0 <= idx < len(ev_list):
        raw_ev = ev_list[idx]
        eid = (raw_ev.get("id") or "").strip()
        if not eid:
            await update.message.reply_text("Sự kiện không có mã id — không lấy được chi tiết.")
            return True
        await _send_meeting_detail_for_event(update.effective_chat, eid, email or "", refresh, name, sb)
        return True

    # Ambiguous / không rõ → hiển thị inline keyboard cho user bấm chọn.
    if reason in ("ambiguous", "error") or idx < 0:
        keyboard = _build_pick_keyboard(ev_list, GCALENDAR_TZ)
        prompt = (
            f"Ngày {target_day.strftime('%d/%m/%Y')} có {len(ev_list)} cuộc họp. "
            "Bạn muốn xem chi tiết cuộc nào?"
        )
        await update.message.reply_text(prompt, reply_markup=keyboard)
        return True

    # no_match
    await update.message.reply_text(
        "Không có cuộc họp nào khớp câu hỏi trong ngày đó. "
        "Bạn thử ghi rõ tên cuộc họp hoặc khung giờ giúp mình nhé."
    )
    return True


async def on_meeting_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback khi user bấm nút chọn cuộc họp trong inline keyboard."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = (query.data or "").strip()
    if not data.startswith(MEET_PICK_PREFIX):
        return
    event_id = data[len(MEET_PICK_PREFIX):].strip()
    if not event_id:
        await query.edit_message_text("Mã sự kiện trống.")
        return

    sb = get_supabase_client()
    if not sb:
        await query.edit_message_text("Chưa cấu hình Supabase.")
        return
    chat_id = update.effective_chat.id
    email, refresh, name, profile_err = get_user_calendar_profile(sb, chat_id)
    if profile_err:
        await query.edit_message_text(profile_err)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await update.effective_chat.send_action("typing")
    await _send_meeting_detail_for_event(update.effective_chat, event_id, email or "", refresh, name, sb)


async def cmd_lich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tra lịch theo cách nói tự nhiên: nay/mai/dd/mm/thứ N..."""
    day_raw = " ".join(context.args or []).strip() if context.args else "nay"
    await answer_calendar_question(update, day_raw)
