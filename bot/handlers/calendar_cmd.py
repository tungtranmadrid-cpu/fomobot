"""Handlers Google Calendar: /lich + trả lời câu hỏi lịch / chi tiết cuộc họp."""
import asyncio
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
    parse_google_start_end,
)
from ..calendar.intent import (
    extract_time_of_day,
    resolve_day_for_meeting_query,
    resolve_day_keyword,
    select_event_index_by_ai,
)
from ..calendar.profile import get_user_calendar_profile
from ..calendar.tasks import (
    fetch_members_snapshot,
    format_summary_section,
    format_tasks_table,
    has_saved_tasks_for_event,
    resolve_assignees,
    save_meeting_tasks,
    summarize_and_extract_tasks,
)
from ..clients import get_supabase_client
from ..config import GCALENDAR_TZ

logger = logging.getLogger(__name__)

# Callback prefix cho inline keyboard chọn cuộc họp khi ambiguous.
# callback_data tối đa 64 byte → không nhét event_id dài vào được (recurring
# event id dạng 'xxx_20260414T030000Z' có thể > 64B). Lưu event_ids ở
# _pending_picks theo chat_id, callback chỉ mang index ngắn.
MEET_PICK_PREFIX = "meetpick:"
_pending_picks: Dict[int, List[str]] = {}


def _register_pending_picks(chat_id: int, event_ids: List[str]) -> None:
    _pending_picks[chat_id] = event_ids


def _get_pending_pick(chat_id: int, idx: int) -> Optional[str]:
    ids = _pending_picks.get(chat_id) or []
    if 0 <= idx < len(ids):
        return ids[idx]
    return None


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


def _build_pick_keyboard(
    chat_id: int,
    events: List[Dict[str, Any]],
    display_tz: str,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    event_ids: List[str] = []
    for i, ev in enumerate(events):
        eid = (ev.get("id") or "").strip()
        if not eid:
            continue
        event_ids.append(eid)
        idx = len(event_ids) - 1
        label = _short_label_for_event(ev, idx, display_tz)
        rows.append([InlineKeyboardButton(label, callback_data=f"{MEET_PICK_PREFIX}{idx}")])
    _register_pending_picks(chat_id, event_ids)
    return InlineKeyboardMarkup(rows)


async def _send_long_text(chat: Any, text: str) -> None:
    """Gửi tin nhắn; chia 4000 char nếu quá dài."""
    if len(text) <= 4000:
        await chat.send_message(text)
        return
    for i in range(0, len(text), 4000):
        await chat.send_message(text[i : i + 4000])


async def _run_summary_and_save_tasks(
    full_ev: Dict[str, Any],
    members: List[Dict[str, Any]],
    sb: Any,
    created_by_chat_id: int,
) -> str:
    """Auto: LLM tóm tắt + trích task → resolve assignees → save (dedup). Trả text gộp."""
    summary_dict, tasks = await run_blocking(
        summarize_and_extract_tasks, full_ev, members, GCALENDAR_TZ
    )
    parts: List[str] = []
    summary_text = format_summary_section(summary_dict)
    if summary_text:
        parts.append(summary_text)

    if tasks:
        tasks = await run_blocking(resolve_assignees, sb, tasks, members)
        event_id = (full_ev.get("id") or "").strip()
        already = await run_blocking(has_saved_tasks_for_event, sb, event_id)
        saved = 0
        if not already:
            saved = await run_blocking(
                save_meeting_tasks, sb, full_ev, tasks, created_by_chat_id, GCALENDAR_TZ
            )
        body = format_tasks_table(full_ev, tasks, GCALENDAR_TZ)
        if already:
            body += "\n\n(Các công việc này đã được lưu trước đó — không insert lại.)"
        elif saved:
            body += f"\n\nĐã lưu {saved} công việc vào meeting_tasks."
        parts.append(body)
    elif summary_text:
        # Có biên bản để tóm tắt nhưng không trích được task → báo nhẹ.
        parts.append(format_tasks_table(full_ev, [], GCALENDAR_TZ))

    return "\n\n".join(p for p in parts if p)


async def _send_meeting_detail_for_event(
    chat: Any,
    event_id: str,
    email: str,
    refresh: Optional[str],
    name: Optional[str],
    sb: Any,
) -> None:
    """Fetch chi tiết 1 event + format + send. Auto tóm tắt + trích task nếu có biên bản."""
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

    has_minutes = bool((full_ev.get("description") or "").strip())
    # Có biên bản → bỏ phần raw description, thay bằng tóm tắt + task list.
    body = format_meeting_details_text(
        full_ev, GCALENDAR_TZ, members_by_email, show_description=not has_minutes
    )
    if name:
        body = f"Chào {name},\n\n{body}"

    if has_minutes:
        await chat.send_action("typing")
        members = await run_blocking(fetch_members_snapshot, sb)
        extra = await _run_summary_and_save_tasks(full_ev, members, sb, chat.id)
        if extra:
            body = body + "\n\n" + extra

    await _send_long_text(chat, body)


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
        keyboard = _build_pick_keyboard(chat_id, ev_list, GCALENDAR_TZ)
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
    idx_raw = data[len(MEET_PICK_PREFIX):].strip()
    try:
        idx = int(idx_raw)
    except ValueError:
        await query.edit_message_text("Callback không hợp lệ.")
        return

    chat_id = update.effective_chat.id
    event_id = _get_pending_pick(chat_id, idx)
    if not event_id:
        await query.edit_message_text(
            "Danh sách cuộc họp đã hết hạn. Hỏi lại giúp mình nhé."
        )
        return

    sb = get_supabase_client()
    if not sb:
        await query.edit_message_text("Chưa cấu hình Supabase.")
        return
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


def _filter_events_by_time_of_day(
    events: List[Dict[str, Any]],
    tod: Optional[tuple],
    display_tz: str,
) -> List[Dict[str, Any]]:
    """Lọc events theo khung giờ (sáng/chiều/tối). tod=None → trả nguyên."""
    if not tod:
        return events
    h_start, h_end = tod
    out: List[Dict[str, Any]] = []
    for ev in events:
        try:
            _is_all, st, _en, _loc = parse_google_start_end(ev, display_tz)
            if h_start <= st.hour < h_end:
                out.append(ev)
        except Exception:
            continue
    return out


def _format_aggregated_tasks(all_tasks: List[Dict[str, Any]]) -> str:
    """Gom task từ nhiều cuộc họp thành 1 bảng tổng."""
    if not all_tasks:
        return "(Không trích được công việc nào từ các cuộc họp trong khung thời gian này.)"
    from datetime import date as _date

    lines: List[str] = ["Danh sách công việc tổng hợp:", ""]
    for i, t in enumerate(all_tasks, 1):
        lines.append(f"{i}. Tên CV: {t.get('task_name') or '(không có)'}")
        src = (t.get("_meeting_title") or "").strip()
        if src:
            lines.append(f"   Cuộc họp: {src}")
        detail = (t.get("task_detail") or "").strip()
        if detail:
            lines.append(f"   Chi tiết: {detail}")
        assignees = t.get("assignees") or []
        if assignees:
            parts: List[str] = []
            for a in assignees:
                nm = (a.get("name") or "").strip() or "(không tên)"
                em = (a.get("email") or "").strip()
                parts.append(f"{nm} ({em})" if em else f"{nm} (chưa khớp)")
            lines.append("   Người thực hiện: " + ", ".join(parts))
        else:
            lines.append("   Người thực hiện: (chưa rõ)")
        dl = t.get("deadline")
        dl_raw = (t.get("deadline_raw") or "").strip()
        if isinstance(dl, _date):
            lines.append(f"   Deadline: {dl.strftime('%d/%m/%Y')}")
        elif dl_raw:
            lines.append(f"   Deadline: {dl_raw}")
        else:
            lines.append("   Deadline: (chưa rõ)")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _summarize_one_event(
    email: str,
    refresh: Optional[str],
    ev: Dict[str, Any],
    members: List[Dict[str, Any]],
    display_tz: str,
) -> Dict[str, Any]:
    """Fetch full event + LLM summarize+extract. Trả dict {event, summary_dict, tasks} hoặc {error}."""
    eid = (ev.get("id") or "").strip()
    if not eid:
        return {"event": ev, "error": "sự kiện không có id"}
    full_ev, gerr = await run_blocking(fetch_calendar_event_by_id, email or "", refresh, eid)
    if gerr or not full_ev:
        return {"event": ev, "error": gerr or "không đọc được chi tiết"}
    desc = (full_ev.get("description") or "").strip()
    if not desc:
        return {"event": full_ev, "summary_dict": {}, "tasks": [], "no_minutes": True}
    summary_dict, tasks = await run_blocking(
        summarize_and_extract_tasks, full_ev, members, display_tz
    )
    return {"event": full_ev, "summary_dict": summary_dict, "tasks": tasks}


async def cmd_tomtat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tomtat [nay/mai/sáng nay/chiều mai/...]: tóm tắt mọi cuộc họp + tổng hợp task."""
    if not update.message:
        return
    if not gcalendar_ready():
        await update.message.reply_text("Google Calendar chưa sẵn sàng. Kiểm tra GOOGLE_* trong .env.")
        return
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return

    arg_raw = " ".join(context.args or []).strip() if context.args else "nay"
    target_day, day_err = resolve_day_keyword(arg_raw)
    if day_err or not target_day:
        await update.message.reply_text(
            day_err or "Cú pháp: /tomtat nay | mai | sáng nay | chiều mai | 26/03"
        )
        return
    tod = extract_time_of_day(arg_raw)

    chat_id = update.effective_chat.id
    email, refresh, name, profile_err = get_user_calendar_profile(sb, chat_id)
    if profile_err:
        await update.message.reply_text(profile_err)
        return

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
        logger.exception("cmd_tomtat fetch: %s", e)
        await update.message.reply_text(f"Lấy lịch lỗi: {e}{calendar_oauth_revoked_hint(e)}")
        return
    if err:
        await update.message.reply_text(f"Lấy lịch lỗi: {err}{calendar_oauth_revoked_hint(err)}")
        return

    ev_list = _filter_events_by_time_of_day(events or [], tod, GCALENDAR_TZ)
    if not ev_list:
        range_label = arg_raw or "hôm nay"
        await update.message.reply_text(
            f"Không có cuộc họp nào ({range_label}) trên Google Calendar."
        )
        return

    day_str = target_day.strftime("%d/%m/%Y")
    tod_label = ""
    if tod:
        h_s, h_e = tod
        tod_label = f" ({h_s:02d}h–{h_e:02d}h)"

    header = f"Tóm tắt các cuộc họp ngày {day_str}{tod_label} — {len(ev_list)} cuộc:\n"
    if name:
        header = f"Chào {name},\n\n" + header
    await update.message.reply_text(header)

    members = await run_blocking(fetch_members_snapshot, sb)

    # Parallel LLM + Google Calendar fetch — mỗi cuộc 1 coroutine.
    results = await asyncio.gather(
        *[_summarize_one_event(email or "", refresh, ev, members, GCALENDAR_TZ) for ev in ev_list],
        return_exceptions=True,
    )

    all_tasks: List[Dict[str, Any]] = []
    for i, res in enumerate(results, 1):
        ev_src = ev_list[i - 1]
        title = (ev_src.get("summary") or "(Không tiêu đề)").strip()
        try:
            _is_all, st, _en, _loc = parse_google_start_end(ev_src, GCALENDAR_TZ)
            time_str = "cả ngày" if _is_all else st.strftime("%H:%M")
        except Exception:
            time_str = ""
        head = f"[{i}] {time_str} — {title}" if time_str else f"[{i}] {title}"

        if isinstance(res, Exception):
            await _send_long_text(update.effective_chat, f"{head}\nLỗi xử lý: {res}")
            continue

        if res.get("error"):
            await _send_long_text(update.effective_chat, f"{head}\nLỗi: {res['error']}")
            continue

        full_ev = res["event"]
        if res.get("no_minutes"):
            await _send_long_text(
                update.effective_chat,
                f"{head}\n(Cuộc họp chưa có biên bản trong mô tả — bỏ qua tóm tắt.)",
            )
            continue

        summary_dict = res.get("summary_dict") or {}
        tasks = res.get("tasks") or []
        if tasks:
            tasks = await run_blocking(resolve_assignees, sb, tasks, members)
            eid = (full_ev.get("id") or "").strip()
            if eid and not await run_blocking(has_saved_tasks_for_event, sb, eid):
                try:
                    await run_blocking(
                        save_meeting_tasks, sb, full_ev, tasks, chat_id, GCALENDAR_TZ
                    )
                except Exception as e:
                    logger.warning("cmd_tomtat save_meeting_tasks: %s", e)
            for t in tasks:
                t["_meeting_title"] = title
                all_tasks.append(t)

        parts: List[str] = [head]
        summary_text = format_summary_section(summary_dict)
        if summary_text:
            parts.append(summary_text)
        else:
            parts.append("(Không tóm tắt được nội dung.)")
        if tasks:
            # Chỉ tên + assignee cho từng cuộc (chi tiết đã nằm ở bảng tổng hợp).
            tks = [
                f"   - {t.get('task_name') or '(không có)'}"
                for t in tasks
            ]
            parts.append("Công việc:")
            parts.extend(tks)
        await _send_long_text(update.effective_chat, "\n".join(parts))

    # Bảng tổng hợp cuối.
    await _send_long_text(update.effective_chat, _format_aggregated_tasks(all_tasks))
