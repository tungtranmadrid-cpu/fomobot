"""Job nhắc lịch hàng ngày + đăng ký bot commands."""
import logging
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from telegram import BotCommand
from telegram.ext import Application, ContextTypes

from ..async_utils import run_blocking
from ..clients import get_supabase_client
from ..config import (
    DAILY_CALENDAR_HOUR,
    DAILY_CALENDAR_MINUTE,
    GCALENDAR_TZ,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    SUPABASE_USER_TABLE,
)
from .auth import calendar_oauth_revoked_hint, use_gcal_master_aggregator
from .fetch import (
    fetch_calendar_events_for_day,
    fetch_master_calendar_raw,
    filter_calendar_events_for_user_email,
    gcalendar_ready,
)
from .format import format_day_schedule

logger = logging.getLogger(__name__)


def parse_telegram_chat_id(raw: Any) -> Optional[int]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


async def daily_calendar_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue: gửi lịch trong ngày — master: một lần gọi API rồi lọc theo email_congty từng user."""
    if not gcalendar_ready():
        logger.warning(
            "Nhắc lịch: thiếu google_calendar hoặc cấu hình Google (GOOGLE_SERVICE_ACCOUNT_JSON hoặc OAuth client)."
        )
        return
    sb = get_supabase_client()
    if not sb:
        logger.warning("Nhắc lịch: chưa cấu hình Supabase.")
        return

    try:
        res = sb.table(SUPABASE_USER_TABLE).select("*").execute()
        rows = res.data or []
    except Exception as e:
        logger.exception("Nhắc lịch: đọc bảng %s: %s", SUPABASE_USER_TABLE, e)
        return

    tz = ZoneInfo(GCALENDAR_TZ)
    today_local = datetime.now(tz).date()
    bot = context.application.bot

    master_raw: Optional[List[Dict[str, Any]]] = None
    master_err: Optional[str] = None
    if use_gcal_master_aggregator():
        try:
            master_raw, master_err = await run_blocking(fetch_master_calendar_raw, today_local, GCALENDAR_TZ)
        except Exception as e:
            logger.exception("Nhắc lịch master fetch: %s", e)
            master_err = str(e)

    for row in rows:
        cal_email = (row.get("email_congty") or "").strip()
        chat_id = parse_telegram_chat_id(row.get("telegram_ID"))
        refresh = (row.get("gcal_refresh_token") or "").strip() or None
        if not chat_id:
            continue
        if not cal_email:
            continue

        if use_gcal_master_aggregator():
            err = master_err
            events = None if err else filter_calendar_events_for_user_email(master_raw or [], cal_email)
        else:
            if not refresh and not (GOOGLE_SERVICE_ACCOUNT_JSON and cal_email):
                continue
            try:
                events, err = await run_blocking(
                    fetch_calendar_events_for_day,
                    cal_email,
                    today_local,
                    GCALENDAR_TZ,
                    refresh,
                )
            except Exception as e:
                logger.exception("Nhắc lịch fetch_calendar %s: %s", chat_id, e)
                err = str(e)
                events = None
        if err:
            who = cal_email or (f"gcal_refresh…{refresh[:6]}" if refresh else "?")
            logger.warning("Lịch %s: %s", who, err)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Không lấy được lịch Google Calendar hôm nay. "
                        "Kiểm tra: GCAL_MASTER_REFRESH_TOKEN + OAuth client; hoặc Service Account; "
                        "hoặc gcal_refresh_token trên Supabase (chế độ không dùng master)."
                        + calendar_oauth_revoked_hint(err)
                    ),
                )
            except Exception as se:
                logger.warning("Gửi lỗi lịch tới %s: %s", chat_id, se)
            continue

        text = format_day_schedule(events or [], today_local, GCALENDAR_TZ)
        name = (row.get("Username") or "").strip()
        if name:
            text = f"Chào {name},\n\n{text}"
        try:
            if len(text) > 4000:
                for i in range(0, len(text), 4000):
                    await bot.send_message(chat_id=chat_id, text=text[i : i + 4000])
            else:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as se:
            logger.warning("Gửi lịch tới %s: %s", chat_id, se)


BOT_COMMANDS: List[Tuple[str, str]] = [
    ("start", "Lời chào + danh sách lệnh"),
    ("lich", "Xem lịch hôm nay / ngày mai / thứ mấy"),
    ("tomtat", "Tóm tắt biên bản + công việc cuộc họp"),
    ("think", "Bật/tắt chế độ suy nghĩ từng bước"),
    ("clear", "Xoá lịch sử hội thoại"),
    ("id", "Hiện Chat ID của bạn"),
    ("dk", "Đăng ký sử dụng bot"),
    ("model", "Xem model AI đang dùng"),
    ("cancel", "Huỷ đăng ký /dk"),
]


async def post_init_schedule(application: Application) -> None:
    """Lên lịch 7h sáng (có thể đổi bằng DAILY_CALENDAR_HOUR/MINUTE, GCALENDAR_TZ)."""
    try:
        await application.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
        )
    except Exception as e:
        logger.warning("setMyCommands fail: %s", e)

    jq = application.job_queue
    if not jq:
        logger.warning(
            "Không có JobQueue. Cài: pip install 'python-telegram-bot[job-queue]' (đã khai trong requirements.txt)."
        )
        return
    if not gcalendar_ready():
        logger.info("Nhắc lịch Google: tắt (thiếu GOOGLE_* / google_calendar).")
        return
    tz = ZoneInfo(GCALENDAR_TZ)
    when = dtime(hour=DAILY_CALENDAR_HOUR, minute=DAILY_CALENDAR_MINUTE, tzinfo=tz)
    jq.run_daily(daily_calendar_reminder, time=when, name="daily_gcalendar")
    logger.info(
        "Đã bật nhắc lịch hàng ngày lúc %02d:%02d (%s).",
        DAILY_CALENDAR_HOUR,
        DAILY_CALENDAR_MINUTE,
        GCALENDAR_TZ,
    )
