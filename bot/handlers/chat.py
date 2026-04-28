"""Free-chat handler: intent routing (calendar/meeting) rồi fallback LLM."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..cal.intent import (
    is_calendar_intent,
    is_meeting_detail_intent,
    resolve_day_keyword,
)
from ..clients import get_openai_client
from ..config import AI_MODEL
from ..state import add_to_conversation, get_messages_for_user, user_thinking
from ..telegram_utils import reply_safe
from .calendar_cmd import answer_calendar_question, answer_meeting_detail_question

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text or not user_text.strip():
        return

    if is_meeting_detail_intent(user_text):
        handled = await answer_meeting_detail_question(update, user_text.strip())
        if handled:
            return

    if is_calendar_intent(user_text):
        target_day, _ = resolve_day_keyword(user_text)
        if target_day is not None:
            handled = await answer_calendar_question(update, user_text.strip(), custom_question=user_text.strip())
            if handled:
                return
        await update.message.reply_text(
            "Mình nhận ra bạn đang hỏi lịch, nhưng chưa rõ ngày. "
            "Bạn thử ghi rõ: hôm nay, ngày mai, ngày kia, thứ mấy, hoặc 26/03."
        )
        return

    await update.message.chat.send_action("typing")

    try:
        client = get_openai_client()
        history = get_messages_for_user(chat_id)
        thinking_on = user_thinking.get(chat_id, False)
        if thinking_on:
            system = (
                "Bạn là trợ lý hữu ích. Khi trả lời, LUÔN làm theo đúng format sau:\n\n"
                "**Suy nghĩ:**\n(Trình bày từng bước suy luận, phân tích câu hỏi, cân nhắc các khả năng trước khi kết luận. Dùng tiếng Việt.)\n\n"
                "**Trả lời:**\n(Câu trả lời ngắn gọn, rõ ràng dựa trên phần suy nghĩ trên.)\n\n"
                "Nếu câu hỏi đơn giản thì phần Suy nghĩ có thể ngắn, nhưng luôn có đủ hai phần."
            )
        else:
            system = "Bạn là trợ lý hữu ích. Trả lời ngắn gọn, rõ ràng."
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
        )
        reply = response.choices[0].message.content

        add_to_conversation(chat_id, "user", user_text)
        add_to_conversation(chat_id, "assistant", reply)

        use_md = thinking_on and len(reply) <= 4000
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i : i + 4000])
        else:
            await reply_safe(
                update.message,
                reply,
                parse_mode="Markdown" if use_md else None,
            )

    except Exception as e:
        logger.exception("Lỗi khi gọi AI: %s", e)
        await update.message.reply_text(
            f"Có lỗi khi gọi AI: {str(e)}\nKiểm tra API key và .env."
        )
