"""Load biến môi trường và expose hằng số cho toàn bộ bot."""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ---- Chat AI (OpenAI / Deepseek) ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
_default_model = (
    "deepseek-chat"
    if (OPENAI_BASE_URL and "deepseek" in OPENAI_BASE_URL.lower())
    else "gpt-4o-mini"
)
AI_MODEL = os.getenv("AI_MODEL", _default_model).strip() or _default_model

# ---- Embedding (RAG) ----
OPENAI_EMBEDDING_API_KEY = (os.getenv("OPENAI_EMBEDDING_API_KEY") or "").strip() or None
OPENAI_EMBEDDING_BASE_URL = (
    (os.getenv("OPENAI_EMBEDDING_BASE_URL") or "").strip() or "https://api.openai.com/v1"
)
EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small").strip()

# ---- Supabase ----
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip() or None
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip() or None
SUPABASE_RAG_BUCKET = (os.getenv("SUPABASE_RAG_BUCKET") or "documents").strip()
SUPABASE_RAG_TABLE = (os.getenv("SUPABASE_RAG_TABLE") or "rag_chunks").strip()
SUPABASE_CHAT_LOG_TABLE = (
    (os.getenv("SUPABASE_CHAT_LOG_TABLE") or "telegram_chat_logs").strip()
    or "telegram_chat_logs"
)
SUPABASE_USER_TABLE = (os.getenv("SUPABASE_USER_TABLE") or "user").strip() or "user"
SUPABASE_MEMBERS_TABLE = (os.getenv("SUPABASE_MEMBERS_TABLE") or "members").strip() or "members"
SUPABASE_STATE_TABLE = (os.getenv("SUPABASE_STATE_TABLE") or "bot_state").strip() or "bot_state"

# ---- Google Calendar ----
GCALENDAR_TZ = (
    (os.getenv("GCALENDAR_TZ") or os.getenv("MS_CALENDAR_TZ") or "Asia/Ho_Chi_Minh").strip()
    or "Asia/Ho_Chi_Minh"
)
GOOGLE_SERVICE_ACCOUNT_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip() or None
GOOGLE_OAUTH_CLIENT_ID = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None
GOOGLE_OAUTH_CLIENT_SECRET = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip() or None
GCAL_MASTER_EMAIL = (os.getenv("GCAL_MASTER_EMAIL") or "").strip() or None
GCAL_MASTER_REFRESH_TOKEN = (os.getenv("GCAL_MASTER_REFRESH_TOKEN") or "").strip() or None
DAILY_CALENDAR_HOUR = max(0, min(23, int(os.getenv("DAILY_CALENDAR_HOUR", "7"))))
DAILY_CALENDAR_MINUTE = max(0, min(59, int(os.getenv("DAILY_CALENDAR_MINUTE", "0"))))
MEETING_HIDE_EMAILS_RAW = (os.getenv("MEETING_HIDE_EMAILS") or "").strip()

# ---- Đăng ký /dk ----
DEFAULT_REGISTRATION_USEREMAIL = (
    (os.getenv("DEFAULT_REGISTRATION_USEREMAIL") or "").strip() or None
)
DEFAULT_REGISTRATION_GCAL_REFRESH = (
    os.getenv("DEFAULT_REGISTRATION_GCAL_REFRESH_TOKEN") or ""
).strip()

# ---- Rate limit ----
RATE_LIMIT_PER_MINUTE = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "20")))
RATE_LIMIT_BURST = max(1, int(os.getenv("RATE_LIMIT_BURST", "5")))

# ---- RAG tuning ----
RAG_CHUNK_SIZE = max(100, min(2000, int(os.getenv("RAG_CHUNK_SIZE", "800"))))
RAG_CHUNK_OVERLAP = max(0, min(200, int(os.getenv("RAG_CHUNK_OVERLAP", "100"))))
RAG_TOP_K = max(1, min(20, int(os.getenv("RAG_TOP_K", "8"))))
RAG_EMBEDDING_BATCH = max(1, min(100, int(os.getenv("RAG_EMBEDDING_BATCH", "50"))))

# ---- Conversation history ----
MAX_HISTORY = 20
MAX_QUERY_HISTORY = 10
