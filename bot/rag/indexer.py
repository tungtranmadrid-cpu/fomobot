"""Index Supabase Storage vào rag_chunks (chunk + embed + insert)."""
import logging
from typing import Any, List, Optional, Tuple

from openai import OpenAI

from ..config import SUPABASE_RAG_TABLE
from .chunker import chunk_text
from .embedding import embedding_to_text, get_embeddings
from .extractors import decode_file_content, extract_pdf_text, fitz, list_storage_files

logger = logging.getLogger(__name__)


def rag_index_storage(sb: Any, bucket: str, embedding_client: Optional[OpenAI] = None) -> Tuple[int, str]:
    """Quét Storage, chunk, embed (nếu có API), rồi insert vào rag_chunks."""
    paths = list_storage_files(sb, bucket)
    text_ext = (".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html", ".htm", ".xml", ".yaml", ".yml", ".rst")
    all_chunks: List[Tuple[str, str]] = []
    errors = []
    for path in paths:
        is_pdf = path.lower().endswith(".pdf")
        is_text = any(path.lower().endswith(ext) for ext in text_ext)
        if not is_pdf and not is_text:
            continue
        try:
            raw = sb.storage.from_(bucket).download(path)
            if not raw:
                continue
            data = bytes(raw) if not isinstance(raw, bytes) else raw
            if is_pdf:
                if not fitz:
                    errors.append(f"{path}: cần cài PyMuPDF (pip install PyMuPDF)")
                    continue
                text = extract_pdf_text(data)
                if not text:
                    errors.append(f"{path}: không trích xuất được text từ PDF")
                    continue
            else:
                text = decode_file_content(data, path)
                if not text:
                    errors.append(f"{path}: không decode được text")
                    continue
            chunks = chunk_text(text)
            if not chunks:
                continue
            for c in chunks:
                all_chunks.append((path, c))
        except Exception as e:
            errors.append(f"{path}: {e}")

    if not all_chunks:
        return 0, f"Không có file text/PDF nào trong bucket (đã quét {len(paths)} file). " + ("Lỗi: " + "; ".join(errors[:3]) if errors else "")

    contents = [c for _, c in all_chunks]
    embeddings: List[List[float]] = []
    if embedding_client:
        embeddings = get_embeddings(embedding_client, contents)
        if len(embeddings) != len(contents):
            errors.append("Số embedding không khớp số chunk; kiểm tra OPENAI_EMBEDDING_API_KEY và EMBEDDING_MODEL.")
    else:
        embeddings = [[] for _ in contents]

    # Xóa dữ liệu cũ (re-index toàn bộ)
    try:
        sb.rpc("truncate_rag_chunks", {}).execute()
    except Exception as e:
        logger.warning("RAG clear old chunks: %s", e)

    batch_size = 100
    inserted = 0
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i : i + batch_size]
        embs = embeddings[i : i + batch_size]
        rows = []
        for j, (source, content) in enumerate(batch):
            row = {"source": source, "content": content}
            if j < len(embs) and embs[j]:
                row["embedding"] = embedding_to_text(embs[j])
            rows.append(row)
        try:
            sb.table(SUPABASE_RAG_TABLE).insert(rows).execute()
            inserted += len(rows)
        except Exception as e:
            errors.append(f"insert batch: {e}")

    msg = f"Đã index {len(paths)} file, {inserted} chunk (embedding: {'có' if embedding_client else 'không'})."
    if errors:
        msg += " Lỗi: " + "; ".join(errors[:5])
        if len(errors) > 5:
            msg += f" (+{len(errors) - 5} lỗi khác)"
    return inserted, msg
