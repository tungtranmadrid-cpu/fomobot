"""
Lấy các chat_id xuất hiện trong getUpdates của bot (token đọc từ .env).

Lưu ý Telegram API:
- Không có endpoint "danh sách mọi user đã từng chat"; chỉ thấy các update
  mà server còn giữ (thường trong khoảng 24 giờ gần đây với getUpdates).
- Nếu bot đang bật webhook, getUpdates thường rỗng — script sẽ gọi deleteWebhook
  (tùy chọn) để có thể poll getUpdates.

Cách chạy (từ thư mục jkv):
  python list_telegram_chat_ids.py
  python list_telegram_chat_ids.py --no-delete-webhook
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Tránh lỗi UnicodeEncodeError khi in tiếng Việt trên Windows (cp1252)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_token() -> str:
    load_dotenv(ENV_PATH)
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Thiếu TELEGRAM_BOT_TOKEN trong .env", file=sys.stderr)
        sys.exit(1)
    return token


def _api_json(token: str, method: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    base = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        q = urllib.parse.urlencode(params)
        url = f"{base}?{q}"
    else:
        url = base
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"Lỗi HTTP {e.code}: {err_body}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(body)
    if not data.get("ok"):
        print(f"API lỗi: {data}", file=sys.stderr)
        sys.exit(1)
    return data


def _collect_chats_from_update(update: Dict[str, Any]) -> List[Tuple[int, Optional[str], str]]:
    """Trả về danh sách (chat_id, type, label) từ một update."""
    found: List[Tuple[int, Optional[str], str]] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            chat = obj.get("chat")
            if isinstance(chat, dict) and "id" in chat:
                cid = chat["id"]
                ctype = chat.get("type")
                label = (
                    chat.get("title")
                    or chat.get("username")
                    or " ".join(
                        filter(
                            None,
                            [chat.get("first_name"), chat.get("last_name")],
                        )
                    )
                    or ""
                )
                if isinstance(cid, int):
                    found.append((cid, ctype if isinstance(ctype, str) else None, str(label)))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(update)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Liệt kê chat_id từ getUpdates (Telegram).")
    parser.add_argument(
        "--no-delete-webhook",
        action="store_true",
        help="Không gọi deleteWebhook trước khi getUpdates.",
    )
    args = parser.parse_args()

    token = _load_token()

    if not args.no_delete_webhook:
        _api_json(token, "deleteWebhook", {"drop_pending_updates": "false"})

    offset = 0
    by_id: Dict[int, Tuple[Optional[str], str]] = {}

    while True:
        data = _api_json(
            token,
            "getUpdates",
            {"timeout": "0", "limit": "100", "offset": str(offset)},
        )
        updates = data.get("result") or []
        if not updates:
            break
        for u in updates:
            if not isinstance(u, dict):
                continue
            uid = u.get("update_id")
            for cid, ctype, label in _collect_chats_from_update(u):
                by_id[cid] = (ctype, label)
            if isinstance(uid, int):
                offset = uid + 1

    if not by_id:
        print(
            "Không có update nào. Có thể: chưa ai nhắn bot gần đây, "
            "webhook vẫn đang nhận (thử bỏ --no-delete-webhook), hoặc bot khác đang getUpdates.",
        )
        return

    print(f"Tìm thấy {len(by_id)} chat_id:\n")
    for cid in sorted(by_id.keys()):
        ctype, label = by_id[cid]
        extra = f" | type={ctype}" if ctype else ""
        name = f" | {label}" if label else ""
        print(f"{cid}{extra}{name}")


if __name__ == "__main__":
    main()
