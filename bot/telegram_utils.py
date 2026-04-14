"""Tiện ích Telegram: escape markdown, safe reply."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MD_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape_md_v2(text: str) -> str:
    """Escape đầy đủ cho Telegram MarkdownV2."""
    out = []
    for ch in text or "":
        if ch in _MD_V2_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


async def reply_safe(message, text: str, parse_mode: Optional[str] = None, **kwargs) -> None:
    """Reply với parse_mode; nếu Telegram báo lỗi parse thì gửi lại plain text."""
    try:
        await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        if parse_mode is None:
            raise
        logger.debug("reply parse fail (%s), retry plain: %s", parse_mode, e)
        try:
            await message.reply_text(text, **kwargs)
        except Exception as e2:
            logger.warning("reply plain fail: %s", e2)
