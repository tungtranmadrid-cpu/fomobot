"""Handlers cơ bản: /start /clear /id /think /model."""
from telegram import Update
from telegram.ext import ContextTypes

from ..calendar.fetch import gcalendar_ready
from ..clients import get_supabase_client
from ..config import (
    AI_MODEL,
    DAILY_CALENDAR_HOUR,
    DAILY_CALENDAR_MINUTE,
    GCALENDAR_TZ,
    OPENAI_BASE_URL,
    SUPABASE_RAG_BUCKET,
)
from ..state import query_history, schedule_save_state, user_conversations, user_thinking


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "Chào! Gửi tin nhắn để chat với AI.",
        "/clear - Xóa lịch sử hội thoại.",
        "/model - Xem model đang dùng.",
        "/think - Bật/tắt chế độ suy nghĩ (reasoning): AI sẽ trình bày bước suy luận trước khi trả lời.",
    ]
    if gcalendar_ready():
        lines.append(
            f"(Bot sẽ gửi lịch Google Calendar mỗi ngày khoảng {DAILY_CALENDAR_HOUR:02d}:{DAILY_CALENDAR_MINUTE:02d} giờ {GCALENDAR_TZ} nếu bạn có trong Supabase.)"
        )
        lines.append("/lich <nay|mai> - Xem lịch Google Calendar đã qua AI duyệt.")
        lines.append(
            "Chi tiết cuộc họp: hỏi thành viên / tài liệu / link (kèm ngày nếu cần) — bot lấy từ Google Calendar."
        )
    if get_supabase_client():
        lines.append("/dk - Đăng ký tự động (username + email công ty), chờ admin duyệt.")
        lines.append("/query <câu hỏi> - Truy vấn CSDL bằng ngôn ngữ tự nhiên.")
        lines.append("/tables - Xem cấu trúc CSDL (bảng, cột).")
        lines.append("/refresh - Cập nhật lại cache schema.")
        lines.append("/id - Xem Chat ID (để thêm vào bảng user / lịch Google Calendar).")
    if get_supabase_client() and SUPABASE_RAG_BUCKET:
        lines.append("/rag_index - Index file trong Supabase Storage vào RAG.")
        lines.append("/ask <câu hỏi> - Trả lời dựa trên tài liệu đã index (RAG).")
    await update.message.reply_text("\n".join(lines))


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_conversations.pop(chat_id, None)
    query_history.pop(chat_id, None)
    schedule_save_state(chat_id)
    await update.message.reply_text("Đã xóa lịch sử hội thoại và lịch sử truy vấn.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chat ID dùng cho cột telegram_ID trong bảng user (Supabase)."""
    cid = update.effective_chat.id
    chat_type = update.effective_chat.type
    user = update.effective_user
    # Không dùng Markdown/HTML: username có dấu _ hoặc ký tự đặc biệt sẽ làm Telegram báo lỗi parse entity.
    parts = [f"Chat ID: {cid}"]
    if user and user.username:
        parts.append(f"Username: @{user.username}")
    if chat_type == "private":
        parts.append(
            "Trong chat riêng, số trên là ID của bạn — dán vào cột telegram_ID (text) "
            "khi thêm dòng trong bảng user trên Supabase."
        )
    else:
        parts.append(
            "Đây là ID nhóm/kênh. Lịch cá nhân thường cấu hình bằng Chat ID lấy từ tin nhắn riêng với bot."
        )
    await update.message.reply_text("\n".join(parts))


async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bật/tắt chế độ thinking: AI suy nghĩ từng bước trước khi trả lời."""
    chat_id = update.effective_chat.id
    current = user_thinking.get(chat_id, False)
    user_thinking[chat_id] = not current
    new_state = user_thinking[chat_id]
    schedule_save_state(chat_id)
    if new_state:
        await update.message.reply_text(
            "Đã bật chế độ **Suy nghĩ** (reasoning).\n"
            "Từ giờ mỗi khi bạn chat, AI sẽ trình bày phần suy luận trước, rồi mới đưa ra câu trả lời.\n"
            "Gõ /think lần nữa để tắt.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Đã tắt chế độ Suy nghĩ. Chat sẽ trả lời trực tiếp.\nGõ /think để bật lại."
        )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    base = OPENAI_BASE_URL or "api.openai.com"
    await update.message.reply_text(f"Model: {AI_MODEL}\nBase URL: {base}")
