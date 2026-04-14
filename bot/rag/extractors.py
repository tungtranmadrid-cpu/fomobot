"""Đọc file từ Supabase Storage + decode text / extract PDF."""
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore


def list_storage_files(sb: Any, bucket: str, prefix: str = "") -> List[str]:
    out: List[str] = []
    try:
        opts = {"limit": 1000}
        path = prefix if prefix else ""
        resp = sb.storage.from_(bucket).list(path, opts)
        if hasattr(resp, "data"):
            resp = resp.data
        if not resp or not isinstance(resp, list):
            return out
        for item in resp:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            fpath = f"{prefix}/{name}" if prefix else name
            is_file = "." in name
            is_folder = isinstance(item.get("metadata"), dict) and (item.get("metadata") or {}).get("mimetype") == "application/folder"
            if is_file and not is_folder:
                out.append(fpath)
            elif is_folder or not is_file:
                sub = list_storage_files(sb, bucket, fpath)
                out.extend(sub)
            else:
                out.append(fpath)
    except Exception as e:
        logger.warning("Storage list %s/%s: %s", bucket, prefix, e)
    return out


def decode_file_content(data: bytes, path: str) -> Optional[str]:
    for enc in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def extract_pdf_text(data: bytes) -> Optional[str]:
    """Trích xuất toàn bộ text từ file PDF (dùng PyMuPDF)."""
    if not fitz:
        return None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page in doc:
            text = page.get_text("text")
            if text and text.strip():
                pages.append(text.strip())
        doc.close()
        if pages:
            return "\n\n".join(pages)
    except Exception as e:
        logger.warning("PDF extract error: %s", e)
    return None
