"""Handler chạy trước mọi update: log + rate-limit gate."""
import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..chat_log import extract_message_payload, save_chat_log_async
from ..config import RATE_LIMIT_PER_MINUTE
from ..rate_limit import rate_limit_check, rate_limit_should_notify
from ..state import ensure_state_loaded

logger = logging.getLogger(__name__)


async def capture_incoming_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    if chat_id is None:
        return
    await ensure_state_loaded(chat_id)
    text, message_type = extract_message_payload(update)
    await save_chat_log_async(
        direction="incoming",
        chat_id=chat_id,
        message_text=text,
        message_type=message_type,
        telegram_user_id=user.id if user else None,
        telegram_username=user.username if user else None,
        telegram_full_name=user.full_name if user else None,
        update_id=update.update_id,
    )

    if not rate_limit_check(chat_id):
        if rate_limit_should_notify(chat_id):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Bạn đang gửi quá nhanh. Giới hạn: {RATE_LIMIT_PER_MINUTE} tin/phút. "
                        "Chờ một chút rồi thử lại nhé."
                    ),
                )
            except Exception as e:
                logger.debug("rate-limit notify fail: %s", e)
        raise ApplicationHandlerStop
