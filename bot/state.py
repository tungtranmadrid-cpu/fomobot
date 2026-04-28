"""State per chat_id — cache RAM, sync với bảng bot_state trên Supabase."""
import asyncio
import logging
from typing import Dict, List

from .async_utils import run_blocking
from .clients import get_supabase_client
from .config import MAX_HISTORY, SUPABASE_STATE_TABLE

logger = logging.getLogger(__name__)

# chat_id -> list messages (role/content)
user_conversations: Dict[int, List[dict]] = {}

# chat_id -> True/False (chế độ reasoning)
user_thinking: Dict[int, bool] = {}

# Đánh dấu chat_id đã load state từ DB
_state_loaded: set = set()


async def ensure_state_loaded(chat_id: int) -> None:
    """Nạp state cho chat_id từ Supabase lần đầu tiên (chạy ở thread pool)."""
    if chat_id in _state_loaded:
        return
    _state_loaded.add(chat_id)
    sb = get_supabase_client()
    if sb is None:
        return
    try:
        r = await run_blocking(
            lambda: sb.table(SUPABASE_STATE_TABLE)
            .select("conversation,thinking")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        rows = r.data if hasattr(r, "data") else []
        if rows:
            row = rows[0]
            conv = row.get("conversation")
            if isinstance(conv, list):
                user_conversations[chat_id] = conv
            user_thinking[chat_id] = bool(row.get("thinking"))
    except Exception as e:
        logger.debug("load state %s: %s", chat_id, e)


def schedule_save_state(chat_id: int) -> None:
    """Fire-and-forget upsert state xuống Supabase."""
    sb = get_supabase_client()
    if sb is None:
        return
    payload = {
        "chat_id": chat_id,
        "conversation": user_conversations.get(chat_id, []),
        "thinking": user_thinking.get(chat_id, False),
    }

    async def _save():
        try:
            await run_blocking(
                lambda: sb.table(SUPABASE_STATE_TABLE)
                .upsert(payload, on_conflict="chat_id")
                .execute()
            )
        except Exception as e:
            logger.debug("save state %s: %s", chat_id, e)

    try:
        asyncio.create_task(_save())
    except RuntimeError:
        pass


def get_messages_for_user(chat_id: int) -> List[dict]:
    if chat_id not in user_conversations:
        return []
    return user_conversations[chat_id][-MAX_HISTORY:]


def add_to_conversation(chat_id: int, role: str, content: str) -> None:
    if chat_id not in user_conversations:
        user_conversations[chat_id] = []
    user_conversations[chat_id].append({"role": role, "content": content})
    user_conversations[chat_id] = user_conversations[chat_id][-MAX_HISTORY:]
    schedule_save_state(chat_id)
