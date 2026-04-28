"""Factory cho OpenAI (chat) và Supabase client."""
import logging
from typing import Any, Optional

from openai import OpenAI

from .config import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    SUPABASE_KEY,
    SUPABASE_URL,
)

logger = logging.getLogger(__name__)

try:
    from supabase import create_client, Client  # type: ignore
except ImportError:
    create_client = None  # type: ignore
    Client = None  # type: ignore


def get_openai_client() -> OpenAI:
    """Client cho chat (dùng OPENAI_API_KEY, có thể trỏ Deepseek)."""
    kwargs = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        kwargs["base_url"] = OPENAI_BASE_URL
    return OpenAI(**kwargs)


def get_supabase_client() -> Optional[Any]:
    if not create_client or not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.warning("Không tạo được Supabase client: %s", e)
        return None
