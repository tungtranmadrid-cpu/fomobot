"""Job nhắc lịch hàng ngày (đọc từ DB) + đăng ký bot commands."""
import logging
from datetime import datetime, time as dtime
from typing import Any, List, Optional, Tuple

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
    SUPABASE_USER_TABLE,
)
from .db_fetch import fetch_events_for_user_date
from .format import format_day_schedule

logger = logging.getLogger(__name__)


def _parse_telegram_chat_id(raw: Any) -> Optional[int]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


async def daily_calendar_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue: gửi lịch trong ngày cho mọi user trong bảng user (đọc từ calendar_events)."""
    sb = get_supabase_client()
    if not sb:
        logger.warning("Nhắc lịch: chưa cấu hình Supabase.")
        return

    try:
        res = sb.table(SUPABASE_USER_TABLE).select("telegram_ID, email_congty, Username").execute()
        rows = res.data or []
    except Exception as e:
        logger.exception("Nhắc lịch: đọc bảng user: %s", e)
        return

    tz = ZoneInfo(GCALENDAR_TZ)
    today = datetime.now(tz).date()
    bot   = context.application.bot

    for row in rows:
        chat_id = _parse_telegram_chat_id(row.get("telegram_ID"))
        email   = (row.get("email_congty") or "").strip()
        name    = (row.get("Username")     or "").strip()

        if not chat_id or not email:
            continue

        try:
            events, err = await run_blocking(
                fetch_events_for_user_date, sb, email, today, GCALENDAR_TZ
            )
        except Exception as e:
            logger.exception("Nhắc lịch fetch_events %s: %s", chat_id, e)
            continue

        if err:
            logger.warning("Nhắc lịch %s: %s", email, err)
            continue

        text = format_day_schedule(events or [], today, GCALENDAR_TZ)
        if name:
            text = f"Chào {name},\n\n{text}"

        try:
            if len(text) > 4000:
                for i in range(0, len(text), 4000):
                    await bot.send_message(chat_id=chat_id, text=text[i : i + 4000])
            else:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as se:
            logger.warning("Nhắc lịch gửi tới %s: %s", chat_id, se)


BOT_COMMANDS: List[Tuple[str, str]] = [
    ("start",  "Lời chào + danh sách lệnh"),
    ("lich",   "Xem lịch hôm nay / ngày mai / thứ mấy"),
    ("tomtat", "Tóm tắt biên bản + công việc cuộc họp"),
    ("think",  "Bật/tắt chế độ suy nghĩ từng bước"),
    ("clear",  "Xoá lịch sử hội thoại"),
    ("id",     "Hiện Chat ID của bạn"),
    ("dk",     "Đăng ký sử dụng bot"),
    ("model",  "Xem model AI đang dùng"),
    ("cancel", "Huỷ đăng ký /dk"),
]


async def post_init_schedule(application: Application) -> None:
    """Lên lịch nhắc lịch hàng ngày (DAILY_CALENDAR_HOUR:MINUTE, GCALENDAR_TZ)."""
    try:
        await application.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
        )
    except Exception as e:
        logger.warning("setMyCommands fail: %s", e)

    jq = application.job_queue
    if not jq:
        logger.warning("Không có JobQueue — cài: pip install 'python-telegram-bot[job-queue]'.")
        return

    if not get_supabase_client():
        logger.info("Nhắc lịch: tắt (chưa cấu hình Supabase).")
        return

    tz   = ZoneInfo(GCALENDAR_TZ)
    when = dtime(hour=DAILY_CALENDAR_HOUR, minute=DAILY_CALENDAR_MINUTE, tzinfo=tz)
    jq.run_daily(daily_calendar_reminder, time=when, name="daily_calendar")
    logger.info(
        "Đã bật nhắc lịch hàng ngày lúc %02d:%02d (%s).",
        DAILY_CALENDAR_HOUR, DAILY_CALENDAR_MINUTE, GCALENDAR_TZ,
    )
