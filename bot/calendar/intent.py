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


def _normalize_vn(s: str) -> str:
    """Lower + bỏ dấu + gộp khoảng trắng. Dùng cho matching keyword."""
    val = (s or "").strip().lower()
    n = unicodedata.normalize("NFD", val)
    n = "".join(ch for ch in n if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", n).strip()


def resolve_day_keyword(raw: str) -> Tuple[Optional[date], Optional[str]]:
    """
    Parse ngày tiếng Việt. Trả (date, None) nếu ok, (None, msg) nếu lỗi,
    (None, None) nếu không tìm thấy gợi ý ngày (caller tự quyết định fallback).
    """
    tz = ZoneInfo(GCALENDAR_TZ)
    today = datetime.now(tz).date()
    val = (raw or "").strip().lower()
    normalized = _normalize_vn(val)

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

    # hôm kia = -2 (trước đó 2 ngày); ngày kia / mai mốt / mốt = +2
    if re.search(r"\bhom kia\b|\bbua kia\b", normalized):
        return today - timedelta(days=2), None
    if re.search(r"\bhom qua\b|\bbua qua\b|\bhqua\b|yesterday", normalized):
        return today - timedelta(days=1), None
    if re.search(r"\bhom kia \b|\bngay kia\b|\bmai mot\b|\bmai mốt\b|\bmot mai\b", normalized) \
            or normalized in ("mot", "mai mot", "mot ngay nua"):
        return today + timedelta(days=2), None
    if any(x in normalized for x in ("hom nay", "ngay nay", "bua nay", "today")) or normalized in ("nay", "hnay", "h nay"):
        return today, None
    if any(x in normalized for x in ("ngay mai", "bua mai", "sang mai", "chieu mai", "toi mai", "trua mai", "tomorrow")) \
            or normalized in ("mai", "nmai"):
        return today + timedelta(days=1), None

    # cuối/đầu tuần (tuần này / tuần sau / tuần trước)
    week_shift = 0
    if "tuan sau" in normalized or "next week" in normalized:
        week_shift = 7
    elif "tuan truoc" in normalized or "last week" in normalized:
        week_shift = -7

    if "cuoi tuan" in normalized:
        monday_this_week = today - timedelta(days=today.weekday())
        return monday_this_week + timedelta(days=5 + week_shift), None
    if "dau tuan" in normalized:
        monday_this_week = today - timedelta(days=today.weekday())
        return monday_this_week + timedelta(days=week_shift), None

    # Thứ N (hai/ba/tư/năm/sáu/bảy, 2..7, t2..t7, chủ nhật/cn)
    weekday_map_num = {"2": 0, "3": 1, "4": 2, "5": 3, "6": 4, "7": 5}
    weekday_map_word = {
        "hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5, "bẩy": 5, "bay.": 5,
    }
    target_weekday: Optional[int] = None

    m_num = re.search(r"\bt(?:hu)?\s*([2-7])\b", normalized)
    if m_num:
        target_weekday = weekday_map_num.get(m_num.group(1))
    else:
        m_word = re.search(r"\bthu\s+(hai|ba|tu|nam|sau|bay)\b", normalized)
        if m_word:
            target_weekday = weekday_map_word.get(m_word.group(1))
        elif re.search(r"\b(chu nhat|cn|chunhat)\b", normalized):
            target_weekday = 6

    if target_weekday is not None:
        is_next_week = "tuan sau" in normalized or "next week" in normalized
        is_prev_week = "tuan truoc" in normalized or "last week" in normalized
        is_this_week = "tuan nay" in normalized or "this week" in normalized
        monday_this_week = today - timedelta(days=today.weekday())
        if is_next_week:
            return monday_this_week + timedelta(days=7 + target_weekday), None
        if is_prev_week:
            return monday_this_week + timedelta(days=-7 + target_weekday), None
        if is_this_week:
            return monday_this_week + timedelta(days=target_weekday), None
        delta = (target_weekday - today.weekday()) % 7
        return today + timedelta(days=delta), None

    return None, "Dùng: /lich nay, /lich mai hoặc /lich 26/03"


# Time-of-day markers giúp narrow sự kiện khi có nhiều cuộc trong ngày.
_TIME_OF_DAY_MARKERS = {
    "sang": (5, 12),    # sáng: 05:00–12:00
    "trua": (11, 14),   # trưa
    "chieu": (12, 18),  # chiều
    "toi": (18, 23),    # tối
    "dem": (21, 24),    # đêm
    "khuya": (0, 5),
}


def extract_time_of_day(user_text: str) -> Optional[Tuple[int, int]]:
    """Trả (hour_start, hour_end) nếu có từ như 'sáng mai', 'chiều nay'. None nếu không có."""
    n = _normalize_vn(user_text)
    for word, span in _TIME_OF_DAY_MARKERS.items():
        if re.search(rf"\b{word}\b", n):
            return span
    return None


_CALENDAR_KEYWORDS = (
    "lich", "calendar", "schedule", "su kien", "lich trinh", "lich lam viec",
    "agenda", "lich hen",
)
_MEETING_KEYWORDS = (
    "hop", "meeting", "cuoc hop", "buoi hop", "phien hop", "event", "su kien",
    "gap mat", "gap go",
)
_DAY_HINTS = (
    "hom nay", "hnay", "h nay", "ngay nay", "bua nay",
    "mai", "ngay mai", "bua mai",
    "ngay kia", "hom kia", "bua kia", "mai mot", "mot mai",
    "hom qua", "bua qua", "hqua",
    "tuan nay", "tuan sau", "tuan truoc",
    "thu ", "t2", "t3", "t4", "t5", "t6", "t7",
    "chu nhat", "chunhat", "cn",
    "cuoi tuan", "dau tuan",
    "sang mai", "chieu mai", "toi mai", "sang nay", "chieu nay", "toi nay",
)

_DETAIL_MARKERS = (
    "thanh vien", "thanh phan", "tham gia", "tham du", "nguoi tham",
    "ai tham", "ai du", "ai hop", "voi ai", "voi nhung ai",
    "tai lieu", "file dinh", "dinh kem", "attachment", "link tai",
    "chi tiet", "thong tin", "noi dung", "agenda",
    "co chua", "da co", "upload", "thanh phan tham du",
    "danh sach tham", "nguoi du",
)
_MEETING_MARKERS = (
    "hop", "cuoc hop", "meeting", "su kien", "buoi hop", "phien hop",
    "lich hop", "calendar", "event", "cuoc", "buoi", "buoi gap",
)


def is_calendar_intent(user_text: str) -> bool:
    normalized = _normalize_vn(user_text)
    if any(k in normalized for k in _CALENDAR_KEYWORDS):
        return True
    if any(k in normalized for k in _MEETING_KEYWORDS):
        if any(h in normalized for h in _DAY_HINTS):
            return True
        if re.search(r"(?<!\d)\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?!\d)", normalized):
            return True
    return False


def is_meeting_detail_intent(user_text: str) -> bool:
    """
    Hỏi chi tiết cuộc họp: thành viên, tài liệu, link... (khác với xem lịch tổng quát).
    Nới: nếu câu có 'với ai', 'gồm những ai', 'tham gia', ... + keyword họp/lịch thì coi là detail.
    """
    normalized = _normalize_vn(user_text)
    has_detail = any(d in normalized for d in _DETAIL_MARKERS)
    has_meeting = any(m in normalized for m in _MEETING_MARKERS)
    if has_detail and has_meeting:
        return True
    # Pattern: "với ai" / "gồm ai" / "có ai" + context ngày → chắc chắn hỏi detail
    if re.search(r"\b(voi ai|gom ai|co ai|ai tham|ai du)\b", normalized) \
            and (has_meeting or any(h in normalized for h in _DAY_HINTS)):
        return True
    # Pattern: "tài liệu/file" + ngày
    if re.search(r"\b(tai lieu|file|attachment|dinh kem|link)\b", normalized) \
            and (has_meeting or any(h in normalized for h in _DAY_HINTS)):
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


def _build_event_brief(ev: Dict[str, Any], display_tz: str) -> str:
    """Tóm tắt 1 event cho LLM: title | HH:MM-HH:MM | location | attendees_count."""
    title = (ev.get("summary") or "(Không tiêu đề)").strip()
    loc = (ev.get("location") or "").strip()
    attendees = ev.get("attendees") or []
    n_att = len([a for a in attendees if (a.get("email") or "").strip()])
    time_str = ""
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    dt_s = str(start.get("dateTime") or "")
    dt_e = str(end.get("dateTime") or "")
    if dt_s:
        try:
            if dt_s.endswith("Z"):
                dt_s = dt_s[:-1] + "+00:00"
            st = datetime.fromisoformat(dt_s).astimezone(ZoneInfo(display_tz))
            time_str = st.strftime("%H:%M")
            if dt_e:
                if dt_e.endswith("Z"):
                    dt_e = dt_e[:-1] + "+00:00"
                en = datetime.fromisoformat(dt_e).astimezone(ZoneInfo(display_tz))
                time_str += "-" + en.strftime("%H:%M")
        except Exception:
            time_str = ""
    elif "date" in start:
        time_str = "cả ngày"
    bits = [title]
    if time_str:
        bits.append(time_str)
    if loc:
        bits.append(f"@{loc[:40]}")
    if n_att:
        bits.append(f"{n_att} người")
    return " | ".join(bits)


def select_event_index_by_ai(
    user_question: str,
    events: List[Dict[str, Any]],
    display_tz: str = GCALENDAR_TZ,
) -> Tuple[int, str]:
    """
    Chọn sự kiện khớp câu hỏi. Trả (idx, reason):
      - (i, "matched")    — khớp rõ 1 sự kiện
      - (-1, "ambiguous") — nhiều sự kiện có khả năng → cần user xác nhận
      - (-1, "no_match")  — không có sự kiện nào liên quan
      - (-1, "error")     — lỗi API
    """
    if not events:
        return -1, "no_match"
    if len(events) == 1:
        return 0, "matched"

    # Heuristic nhanh theo time-of-day (sáng/chiều/tối) trước khi gọi LLM.
    tod = extract_time_of_day(user_question)
    if tod is not None:
        h_lo, h_hi = tod
        candidates: List[int] = []
        for i, ev in enumerate(events):
            start = ev.get("start") or {}
            dt_s = str(start.get("dateTime") or "")
            if not dt_s:
                continue
            try:
                if dt_s.endswith("Z"):
                    dt_s = dt_s[:-1] + "+00:00"
                st = datetime.fromisoformat(dt_s).astimezone(ZoneInfo(display_tz))
                if h_lo <= st.hour < h_hi:
                    candidates.append(i)
            except Exception:
                pass
        if len(candidates) == 1:
            return candidates[0], "matched"
        # Nếu >1 candidate cùng buổi → để LLM xử lý trên subset này
        if len(candidates) > 1:
            events = [events[i] for i in candidates]
            # nhớ map index ngược về original
            orig_map = candidates
        else:
            orig_map = list(range(len(events)))
    else:
        orig_map = list(range(len(events)))

    try:
        client = get_openai_client()
        briefs = [f"{i + 1}. {_build_event_brief(ev, display_tz)}" for i, ev in enumerate(events)]
        system = (
            "Bạn giúp chọn đúng MỘT cuộc họp trong danh sách khớp với câu hỏi người dùng. "
            "Dựa vào tiêu đề, khung giờ, địa điểm, số người dự để suy luận.\n\n"
            "QUY TẮC TRẢ LỜI (bắt buộc đúng format):\n"
            "- Nếu chọn được rõ ràng 1 cuộc: trả đúng số thứ tự (vd: 2).\n"
            "- Nếu nhiều cuộc đều có khả năng khớp (ambiguous): trả 'AMBIGUOUS'.\n"
            "- Nếu không có cuộc nào liên quan: trả '0'.\n"
            "KHÔNG giải thích thêm. Chỉ trả đúng 1 token."
        )
        user = f"Câu hỏi:\n{user_question}\n\nDanh sách cuộc họp trong ngày:\n" + "\n".join(briefs)
        resp = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        if "AMBIG" in raw:
            return -1, "ambiguous"
        m = re.search(r"\b(\d+)\b", raw)
        if not m:
            return -1, "ambiguous"
        n = int(m.group(1))
        if n == 0:
            return -1, "no_match"
        if 1 <= n <= len(events):
            return orig_map[n - 1], "matched"
        return -1, "ambiguous"
    except Exception as e:
        logger.warning("select_event_index_by_ai: %s", e)
        return -1, "error"


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
