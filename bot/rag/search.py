"""Tìm kiếm RAG: vector similarity + keyword fallback + trích keyword."""
import logging
from typing import Any, List

from openai import OpenAI

from ..config import AI_MODEL, RAG_TOP_K
from .embedding import embedding_to_text

logger = logging.getLogger(__name__)


def rag_vector_search(sb: Any, embedding: List[float], top_k: int = RAG_TOP_K) -> List[dict]:
    """Tìm chunk theo độ tương đồng vector (RPC search_rag_by_embedding)."""
    if not embedding or len(embedding) != 1536:
        return []
    try:
        r = sb.rpc(
            "search_rag_by_embedding",
            {"query_embedding_text": embedding_to_text(embedding), "match_count": top_k},
        ).execute()
        return (r.data or []) if hasattr(r, "data") else []
    except Exception as e:
        logger.warning("RAG vector search: %s", e)
        return []


def rag_keyword_search(sb: Any, keywords: List[str], top_k: int = RAG_TOP_K) -> List[dict]:
    """Tìm chunk bằng từ khóa (fallback khi không có embedding)."""
    if not keywords:
        return []
    try:
        r = sb.rpc("search_rag_chunks", {"keywords": keywords, "match_count": top_k}).execute()
        return (r.data or []) if hasattr(r, "data") else []
    except Exception as e:
        logger.warning("RAG keyword search: %s", e)
        return []


def extract_keywords_from_question(client: OpenAI, question: str) -> List[str]:
    """Trích từ khóa từ câu hỏi (fallback khi không dùng embedding)."""
    try:
        resp = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Trích xuất 3-8 từ khóa quan trọng từ câu hỏi để tìm kiếm trong tài liệu. "
                    "Trả về CHỈ các từ khóa, mỗi từ cách nhau bằng dấu phẩy, không giải thích."
                )},
                {"role": "user", "content": question},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        keywords = [k.strip() for k in raw.split(",") if k.strip()]
        return keywords[:10]
    except Exception as e:
        logger.warning("extract_keywords: %s", e)
        return question.split()[:5]
