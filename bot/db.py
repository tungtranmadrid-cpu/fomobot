"""Schema introspection + execute read-only SQL qua RPC."""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_cached_schema: Optional[str] = None


def fetch_db_schema(sb: Any) -> str:
    global _cached_schema
    if _cached_schema:
        return _cached_schema
    try:
        r = sb.rpc("get_schema_info", {}).execute()
        rows = (r.data or []) if hasattr(r, "data") else []
        if not rows:
            return "(Không lấy được schema. Chạy QUERY_SETUP.sql trong Supabase SQL Editor.)"
        tables: Dict[str, List[str]] = {}
        for row in rows:
            tbl = row.get("table_name", "")
            col = row.get("column_name", "")
            dtype = row.get("data_type", "")
            nullable = row.get("is_nullable", "")
            desc = f"{col} ({dtype}{', nullable' if nullable == 'YES' else ''})"
            tables.setdefault(tbl, []).append(desc)
        lines = []
        for tbl, cols in sorted(tables.items()):
            lines.append(f"TABLE {tbl}: {', '.join(cols)}")
        _cached_schema = "\n".join(lines)
        return _cached_schema
    except Exception as e:
        logger.warning("fetch_db_schema: %s", e)
        return f"(Lỗi lấy schema: {e}. Chạy QUERY_SETUP.sql trong Supabase SQL Editor.)"


def execute_sql(sb: Any, sql: str) -> Tuple[List[dict], Optional[str]]:
    try:
        r = sb.rpc("execute_readonly_sql", {"query": sql}).execute()
        data = r.data if hasattr(r, "data") else []
        if isinstance(data, list):
            return data, None
        return [], None
    except Exception as e:
        return [], str(e)


def refresh_schema_cache() -> None:
    global _cached_schema
    _cached_schema = None
