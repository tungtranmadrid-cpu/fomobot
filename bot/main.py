"""Entrypoint: build Application, đăng ký handlers, run_polling."""
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .calendar.reminder import post_init_schedule
from .chat_log import LoggingBot
from .config import OPENAI_API_KEY, TELEGRAM_BOT_TOKEN
from .handlers.basic import cmd_clear, cmd_id, cmd_model, cmd_start, cmd_think
from .handlers.calendar_cmd import (
    MEET_PICK_PREFIX,
    cmd_lich,
    cmd_tomtat,
    on_meeting_pick_callback,
)
from .handlers.capture import capture_incoming_update
from .handlers.chat import handle_message
from .handlers.db_cmd import cmd_query, cmd_refresh, cmd_tables
from .handlers.rag_cmd import cmd_ask, cmd_rag_index
from .handlers.registration import (
    REG_EMAIL,
    REG_USERNAME,
    cmd_dk,
    reg_cancel,
    reg_email,
    reg_username,
    registration_callback,
)

logger = logging.getLogger(__name__)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Thiếu TELEGRAM_BOT_TOKEN trong .env")
    if not OPENAI_API_KEY:
        raise SystemExit("Thiếu OPENAI_API_KEY trong .env")

    request = HTTPXRequest(connection_pool_size=16)
    logging_bot = LoggingBot(token=TELEGRAM_BOT_TOKEN, request=request)

    app = (
        Application.builder()
        .bot(logging_bot)
        .post_init(post_init_schedule)
        .build()
    )
    app.add_handler(TypeHandler(Update, capture_incoming_update), group=-1)
    dk_conv = ConversationHandler(
        entry_points=[CommandHandler("dk", cmd_dk)],
        states={
            REG_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_username)],
            REG_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_email)],
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)],
        name="registration_dk",
        per_message=False,
    )
    app.add_handler(dk_conv)
    app.add_handler(CallbackQueryHandler(registration_callback, pattern=r"^reg:(approve|reject):"))
    app.add_handler(CallbackQueryHandler(on_meeting_pick_callback, pattern=rf"^{MEET_PICK_PREFIX}"))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("lich", cmd_lich))
    app.add_handler(CommandHandler("tomtat", cmd_tomtat))
    app.add_handler(CommandHandler("think", cmd_think))
    app.add_handler(CommandHandler("tables", cmd_tables))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("query", cmd_query))
    app.add_handler(CommandHandler("rag_index", cmd_rag_index))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot đang chạy...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
