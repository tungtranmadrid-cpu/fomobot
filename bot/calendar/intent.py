"""Nhận diện ý định lịch/họp + parse ngày tiếng Việt + chọn sự kiện bằng AI."""
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from ..clients import get_openai_client
from ..config import AI_MODEL, GCALENDAR_TZ

logger = logging.getLogger(__name__)


def resolve_day_keyword(raw: str) -> Tuple[Optional[date], Optional[str]]:
    tz = ZoneInfo(GCALENDAR_TZ)
    today = datetime.now(tz).date()
    val = (raw or "").strip().lower()
    normalized = unicodedata.normalize("NFD", val)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    m = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)", val)
    if m:
        dd = int(m.group(1))
        mm = int(m.group(2))
        yy_raw = m.group(3)
        yy = today.year
        if yy_raw:
            yy = int(yy_raw)
            if yy < 100:
                yy += 2000
        try:
            return date(yy, mm, dd), None
        except ValueError:
            return None, "Ngày không hợp lệ. Ví dụ đúng: 26/03 hoặc 26/03/2026."

    if any(x in normalized for x in ("hom nay", "ngay nay", "today")) or normalized == "nay":
        return today, None
    if any(x in normalized for x in ("ngay mai", "tomorrow")) or normalized == "mai":
        return today + timedelta(days=1), None
    if any(x in normalized for x in ("ngay kia", "hom kia")):
        return today + timedelta(days=2), None
    if "mai mot" in normalized or "mai mốt" in val:
        return today + timedelta(days=2), None
    if "cuoi tuan" in normalized:
        delta = (5 - today.weekday()) % 7
        return today + timedelta(days=delta), None
    if "dau tuan" in normalized:
        delta = (0 - today.weekday()) % 7
        return today + timedelta(days=delta), None

    weekday_map_num = {"2": 0, "3": 1, "4": 2, "5": 3, "6": 4, "7": 5}
    weekday_map_word = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5}
    target_weekday: Optional[int] = None

    m_num = re.search(r"\bthu\s*(2|3|4|5|6|7)\b", normalized)
    if m_num:
        target_weekday = weekday_map_num.get(m_num.group(1))
    else:
        m_word = re.search(r"\bthu\s+(hai|ba|tu|nam|sau|bay)\b", normalized)
        if m_word:
            target_weekday = weekday_map_word.get(m_word.group(1))
        elif re.search(r"\b(chu nhat|cn)\b", normalized):
            target_weekday = 6

    if target_weekday is not None:
        is_next_week = "tuan sau" in normalized or "next week" in normalized
        is_this_week = "tuan nay" in normalized or "this week" in normalized
        monday_this_week = today - timedelta(days=today.weekday())

        if is_next_week:
            monday = monday_this_week + timedelta(days=7)
            return monday + timedelta(days=target_weekday), None
        if is_this_week:
            monday = monday_this_week
            return monday + timedelta(days=target_weekday), None

        delta = (target_weekday - today.weekday()) % 7
        return today + timedelta(days=delta), None

    return None, "Dùng: /lich nay, /lich mai hoặc /lich 26/03"


def is_calendar_intent(user_text: str) -> bool:
    val = (user_text or "").strip().lower()
    normalized = unicodedata.normalize("NFD", val)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    calendar_keywords = ("lich", "calendar", "schedule", "su kien", "lich trinh", "lich lam viec")
    meeting_keywords = ("hop", "meeting", "cuoc hop")
    day_hints = (
        "hom nay", "ngay nay", "mai", "ngay mai", "ngay kia", "hom kia",
        "tuan nay", "tuan sau", "thu ", "chu nhat", "cn", "cuoi tuan", "dau tuan",
    )

    if any(k in normalized for k in calendar_keywords):
        return True
    if any(k in normalized for k in meeting_keywords):
        if any(h in normalized for h in day_hints):
            return True
        if re.search(r"(?<!\d)\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?!\d)", normalized):
            return True
    return False


def is_meeting_detail_intent(user_text: str) -> bool:
    """
    Hỏi chi tiết cuộc họp: thành viên, tài liệu, đính kèm, link, ...
    (khác với chỉ xem lịch trống/tóm tắt ngày).
    """
    val = (user_text or "").strip().lower()
    normalized = unicodedata.normalize("NFD", val)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    detail_markers = (
        "thanh vien",
        "thanh phan",
        "tham gia",
        "tham du",
        "nguoi tham",
        "tai lieu",
        "dinh kem",
        "file dinh",
        "link tai",
        "tai lieu hop",
        "attachment",
        "chi tiet cuoc",
        "thong tin cuoc hop",
        "noi dung hop",
        "ai tham gia",
        "danh sach tham",
        "co chua",
        "da co",
        "upload",
    )
    meeting_markers = (
        "hop",
        "cuoc hop",
        "meeting",
        "su kien",
        "buoi hop",
        "lich hop",
        "calendar",
        "event",
    )
    has_detail = any(d in normalized for d in detail_markers)
    has_meeting = any(m in normalized for m in meeting_markers)
    if has_detail and has_meeting:
        return True
    if "meeting attendee" in normalized or "meeting material" in normalized:
        return True
    return False


def resolve_day_for_meeting_query(user_text: str) -> date:
    """Ngày trong câu hỏi chi tiết; nếu không ghi ngày thì mặc định hôm nay (theo GCALENDAR_TZ)."""
    d, _ = resolve_day_keyword(user_text)
    if d is not None:
        return d
    tz = ZoneInfo(GCALENDAR_TZ)
    return datetime.now(tz).date()


def select_event_index_by_ai(user_question: str, events: List[Dict[str, Any]]) -> int:
    """Chọn chỉ số sự kiện (0..n-1) khớp câu hỏi; -1 nếu không chọn được."""
    if not events:
        return -1
    if len(events) == 1:
        return 0
    try:
        client = get_openai_client()
        titles = []
        for i, ev in enumerate(events):
            t = (ev.get("summary") or "(Không tiêu đề)").strip()
            titles.append(f"{i + 1}. {t}")
        system = (
            "Bạn chọn đúng MỘT sự kiện trong danh sách khớp với câu hỏi người dùng về cuộc họp. "
            "Chỉ trả lời MỘT số nguyên: số thứ tự (1, 2, 3...) hoặc 0 nếu không có sự kiện nào khớp. "
            "Không giải thích thêm."
        )
        user = f"Câu hỏi:\n{user_question}\n\nDanh sách sự kiện trong ngày:\n" + "\n".join(titles)
        resp = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\b(\d+)\b", raw)
        if not m:
            return -1
        n = int(m.group(1))
        if n == 0:
            return -1
        if 1 <= n <= len(events):
            return n - 1
        return -1
    except Exception as e:
        logger.warning("select_event_index_by_ai: %s", e)
        return -1


def summarize_schedule_with_ai(question: str, schedule_text: str) -> str:
    """Nhờ AI duyệt/tóm tắt lịch theo câu hỏi người dùng (đồng bộ — chạy trong thread qua run_blocking)."""
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Bạn là trợ lý lịch làm việc. Dựa vào lịch trong ngày được cung cấp, "
                    "hãy trả lời ngắn gọn bằng tiếng Việt. "
                    "Nếu không có lịch thì nói rõ không có cuộc họp/sự kiện."
                ),
            },
            {
                "role": "user",
                "content": f"Câu hỏi: {question}\n\nDữ liệu lịch:\n{schedule_text}",
            },
        ],
    )
    return (resp.choices[0].message.content or "").strip()
