"""Cắt text thành chunk để embed."""
from typing import List

from ..config import RAG_CHUNK_OVERLAP, RAG_CHUNK_SIZE


def chunk_text(text: str, chunk_size: int = RAG_CHUNK_SIZE, overlap: int = RAG_CHUNK_OVERLAP) -> List[str]:
    if not text or not text.strip():
        return []
    text = text.strip().replace("\r\n", "\n")
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if end < len(text):
            last_br = chunk.rfind("\n")
            if last_br > chunk_size // 2:
                chunk = chunk[: last_br + 1]
                end = start + last_br + 1
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap if overlap > 0 else end
    return chunks
