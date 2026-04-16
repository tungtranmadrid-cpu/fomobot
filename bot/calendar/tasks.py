"""Tóm tắt biên bản cuộc họp → trích danh sách công việc (task_name,
chi tiết, người thực hiện, deadline) bằng LLM + lưu Supabase + format."""
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from ..clients import get_openai_client
from ..config import AI_MODEL, GCALENDAR_TZ, SUPABASE_MEMBERS_TABLE, SUPABASE_USER_TABLE
from .format import html_description_to_text, parse_google_start_end

logger = logging.getLogger(__name__)

SUPABASE_MEETING_TASKS_TABLE = "meeting_tasks"


def _normalize_vn(s: str) -> str:
    val = (s or "").strip().lower()
    n = unicodedata.normalize("NFD", val)
    n = "".join(ch for ch in n if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", n).strip()


# Kính ngữ hay xuất hiện đầu tên trong biên bản VN ("anh A", "chị Bình", "sếp X").
_VN_HONORIFICS = ("anh", "chi", "em", "ong", "ba", "co", "chu", "bac", "sep", "a.", "c.")


def _strip_honorific(name_norm: str) -> str:
    parts = name_norm.split(" ")
    if len(parts) >= 2 and parts[0] in _VN_HONORIFICS:
        return " ".join(parts[1:]).strip()
    return name_norm


def _extract_json_array(raw: str) -> Optional[List[Any]]:
    """LLM hay wrap trong ```json ... ``` hoặc trả text thừa. Bóc ra array."""
    s = (raw or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    lb = s.find("[")
    rb = s.rfind("]")
    if lb == -1 or rb == -1 or rb < lb:
        return None
    try:
        data = json.loads(s[lb : rb + 1])
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning("_extract_json_array parse: %s", e)
    return None


def _parse_iso_date(raw: Any) -> Optional[date]:
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def extract_tasks_from_event(ev: Dict[str, Any], display_tz: str = GCALENDAR_TZ) -> List[Dict[str, Any]]:
    """Gọi LLM để trích task từ description. Trả list dict chuẩn hóa (tên/chi tiết/người/deadline)."""
    desc_html = (ev.get("description") or "").strip()
    desc_text = html_description_to_text(desc_html)
    if not desc_text:
        return []

    try:
        _is_all, st, _en, _loc = parse_google_start_end(ev, display_tz)
        meeting_date_iso = st.date().isoformat()
    except Exception:
        meeting_date_iso = datetime.now(ZoneInfo(display_tz)).date().isoformat()

    summary = (ev.get("summary") or "").strip()
    attendees_lines: List[str] = []
    for a in ev.get("attendees") or []:
        em = (a.get("email") or "").strip()
        nm = (a.get("displayName") or "").strip()
        if em or nm:
            attendees_lines.append(f"- {nm or '(không tên)'} <{em}>" if em else f"- {nm}")
    attendees_block = "\n".join(attendees_lines) if attendees_lines else "(không có)"

    system = (
        "Bạn là trợ lý trích xuất công việc từ biên bản cuộc họp (tiếng Việt). "
        "Đọc biên bản và lọc ra danh sách HÀNH ĐỘNG CỤ THỂ mà ai đó phải làm sau họp. "
        "Bỏ qua phần mô tả chung, thông tin nền, link tài liệu đơn thuần.\n\n"
        "Trả về DUY NHẤT một JSON array, mỗi phần tử có đúng 4 trường:\n"
        '  "ten_cong_viec": tiêu đề ngắn gọn của việc (string, bắt buộc)\n'
        '  "chi_tiet": mô tả đầy đủ (string, có thể rỗng "")\n'
        '  "nguoi_thuc_hien": tên người được giao trong biên bản, giữ nguyên cách viết; '
        'nếu không rõ trả "" (string)\n'
        '  "deadline": ngày hạn dạng "YYYY-MM-DD" nếu suy ra được từ biên bản; '
        'nếu không rõ trả "" (string). Mốc tính ngày tương đối: ngày họp.\n'
        '  "deadline_raw": đoạn text gốc mô tả deadline, ví dụ "cuối tuần sau" (string, có thể rỗng)\n\n'
        "KHÔNG bọc markdown, KHÔNG giải thích. Nếu không có công việc nào rõ ràng, trả []."
    )
    user_msg = (
        f"Ngày họp: {meeting_date_iso}\n"
        f"Tiêu đề cuộc họp: {summary or '(không có)'}\n"
        f"Người dự:\n{attendees_block}\n\n"
        f"Biên bản cuộc họp:\n{desc_text}"
    )

    try:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.exception("extract_tasks_from_event LLM: %s", e)
        return []

    arr = _extract_json_array(raw)
    if not arr:
        logger.warning("extract_tasks_from_event: LLM output không phải JSON array: %r", raw[:200])
        return []

    out: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = (str(item.get("ten_cong_viec") or "")).strip()
        if not name:
            continue
        detail = (str(item.get("chi_tiet") or "")).strip()
        assignee = (str(item.get("nguoi_thuc_hien") or "")).strip()
        dl_iso = (str(item.get("deadline") or "")).strip()
        dl_raw = (str(item.get("deadline_raw") or "")).strip()
        out.append(
            {
                "task_name": name,
                "task_detail": detail,
                "assignee_name": assignee,
                "deadline": _parse_iso_date(dl_iso),
                "deadline_raw": dl_raw,
            }
        )
    return out


def _fetch_all_members(sb: Any) -> List[Dict[str, Any]]:
    try:
        r = (
            sb.table(SUPABASE_MEMBERS_TABLE)
            .select("email_congty,Họ và tên,Chức vụ,Nơi làm việc")
            .execute()
        )
        return list(r.data or [])
    except Exception as e:
        logger.warning("_fetch_all_members: %s", e)
        return []


def _fetch_telegram_by_email(sb: Any, emails: List[str]) -> Dict[str, int]:
    """email_congty.lower() → telegram chat_id (int). Dùng user.email_congty."""
    uniq = sorted({(e or "").strip().lower() for e in emails if (e or "").strip()})
    if not uniq:
        return {}
    out: Dict[str, int] = {}
    try:
        for i in range(0, len(uniq), 80):
            batch = uniq[i : i + 80]
            r = (
                sb.table(SUPABASE_USER_TABLE)
                .select("email_congty,telegram_ID")
                .in_("email_congty", batch)
                .execute()
            )
            for row in r.data or []:
                em = (row.get("email_congty") or "").strip().lower()
                tid_raw = str(row.get("telegram_ID") or "").strip()
                if not em or not tid_raw:
                    continue
                try:
                    out[em] = int(tid_raw)
                except ValueError:
                    continue
    except Exception as e:
        logger.warning("_fetch_telegram_by_email: %s", e)
    return out


def _match_member(name_raw: str, members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Tìm member khớp tên (bỏ dấu, lower, strip kính ngữ, substring 2 chiều)."""
    n = _strip_honorific(_normalize_vn(name_raw))
    if not n or len(n) < 1:
        return None
    # Ưu tiên match toàn bộ từ "Họ và tên" → substring
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for m in members:
        full = _normalize_vn(str(m.get("Họ và tên") or ""))
        if not full:
            continue
        if n == full:
            return m
        score = 0
        if full.endswith(" " + n) or full.startswith(n + " ") or f" {n} " in f" {full} ":
            score = max(score, len(n))
        elif n in full:
            score = max(score, len(n) - 1)
        elif full in n:
            score = max(score, len(full) - 1)
        if score > 0 and (best is None or score > best[0]):
            best = (score, m)
    return best[1] if best else None


def resolve_assignees(sb: Any, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gắn email + chat_id cho từng task nếu khớp được member trong hệ thống."""
    if not tasks or not sb:
        return tasks
    members = _fetch_all_members(sb)
    # Cache: normalized name → member row
    matched: Dict[int, Optional[Dict[str, Any]]] = {}
    emails: List[str] = []
    for i, t in enumerate(tasks):
        nm = t.get("assignee_name") or ""
        mem = _match_member(nm, members) if nm else None
        matched[i] = mem
        if mem:
            em = (mem.get("email_congty") or "").strip()
            if em:
                emails.append(em)
    tid_by_email = _fetch_telegram_by_email(sb, emails)
    for i, t in enumerate(tasks):
        mem = matched.get(i)
        if mem:
            em = (mem.get("email_congty") or "").strip()
            t["assignee_email"] = em or None
            if em:
                t["assignee_chat_id"] = tid_by_email.get(em.lower())
        else:
            t.setdefault("assignee_email", None)
            t.setdefault("assignee_chat_id", None)
    return tasks


def save_meeting_tasks(
    sb: Any,
    ev: Dict[str, Any],
    tasks: List[Dict[str, Any]],
    created_by_chat_id: Optional[int],
    display_tz: str = GCALENDAR_TZ,
) -> int:
    """Insert tasks vào bảng meeting_tasks. Trả số dòng đã insert."""
    if not tasks or not sb:
        return 0
    try:
        _is_all, st, _en, _loc = parse_google_start_end(ev, display_tz)
        meeting_start_iso = st.isoformat()
    except Exception:
        meeting_start_iso = None
    meeting_event_id = (ev.get("id") or "").strip() or "(unknown)"
    meeting_summary = (ev.get("summary") or "").strip() or None

    rows: List[Dict[str, Any]] = []
    for t in tasks:
        dl = t.get("deadline")
        rows.append(
            {
                "meeting_event_id": meeting_event_id,
                "meeting_summary": meeting_summary,
                "meeting_start": meeting_start_iso,
                "task_name": t.get("task_name") or "",
                "task_detail": t.get("task_detail") or None,
                "assignee_name": t.get("assignee_name") or None,
                "assignee_email": t.get("assignee_email"),
                "assignee_chat_id": t.get("assignee_chat_id"),
                "deadline": dl.isoformat() if isinstance(dl, date) else None,
                "deadline_raw": t.get("deadline_raw") or None,
                "created_by_chat_id": created_by_chat_id,
            }
        )
    try:
        sb.table(SUPABASE_MEETING_TASKS_TABLE).insert(rows).execute()
        return len(rows)
    except Exception as e:
        logger.exception("save_meeting_tasks insert: %s", e)
        return 0


def format_tasks_table(ev: Dict[str, Any], tasks: List[Dict[str, Any]], display_tz: str = GCALENDAR_TZ) -> str:
    summary = (ev.get("summary") or "(Không tiêu đề)").strip()
    try:
        _is_all, st, _en, _loc = parse_google_start_end(ev, display_tz)
        day_str = st.strftime("%d/%m/%Y")
    except Exception:
        day_str = ""
    head = f'Danh sách công việc từ cuộc họp "{summary}"'
    if day_str:
        head += f" ({day_str})"
    head += ":"
    if not tasks:
        return head + "\n(Không trích được công việc rõ ràng từ biên bản.)"

    lines: List[str] = [head, ""]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. Tên CV: {t.get('task_name') or '(không có)'}")
        detail = (t.get("task_detail") or "").strip()
        if detail:
            lines.append(f"   Chi tiết: {detail}")
        assignee = (t.get("assignee_name") or "").strip()
        email = (t.get("assignee_email") or "").strip()
        if assignee and email:
            lines.append(f"   Người thực hiện: {assignee} ({email})")
        elif assignee:
            lines.append(f"   Người thực hiện: {assignee} (chưa khớp member trong hệ thống)")
        else:
            lines.append("   Người thực hiện: (chưa rõ)")
        dl = t.get("deadline")
        dl_raw = (t.get("deadline_raw") or "").strip()
        if isinstance(dl, date):
            dl_str = dl.strftime("%d/%m/%Y")
            if dl_raw and _normalize_vn(dl_raw) != _normalize_vn(dl_str):
                lines.append(f"   Deadline: {dl_str} ({dl_raw})")
            else:
                lines.append(f"   Deadline: {dl_str}")
        elif dl_raw:
            lines.append(f"   Deadline: {dl_raw} (chưa parse được ngày cụ thể)")
        else:
            lines.append("   Deadline: (chưa rõ)")
        lines.append("")
    return "\n".join(lines).rstrip()
