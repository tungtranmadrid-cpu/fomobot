"""Định dạng lịch & chi tiết cuộc họp thành text Telegram."""
import html
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from ..config import MEETING_HIDE_EMAILS_RAW, SUPABASE_MEMBERS_TABLE

logger = logging.getLogger(__name__)

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\[\]\"']+")

# Google Calendar lưu description dạng HTML nhẹ (<br>, <div>, <a>, <ul><li>, <b>, <i>...).
# Convert về text thuần để hiển thị trên Telegram.
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_OPEN_RE = re.compile(r"<\s*(div|p|ul|ol|tr)\b[^>]*>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"<\s*/\s*(div|p|ul|ol|tr)\s*>", re.IGNORECASE)
_LI_OPEN_RE = re.compile(r"<\s*li\b[^>]*>", re.IGNORECASE)
_LI_CLOSE_RE = re.compile(r"<\s*/\s*li\s*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_BLANKLINE_RE = re.compile(r"\n{3,}")


def html_description_to_text(desc: str) -> str:
    """Chuyển mô tả Google Calendar (HTML nhẹ) thành text đọc được trên Telegram."""
    s = desc or ""
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_OPEN_RE.sub("\n", s)
    s = _BLOCK_CLOSE_RE.sub("\n", s)
    s = _LI_OPEN_RE.sub("\n  • ", s)
    s = _LI_CLOSE_RE.sub("", s)
    s = _ANY_TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _MULTI_BLANKLINE_RE.sub("\n\n", s)
    lines = [ln.rstrip() for ln in s.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _meeting_hidden_emails_set() -> set:
    raw = (MEETING_HIDE_EMAILS_RAW or "").strip()
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def is_hidden_meeting_report_email(em: str) -> bool:
    """Email không được đưa vào bất kỳ dòng nào của báo cáo chi tiết cuộc họp."""
    return (em or "").strip().lower() in _meeting_hidden_emails_set()


def fetch_members_by_emails(sb: Any, emails: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Tra bảng members theo email_congty (khớp email lịch).
    Trả về dict[email.lower()] = row (có Họ và tên, Chức vụ, Nơi làm việc, ...).
    """
    seen: set = set()
    uniq: List[str] = []
    for e in emails:
        e = (e or "").strip()
        if not e or is_hidden_meeting_report_email(e):
            continue
        el = e.lower()
        if el in seen:
            continue
        seen.add(el)
        uniq.append(e)
    out: Dict[str, Dict[str, Any]] = {}
    if not uniq or not sb:
        return out
    try:
        for i in range(0, len(uniq), 80):
            batch = uniq[i : i + 80]
            r = (
                sb.table(SUPABASE_MEMBERS_TABLE)
                .select("*")
                .in_("email_congty", batch)
                .execute()
            )
            for row in r.data or []:
                ec = (row.get("email_congty") or "").strip()
                if ec:
                    out[ec.lower()] = row
    except Exception as e:
        logger.warning("fetch_members_by_emails: %s", e)
    return out


def _is_online_meeting_url(url: str) -> bool:
    u = (url or "").lower()
    needles = (
        "meet.google.com",
        "zoom.us",
        "zoom.com",
        "teams.microsoft.com",
        "teams.live.com",
        "webex.com",
        "hangouts.google.com",
        "gotomeeting.com",
        "bluejeans.com",
        "whereby.com",
        "jitsi.org",
        "meet.jit.si",
        "duo.google.com",
        "slack.com/call",
        "meetings.hubspot",
        "bigbluebutton.org",
    )
    return any(n in u for n in needles)


def parse_google_start_end(
    ev: Dict[str, Any],
    display_tz: str,
) -> Tuple[bool, datetime, datetime, str]:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    loc = (ev.get("location") or "").strip()

    if "date" in start and "dateTime" not in start:
        try:
            d0 = datetime.strptime(str(start.get("date")), "%Y-%m-%d").date()
        except ValueError:
            d0 = datetime.now(ZoneInfo(display_tz)).date()
        tz = ZoneInfo(display_tz)
        st = datetime.combine(d0, datetime.min.time(), tzinfo=tz)
        en_d = str(end.get("date") or start.get("date"))
        try:
            d1 = datetime.strptime(en_d, "%Y-%m-%d").date()
        except ValueError:
            d1 = d0
        en = datetime.combine(d1, datetime.min.time(), tzinfo=tz)
        return True, st, en, loc

    st_s = str(start.get("dateTime") or "")
    en_s = str(end.get("dateTime") or "")
    if st_s.endswith("Z"):
        st_s = st_s[:-1] + "+00:00"
    if en_s.endswith("Z"):
        en_s = en_s[:-1] + "+00:00"
    try:
        st = datetime.fromisoformat(st_s)
    except ValueError:
        st = datetime.now(ZoneInfo(display_tz))
    try:
        en = datetime.fromisoformat(en_s)
    except ValueError:
        en = st
    if st.tzinfo is None:
        st = st.replace(tzinfo=ZoneInfo(display_tz))
    if en.tzinfo is None:
        en = en.replace(tzinfo=ZoneInfo(display_tz))
    st = st.astimezone(ZoneInfo(display_tz))
    en = en.astimezone(ZoneInfo(display_tz))
    return False, st, en, loc


def format_day_schedule(
    events: List[Dict[str, Any]],
    day: date,
    display_tz: str = "Asia/Ho_Chi_Minh",
) -> str:
    if not events:
        return f"Lịch ngày {day.strftime('%d/%m/%Y')}: không có sự kiện trên Google Calendar."

    lines: List[str] = [f"Lịch ngày {day.strftime('%d/%m/%Y')} (giờ {display_tz}):"]
    for ev in events:
        subj = (ev.get("summary") or "(Không tiêu đề)").strip()
        is_all, st, en, loc = parse_google_start_end(ev, display_tz)
        if is_all:
            piece = f"• Cả ngày: {subj}"
        else:
            piece = f"• {st.strftime('%H:%M')}–{en.strftime('%H:%M')}: {subj}"
        if loc:
            piece += f" — {loc}"
        lines.append(piece)

    return "\n".join(lines)


def format_meeting_details_text(
    ev: Dict[str, Any],
    display_tz: str,
    members_by_email: Optional[Dict[str, Dict[str, Any]]] = None,
    show_description: bool = True,
) -> str:
    """Trình bày chi tiết cuộc họp: giờ, thành phần (+ members), họp trực tuyến vs tài liệu.
    show_description=False: bỏ block "Mô tả / biên bản" (dùng khi sẽ thay bằng tóm tắt LLM)."""
    summary = (ev.get("summary") or "(Không tiêu đề)").strip()
    lines: List[str] = [f"Cuộc họp / sự kiện: {summary}"]

    try:
        start = ev.get("start") or {}
        if "date" in start and "dateTime" not in start:
            d0 = str(start.get("date") or "")
            lines.append(f"Thời gian: cả ngày {d0}")
        else:
            _is_all, st, en, loc = parse_google_start_end(ev, display_tz)
            if loc:
                lines.append(
                    f"Thời gian: {st.strftime('%d/%m/%Y %H:%M')} – {en.strftime('%H:%M')} ({display_tz}) — {loc}"
                )
            else:
                lines.append(
                    f"Thời gian: {st.strftime('%d/%m/%Y %H:%M')} – {en.strftime('%H:%M')} ({display_tz})"
                )
    except Exception:
        lines.append("Thời gian: (không đọc được)")

    attendees = ev.get("attendees") or []
    lines.append("")
    shown_any = False
    if attendees:
        lines.append("Thành phần tham gia:")
        for a in attendees:
            em = (a.get("email") or "").strip()
            if is_hidden_meeting_report_email(em):
                continue
            shown_any = True
            cal_name = (a.get("displayName") or "").strip()
            mem = None
            if members_by_email and em:
                mem = members_by_email.get(em.lower())
            ho_ten = (mem.get("Họ và tên") or "").strip() if mem else ""
            chuc = (mem.get("Chức vụ") or "").strip() if mem else ""
            noi = (mem.get("Nơi làm việc") or "").strip() if mem else ""
            ten_hien = ho_ten or cal_name or "?"
            if ho_ten and chuc and noi:
                line = f"  • {ho_ten} — {chuc} — {noi}"
            else:
                line = f"  • {ten_hien}"
            lines.append(line)
        if not shown_any:
            lines.append(
                "  (Không hiển thị danh sách theo cấu hình ẩn email hoặc chỉ có email đã ẩn.)"
            )
    else:
        lines.append(
            "Thành phần tham gia: không có danh sách trên Google Calendar "
            "(sự kiện cá nhân hoặc chưa mời qua Calendar)."
        )

    online_lines: List[str] = []
    doc_lines: List[str] = []
    seen_urls: set = set()

    hangout = (ev.get("hangoutLink") or "").strip()
    if hangout:
        if hangout not in seen_urls:
            seen_urls.add(hangout)
            online_lines.append(f"  • Google Meet / Hangouts\n    {hangout}")

    cd = ev.get("conferenceData") or {}
    for ep in cd.get("entryPoints") or []:
        uri = (ep.get("uri") or "").strip()
        ep_type = (ep.get("entryPointType") or "").strip()
        if not uri or uri in seen_urls:
            continue
        seen_urls.add(uri)
        is_phone = uri.lower().startswith("tel:") or uri.lower().startswith("sip:") or ep_type == "phone"
        if ep_type == "video" or _is_online_meeting_url(uri) or is_phone:
            if ep_type == "video":
                label = "Họp trực tuyến (video)"
            elif is_phone:
                label = "Gọi vào phòng (SIP/điện thoại)"
            else:
                label = "Họp trực tuyến"
            online_lines.append(f"  • {label}\n    {uri}")
        else:
            doc_lines.append(f"  • Liên kết khác ({ep_type or 'mô tả'})\n    {uri}")

    for att in ev.get("attachments") or []:
        url = (att.get("fileUrl") or "").strip()
        title = (att.get("title") or "Tài liệu đính kèm trên lịch").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        doc_lines.append(f"  • {title} (đính kèm Calendar)\n    {url}")

    desc = (ev.get("description") or "").strip()
    for m in _URL_IN_TEXT_RE.finditer(desc):
        u = m.group(0).rstrip(").,;]")
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        if _is_online_meeting_url(u):
            online_lines.append(f"  • Link họp (trong mô tả)\n    {u}")
        else:
            doc_lines.append(f"  • Link / tài liệu (trong mô tả)\n    {u}")

    lines.append("")
    if online_lines:
        lines.append("Họp trực tuyến (link tham gia):")
        lines.extend(online_lines)
    else:
        lines.append("Họp trực tuyến: không thấy link Meet/Zoom/Teams hoặc tương đương trên sự kiện.")

    lines.append("")
    if doc_lines:
        lines.append("Tài liệu cuộc họp (file đính kèm lịch & link tài liệu trong mô tả):")
        lines.extend(doc_lines)
    else:
        lines.append(
            "Tài liệu cuộc họp: không thấy file đính kèm hay link tài liệu (ngoài link họp trực tuyến). "
            "Có thể bổ sung trên Google Calendar."
        )

    if show_description:
        desc_text = html_description_to_text(desc)
        if desc_text:
            lines.append("")
            lines.append("Mô tả / biên bản cuộc họp (từ Google Calendar):")
            lines.append(desc_text)

    return "\n".join(lines)
