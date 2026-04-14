"""Handlers RAG: /rag_index + /ask."""
import logging
from typing import List

from telegram import Update
from telegram.ext import ContextTypes

from ..clients import get_embedding_client, get_openai_client, get_supabase_client
from ..config import AI_MODEL, RAG_TOP_K, SUPABASE_RAG_BUCKET, SUPABASE_RAG_TABLE
from ..rag.embedding import get_embeddings
from ..rag.indexer import rag_index_storage
from ..rag.search import extract_keywords_from_question, rag_keyword_search, rag_vector_search

logger = logging.getLogger(__name__)


async def cmd_rag_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase (SUPABASE_URL, SUPABASE_KEY).")
        return
    emb_client = get_embedding_client()
    if not emb_client:
        await update.message.reply_text(
            "Chưa cấu hình OPENAI_EMBEDDING_API_KEY trong .env. "
            "RAG cần API key OpenAI riêng để embed tài liệu (xem hướng dẫn trong .env.example)."
        )
        return
    await update.message.reply_text("Đang quét Storage, tạo embedding và lưu... Vui lòng đợi.")
    try:
        total, msg = rag_index_storage(sb, SUPABASE_RAG_BUCKET, embedding_client=emb_client)
        await update.message.reply_text(msg)
    except Exception as e:
        logger.exception("rag_index: %s", e)
        await update.message.reply_text(f"Lỗi: {str(e)}. Kiểm tra bucket '{SUPABASE_RAG_BUCKET}', bảng '{SUPABASE_RAG_TABLE}' và QUERY_SETUP.sql (pgvector, search_rag_by_embedding).")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trả lời câu hỏi dựa trên tài liệu đã index (RAG). Ưu tiên tìm theo embedding."""
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return
    question = context.args or []
    if not question:
        await update.message.reply_text("Dùng: /ask <câu hỏi>\nVí dụ: /ask chính sách bảo hành là gì?")
        return
    user_question = " ".join(question).strip()
    if not user_question:
        await update.message.reply_text("Vui lòng nhập câu hỏi sau /ask.")
        return

    await update.message.chat.send_action("typing")

    try:
        chat_client = get_openai_client()
        emb_client = get_embedding_client()
        chunks: List[dict] = []

        if emb_client:
            q_emb = get_embeddings(emb_client, [user_question], batch_size=1)
            if q_emb and q_emb[0]:
                chunks = rag_vector_search(sb, q_emb[0], top_k=RAG_TOP_K)
                logger.info("RAG vector search: %s chunks", len(chunks))
        if not chunks and chat_client:
            keywords = extract_keywords_from_question(chat_client, user_question)
            chunks = rag_keyword_search(sb, keywords, top_k=RAG_TOP_K)
            logger.info("RAG keyword fallback: %s", keywords)

        if not chunks:
            await update.message.reply_text(
                "Không tìm thấy tài liệu liên quan. Chạy /rag_index (cần OPENAI_EMBEDDING_API_KEY) để index file trước."
            )
            return
        context_parts = []
        for i, row in enumerate(chunks, 1):
            content = (row.get("content") or "").strip()
            source = (row.get("source") or "").strip()
            if content:
                context_parts.append(f"[{i}] (nguồn: {source})\n{content}")
        context_text = "\n\n---\n\n".join(context_parts)
        system = (
            "Bạn trả lời câu hỏi CHỈ dựa trên ngữ cảnh tài liệu được cung cấp bên dưới. "
            "Nếu ngữ cảnh không đủ để trả lời, hãy nói rõ. Trả lời ngắn gọn, rõ ràng, bằng tiếng Việt."
        )
        user_msg = f"Ngữ cảnh tài liệu:\n\n{context_text}\n\nCâu hỏi: {user_question}"
        resp = chat_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        reply = (resp.choices[0].message.content or "").strip()
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i : i + 4000])
        else:
            await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("cmd_ask: %s", e)
        await update.message.reply_text(f"Có lỗi: {str(e)}")
