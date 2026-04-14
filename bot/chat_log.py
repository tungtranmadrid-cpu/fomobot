"""Ghi log chat Telegram vào Supabase + LoggingBot wrapper."""
import asyncio
import logging
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ExtBot

from .async_utils import run_blocking
from .clients import get_supabase_client
from .config import SUPABASE_CHAT_LOG_TABLE

logger = logging.getLogger(__name__)


def save_chat_log(
    *,
    direction: str,
    chat_id: Optional[int],
    message_text: str,
    message_type: str = "text",
    telegram_user_id: Optional[int] = None,
    telegram_username: Optional[str] = None,
    telegram_full_name: Optional[str] = None,
    update_id: Optional[int] = None,
) -> None:
    if not chat_id:
        return
    sb = get_supabase_client()
    if not sb:
        return
    payload = {
        "chat_id": int(chat_id),
        "direction": (direction or "").strip().lower(),
        "message_text": (message_text or "").strip(),
        "message_type": (message_type or "text").strip().lower(),
        "telegram_user_id": int(telegram_user_id) if telegram_user_id else None,
        "telegram_username": (telegram_username or "").strip() or None,
        "telegram_full_name": (telegram_full_name or "").strip() or None,
        "update_id": int(update_id) if update_id is not None else None,
    }
    try:
        sb.table(SUPABASE_CHAT_LOG_TABLE).insert(payload).execute()
    except Exception as e:
        logger.warning(
            "Không lưu được chat log (%s): %s",
            SUPABASE_CHAT_LOG_TABLE,
            e,
        )


async def save_chat_log_async(
    *,
    direction: str,
    chat_id: Optional[int],
    message_text: str,
    message_type: str = "text",
    telegram_user_id: Optional[int] = None,
    telegram_username: Optional[str] = None,
    telegram_full_name: Optional[str] = None,
    update_id: Optional[int] = None,
) -> None:
    """Ghi log chat vào Supabase nhưng không block event loop."""
    await run_blocking(
        save_chat_log,
        direction=direction,
        chat_id=chat_id,
        message_text=message_text,
        message_type=message_type,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        telegram_full_name=telegram_full_name,
        update_id=update_id,
    )


def extract_message_payload(update: Update) -> Tuple[str, str]:
    msg = update.effective_message
    if not msg:
        return "", "unknown"
    if msg.text:
        return msg.text, "text"
    if msg.caption:
        return msg.caption, "caption"
    if msg.sticker:
        return "[sticker]", "sticker"
    if msg.photo:
        return "[photo]", "photo"
    if msg.video:
        return "[video]", "video"
    if msg.document:
        return "[document]", "document"
    if msg.voice:
        return "[voice]", "voice"
    if msg.audio:
        return "[audio]", "audio"
    if msg.location:
        return "[location]", "location"
    if msg.contact:
        return "[contact]", "contact"
    return "[unsupported]", "other"


class LoggingBot(ExtBot):
    """ExtBot ghi log mọi message gửi đi vào telegram_chat_logs (không chặn luồng)."""

    async def send_message(self, chat_id, text, *args, **kwargs):
        sent = await super().send_message(chat_id, text, *args, **kwargs)
        try:
            asyncio.create_task(
                save_chat_log_async(
                    direction="outgoing",
                    chat_id=int(chat_id) if chat_id is not None else None,
                    message_text=str(text or ""),
                    message_type="text",
                    telegram_user_id=None,
                    telegram_username="bot",
                    telegram_full_name="bot",
                    update_id=None,
                )
            )
        except Exception as e:
            logger.debug("Không tạo được task ghi chat log outgoing: %s", e)
        return sent
