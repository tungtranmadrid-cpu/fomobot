"""Gọi OpenAI embedding API + serialize vector cho RPC."""
import logging
from typing import List

from openai import OpenAI

from ..config import EMBEDDING_MODEL, RAG_EMBEDDING_BATCH

logger = logging.getLogger(__name__)


def get_embeddings(client: OpenAI, texts: List[str], batch_size: int = RAG_EMBEDDING_BATCH) -> List[List[float]]:
    """Gọi API embedding (OpenAI), trả về list vector 1536 chiều. Batch để tránh quá tải."""
    out: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        try:
            r = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            for e in (r.data or []):
                emb = getattr(e, "embedding", None)
                if emb is not None:
                    out.append(emb)
                else:
                    out.append([])
        except Exception as e:
            logger.warning("get_embeddings batch %s: %s", i, e)
            for _ in batch:
                out.append([])
    return out


def embedding_to_text(emb: List[float]) -> str:
    """Chuyển list float sang chuỗi '[a,b,c,...]' để gửi RPC vector."""
    return "[" + ",".join(str(x) for x in emb) + "]"
