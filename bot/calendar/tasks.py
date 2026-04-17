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
    # Unicode NFD KHÔNG decompose 'đ' (U+0111) → cần thay thủ công.
    n = n.replace("đ", "d")
    return re.sub(r"\s+", " ", n).strip()


# Kính ngữ hay xuất hiện đầu tên trong biên bản VN ("anh A", "chị Bình", "sếp X").
_VN_HONORIFICS = ("anh", "chi", "em", "ong", "ba", "co", "chu", "bac", "sep", "a.", "c.")

# Ký hiệu tách nhiều người/phòng khi LLM trả string thay vì array. Chỉ split ở
# khoảng trống rõ ràng để tránh ăn vào dấu phẩy trong tên đơn.
_MULTI_ASSIGNEE_SPLIT_RE = re.compile(r"\s*(?:&|\+|\sv[àa]\s|/|;)\s*")


def _coerce_assignee_list(raw: Any) -> List[str]:
    """LLM có thể trả string hoặc list. Chuẩn hoá về list[str] non-empty."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    parts = [p.strip() for p in _MULTI_ASSIGNEE_SPLIT_RE.split(s) if p.strip()]
    return parts or [s]


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


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Bóc JSON object (có thể wrap trong ```json ... ```). Dùng cho summary+tasks."""
    s = (raw or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    lb = s.find("{")
    rb = s.rfind("}")
    if lb == -1 or rb == -1 or rb < lb:
        return None
    try:
        data = json.loads(s[lb : rb + 1])
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("_extract_json_object parse: %s", e)
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


def fetch_members_snapshot(sb: Any) -> List[Dict[str, Any]]:
    """Helper public: gọi 1 lần cho cả extract_tasks + resolve_assignees."""
    return _fetch_all_members(sb) if sb else []


def _format_members_hint(members: List[Dict[str, Any]], limit: int = 80) -> str:
    lines: List[str] = []
    for m in members[:limit]:
        nm = (str(m.get("Họ và tên") or "")).strip()
        if not nm:
            continue
        place = (str(m.get("Nơi làm việc") or "")).strip()
        chuc = (str(m.get("Chức vụ") or "")).strip()
        parts = [nm]
        if chuc:
            parts.append(chuc)
        if place:
            parts.append(place)
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _empty_summary() -> Dict[str, List[str]]:
    return {"thao_luan": [], "quyet_dinh": [], "luu_y": []}


def _coerce_bullet_list(raw: Any, max_items: int = 20) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        out = [str(x).strip() for x in raw if str(x).strip()]
        return out[:max_items]
    s = str(raw).strip()
    if not s:
        return []
    parts = [p.strip("-• ").strip() for p in s.split("\n") if p.strip()]
    return [p for p in parts if p][:max_items]


def summarize_and_extract_tasks(
    ev: Dict[str, Any],
    members_hint: Optional[List[Dict[str, Any]]] = None,
    display_tz: str = GCALENDAR_TZ,
) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
    """1 LLM call → (summary_dict, tasks_list). summary_dict có 3 mảng bullet:
    thao_luan, quyet_dinh, luu_y. tasks_list giữ schema cũ (task_name, assignees, ...)."""
    desc_html = (ev.get("description") or "").strip()
    desc_text = html_description_to_text(desc_html)
    if not desc_text:
        return _empty_summary(), []

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
        "Bạn là trợ lý đọc biên bản cuộc họp (tiếng Việt) và thực hiện 2 việc:\n"
        "  (A) TÓM TẮT nội dung cuộc họp.\n"
        "  (B) TRÍCH TẤT CẢ công việc / hành động được phân công.\n\n"
        "YÊU CẦU CỐT LÕI:\n"
        "- Đọc TOÀN BỘ biên bản từ DÒNG ĐẦU tới DÒNG CUỐI. KHÔNG chỉ đọc phần kết luận.\n"
        "- Với (A) tóm tắt: gọn, khách quan, bullet ngắn. Không copy nguyên câu dài; chia 3 nhóm:\n"
        "    thao_luan: các nội dung / chủ đề đã thảo luận.\n"
        "    quyet_dinh: các quyết định, kết luận đã chốt.\n"
        "    luu_y: các điểm cần lưu ý, rủi ro, yêu cầu đặc biệt (có thể rỗng).\n"
        "- Với (B) task: trích MỌI hành động được phân công, kể cả nằm trong phần thảo luận. "
        "Dấu hiệu: giao, phân công, chịu trách nhiệm, chủ trì, phụ trách, thực hiện, triển khai, "
        "chuẩn bị, soạn, báo cáo, gửi, hoàn thiện, hoàn thành, rà soát, tổng hợp, cập nhật, đề xuất, "
        "trình, ký, duyệt, theo dõi, kiểm tra, đảm bảo, nộp, trước ngày, deadline, hạn.\n"
        "- Một người nhận NHIỀU việc → TÁCH thành nhiều entry.\n"
        "- Một việc cho NHIỀU người/phòng → MỘT entry với mảng nguoi_thuc_hien nhiều tên.\n\n"
        "ĐỊNH DẠNG OUTPUT: DUY NHẤT một JSON object đúng schema:\n"
        "{\n"
        '  "tom_tat": {\n'
        '    "thao_luan": [string, ...],\n'
        '    "quyet_dinh": [string, ...],\n'
        '    "luu_y":    [string, ...]\n'
        "  },\n"
        '  "cong_viec": [\n'
        "    {\n"
        '      "ten_cong_viec": string,\n'
        '      "chi_tiet": string,\n'
        '      "nguoi_thuc_hien": [string, ...],\n'
        '      "deadline": "YYYY-MM-DD" hoặc "",\n'
        '      "deadline_raw": string\n'
        "    }, ...\n"
        "  ]\n"
        "}\n\n"
        "QUY TẮC nguoi_thuc_hien:\n"
        "  1. 1 người/phòng → mảng 1 phần tử.\n"
        "  2. Nhiều người/phòng (\"Ban A & Ban B\", \"anh X và chị Y\") → tách từng phần tử.\n"
        "  3. Tên cụ thể (\"anh Huy\", \"chị Nga\") → giữ nguyên.\n"
        "  4. Đại từ chung (\"anh này\", \"chị kia\", \"ông đó\") → SUY LUẬN từ ngữ cảnh + "
        "người dự + thành viên hệ thống để chọn tên khớp nhất. KHÔNG trả đại từ.\n"
        "  5. Không rõ ai → [].\n\n"
        "KHÔNG bọc markdown, KHÔNG giải thích, KHÔNG ghi text ngoài JSON. "
        "Nếu thực sự không có việc nào → cong_viec = []. Nếu biên bản không có nội dung → "
        "tom_tat có các mảng rỗng."
    )
    members_block = _format_members_hint(members_hint or [])

    ctx_lines: List[str] = [
        f"Ngày họp: {meeting_date_iso}",
        f"Tiêu đề cuộc họp: {summary or '(không có)'}",
        "",
        f"Người dự:\n{attendees_block}",
    ]
    if members_block:
        ctx_lines += [
            "",
            "Thành viên hệ thống (Họ và tên | Chức vụ | Nơi làm việc) — "
            "dùng để resolve đại từ \"anh này/chị kia\" nếu biên bản ghi chung chung:",
            members_block,
        ]
    context_block = "\n".join(ctx_lines)

    user_msg = (
        "NGỮ CẢNH:\n"
        f"{context_block}\n\n"
        "=== BIÊN BẢN CUỘC HỌP (đọc KỸ từ đầu đến cuối, không được bỏ sót đoạn nào) ===\n"
        f"{desc_text}\n"
        "=== HẾT BIÊN BẢN ===\n\n"
        "NHIỆM VỤ: Tóm tắt + trích TẤT CẢ công việc theo schema đã nêu. "
        "Scan lại từ dòng đầu tới dòng cuối đảm bảo không bỏ sót."
    )

    logger.info(
        "summarize_and_extract_tasks: desc=%d chars, attendees=%d, members_hint=%d",
        len(desc_text), len(ev.get("attendees") or []), len(members_hint or []),
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
            max_tokens=4096,
        )
        raw = (resp.choices[0].message.content or "").strip()
        finish = getattr(resp.choices[0], "finish_reason", None)
        if finish and finish != "stop":
            logger.warning(
                "summarize_and_extract_tasks: LLM finish_reason=%s (có thể truncate)",
                finish,
            )
    except Exception as e:
        logger.exception("summarize_and_extract_tasks LLM: %s", e)
        return _empty_summary(), []

    obj = _extract_json_object(raw)
    if not obj:
        logger.warning(
            "summarize_and_extract_tasks: LLM output không phải JSON object: %r", raw[:200]
        )
        return _empty_summary(), []

    tom_tat_raw = obj.get("tom_tat") or {}
    if not isinstance(tom_tat_raw, dict):
        tom_tat_raw = {}
    summary_dict: Dict[str, List[str]] = {
        "thao_luan": _coerce_bullet_list(tom_tat_raw.get("thao_luan")),
        "quyet_dinh": _coerce_bullet_list(tom_tat_raw.get("quyet_dinh")),
        "luu_y": _coerce_bullet_list(tom_tat_raw.get("luu_y")),
    }

    arr = obj.get("cong_viec") or []
    if not isinstance(arr, list):
        arr = []
    logger.info(
        "summarize_and_extract_tasks: LLM trả %d task + tóm tắt %d/%d/%d",
        len(arr),
        len(summary_dict["thao_luan"]),
        len(summary_dict["quyet_dinh"]),
        len(summary_dict["luu_y"]),
    )

    out: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = (str(item.get("ten_cong_viec") or "")).strip()
        if not name:
            continue
        detail = (str(item.get("chi_tiet") or "")).strip()
        assignees_raw = _coerce_assignee_list(item.get("nguoi_thuc_hien"))
        dl_iso = (str(item.get("deadline") or "")).strip()
        dl_raw = (str(item.get("deadline_raw") or "")).strip()
        out.append(
            {
                "task_name": name,
                "task_detail": detail,
                "assignees": [{"name": a, "email": None, "chat_id": None} for a in assignees_raw],
                "deadline": _parse_iso_date(dl_iso),
                "deadline_raw": dl_raw,
            }
        )
    return summary_dict, out


def extract_tasks_from_event(
    ev: Dict[str, Any],
    display_tz: str = GCALENDAR_TZ,
    members_hint: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Wrapper cũ: chỉ trả danh sách task, bỏ phần tóm tắt."""
    _summary, tasks = summarize_and_extract_tasks(ev, members_hint, display_tz)
    return tasks


def format_summary_section(summary_dict: Dict[str, List[str]]) -> str:
    """Render dict summary thành text bullet 3 nhóm. Trả "" nếu rỗng toàn bộ."""
    tl = summary_dict.get("thao_luan") or []
    qd = summary_dict.get("quyet_dinh") or []
    ly = summary_dict.get("luu_y") or []
    if not (tl or qd or ly):
        return ""
    lines: List[str] = ["Tóm tắt biên bản cuộc họp:"]
    if tl:
        lines.append("• Đã thảo luận:")
        lines.extend(f"   - {x}" for x in tl)
    if qd:
        lines.append("• Quyết định / kết luận:")
        lines.extend(f"   - {x}" for x in qd)
    if ly:
        lines.append("• Lưu ý:")
        lines.extend(f"   - {x}" for x in ly)
    return "\n".join(lines)


def has_saved_tasks_for_event(sb: Any, event_id: str) -> bool:
    """Check xem event_id đã có row nào trong meeting_tasks chưa (dedup trước khi save)."""
    if not sb or not event_id:
        return False
    try:
        r = (
            sb.table(SUPABASE_MEETING_TASKS_TABLE)
            .select("id")
            .eq("meeting_event_id", event_id)
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception as e:
        logger.warning("has_saved_tasks_for_event: %s", e)
        return False


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


def _match_by_full_name(n: str, members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match LLM name (đã normalize + strip honorific) vào members.'Họ và tên'."""
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


# Ưu tiên gán cho người đứng đầu phòng/ban (nếu biên bản viết "Ban X làm Y"
# mà ban có nhiều member). Điểm càng cao càng ưu tiên.
_LEADER_RANK = (
    ("truong ban", 50), ("ban truong", 50),
    ("truong phong", 45), ("giam doc", 45), ("director", 45),
    ("chu tich", 44), ("chairman", 44),
    ("head of", 40), ("head ", 40), ("lead ", 38), ("manager", 38),
    ("truong", 35), ("chu nhiem", 32),
    ("pho truong ban", 25), ("pho truong phong", 25),
    ("pho truong", 25), ("pho phong", 25), ("pho giam doc", 25), ("deputy", 25),
    ("pho ", 20), ("quan ly", 18),
)


_LEADER_RANK_BY_LEN = sorted(_LEADER_RANK, key=lambda x: -len(x[0]))


def _leader_score(chuc_vu_raw: str) -> int:
    c = _normalize_vn(chuc_vu_raw)
    if not c:
        return 0
    # Dùng longest-match để "pho giam doc" không ăn rank của "giam doc".
    for kw, score in _LEADER_RANK_BY_LEN:
        if kw in c:
            return score
    return 0


def _match_by_workplace(n: str, members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match vào members.'Nơi làm việc'. Nếu nhiều người thuộc nơi đó, ưu tiên
    người có Chức vụ cấp cao nhất (trưởng/phó/giám đốc). Nếu cùng rank thì lấy
    người đầu tiên (ổn định theo thứ tự DB trả về)."""
    hits: List[Dict[str, Any]] = []
    for m in members:
        place = _normalize_vn(str(m.get("Nơi làm việc") or ""))
        if not place:
            continue
        if n == place or n in place or place in n:
            hits.append(m)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    scored = sorted(
        hits,
        key=lambda m: _leader_score(str(m.get("Chức vụ") or "")),
        reverse=True,
    )
    return scored[0]


def _match_member(name_raw: str, members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match 2 tầng: (1) Họ và tên → (2) fallback Nơi làm việc (nếu unique)."""
    n = _strip_honorific(_normalize_vn(name_raw))
    if not n:
        return None
    m = _match_by_full_name(n, members)
    if m:
        return m
    return _match_by_workplace(n, members)


def resolve_assignees(
    sb: Any,
    tasks: List[Dict[str, Any]],
    members: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Với mỗi task, match từng assignee (tên người HOẶC phòng/ban) → gắn email +
    chat_id. Nếu 1 task có N assignees, kết quả vẫn giữ nguyên cấu trúc list N
    phần tử. Truyền `members` nếu đã fetch sẵn để né query trùng."""
    if not tasks or not sb:
        return tasks
    if members is None:
        members = _fetch_all_members(sb)

    all_emails: List[str] = []
    for t in tasks:
        for a in t.get("assignees") or []:
            mem = _match_member(a.get("name") or "", members)
            a["matched_member"] = mem  # tạm giữ, strip cuối
            if mem:
                em = (mem.get("email_congty") or "").strip()
                a["email"] = em or None
                if em:
                    all_emails.append(em)
    tid_by_email = _fetch_telegram_by_email(sb, all_emails)
    for t in tasks:
        for a in t.get("assignees") or []:
            em = (a.get("email") or "").strip().lower()
            if em:
                a["chat_id"] = tid_by_email.get(em)
            a.pop("matched_member", None)
    return tasks


def save_meeting_tasks(
    sb: Any,
    ev: Dict[str, Any],
    tasks: List[Dict[str, Any]],
    created_by_chat_id: Optional[int],
    display_tz: str = GCALENDAR_TZ,
) -> int:
    """Insert vào bảng meeting_tasks. Với task có N assignees, insert N rows
    chia sẻ task_name/detail/deadline (để reminder query `WHERE assignee_chat_id = X`
    vẫn đơn giản). Task không có assignee nào → 1 row với assignee_* = NULL.
    Trả tổng số rows đã insert."""
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
        dl_iso = dl.isoformat() if isinstance(dl, date) else None
        base = {
            "meeting_event_id": meeting_event_id,
            "meeting_summary": meeting_summary,
            "meeting_start": meeting_start_iso,
            "task_name": t.get("task_name") or "",
            "task_detail": t.get("task_detail") or None,
            "deadline": dl_iso,
            "deadline_raw": t.get("deadline_raw") or None,
            "created_by_chat_id": created_by_chat_id,
        }
        assignees = t.get("assignees") or []
        if not assignees:
            rows.append({
                **base,
                "assignee_name": None,
                "assignee_email": None,
                "assignee_chat_id": None,
            })
            continue
        for a in assignees:
            rows.append({
                **base,
                "assignee_name": (a.get("name") or "").strip() or None,
                "assignee_email": a.get("email"),
                "assignee_chat_id": a.get("chat_id"),
            })
    try:
        sb.table(SUPABASE_MEETING_TASKS_TABLE).insert(rows).execute()
        return len(rows)
    except Exception as e:
        logger.exception("save_meeting_tasks insert: %s", e)
        return 0


def _format_assignees_line(assignees: List[Dict[str, Any]]) -> str:
    if not assignees:
        return "   Người thực hiện: (chưa rõ)"
    parts: List[str] = []
    for a in assignees:
        nm = (a.get("name") or "").strip() or "(không tên)"
        em = (a.get("email") or "").strip()
        if em:
            parts.append(f"{nm} ({em})")
        else:
            parts.append(f"{nm} (chưa khớp)")
    return "   Người thực hiện: " + ", ".join(parts)


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
        lines.append(_format_assignees_line(t.get("assignees") or []))
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
