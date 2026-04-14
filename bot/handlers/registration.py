"""Đăng ký /dk: hội thoại + inline-button duyệt bởi admin."""
import logging
import secrets
from typing import Any, Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ConversationHandler, ContextTypes

from ..clients import get_supabase_client
from ..config import (
    DEFAULT_REGISTRATION_GCAL_REFRESH,
    DEFAULT_REGISTRATION_USEREMAIL,
    SUPABASE_USER_TABLE,
)

logger = logging.getLogger(__name__)

REG_USERNAME, REG_EMAIL = range(2)


def _email_looks_valid(s: str) -> bool:
    s = (s or "").strip()
    if not s or "@" not in s or s.count("@") != 1:
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return True


def _pending_registrations(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    app = context.application
    if app.bot_data is None:
        app.bot_data = {}
    reg = app.bot_data.setdefault("pending_registrations", {})
    return reg


def user_row_exists_for_telegram(sb: Any, chat_id: int) -> bool:
    try:
        r = (
            sb.table(SUPABASE_USER_TABLE)
            .select("id")
            .eq("telegram_ID", str(chat_id))
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception as e:
        logger.warning("user_row_exists_for_telegram: %s", e)
        return False


def get_admin_telegram_chat_ids(sb: Any) -> List[int]:
    """Danh sách chat_id (telegram_ID) của user có Role admin (không phân biệt hoa thường)."""
    try:
        r = sb.table(SUPABASE_USER_TABLE).select("telegram_ID,Role").execute()
        rows = r.data or []
        out: List[int] = []
        seen = set()
        for row in rows:
            role = (row.get("Role") or "").strip().lower()
            if role != "admin":
                continue
            cid = _parse_telegram_chat_id(row.get("telegram_ID"))
            if cid is not None and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out
    except Exception as e:
        logger.warning("get_admin_telegram_chat_ids: %s", e)
        return []


def _parse_telegram_chat_id(raw: Any):
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def is_telegram_admin(sb: Any, user_telegram_id: int) -> bool:
    return user_telegram_id in get_admin_telegram_chat_ids(sb)


async def cmd_dk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bắt đầu đăng ký: chỉ chat riêng, cần Supabase."""
    if not update.message:
        return ConversationHandler.END
    if update.effective_chat.type != "private":
        await update.message.reply_text("Chỉ dùng lệnh /dk trong chat riêng với bot.")
        return ConversationHandler.END
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase (SUPABASE_URL, SUPABASE_KEY).")
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    if user_row_exists_for_telegram(sb, chat_id):
        await update.message.reply_text(
            "Tài khoản Telegram của bạn đã có trong bảng user. Không cần đăng ký lại."
        )
        return ConversationHandler.END
    admins = get_admin_telegram_chat_ids(sb)
    if not admins:
        await update.message.reply_text(
            "Hệ thống chưa có admin (bảng user, cột Role = admin). Liên hệ quản trị."
        )
        return ConversationHandler.END
    context.user_data.pop("reg_username", None)
    await update.message.reply_text(
        "Đăng ký tham gia hệ thống.\n\n"
        "Bước 1/2: Gửi Username (tên hiển thị trong hệ thống).\n"
        "Gõ /cancel để hủy."
    )
    return REG_USERNAME


async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Đã hủy đăng ký.")
    context.user_data.pop("reg_username", None)
    return ConversationHandler.END


async def reg_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return REG_USERNAME
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Username không được để trống. Nhập lại:")
        return REG_USERNAME
    context.user_data["reg_username"] = text
    await update.message.reply_text("Bước 2/2: Gửi email công ty của bạn.")
    return REG_EMAIL


async def reg_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return REG_EMAIL
    email = update.message.text.strip()
    if not _email_looks_valid(email):
        await update.message.reply_text("Email không hợp lệ. Nhập lại email công ty:")
        return REG_EMAIL

    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    if user_row_exists_for_telegram(sb, chat_id):
        await update.message.reply_text("Bạn đã có trong bảng user. Không gửi yêu cầu mới.")
        return ConversationHandler.END

    admins = get_admin_telegram_chat_ids(sb)
    if not admins:
        await update.message.reply_text("Không tìm thấy admin để duyệt. Thử lại sau.")
        return ConversationHandler.END

    username = (context.user_data.get("reg_username") or "").strip()
    context.user_data.pop("reg_username", None)

    req_id = secrets.token_hex(8)
    pending = _pending_registrations(context)
    pending[req_id] = {
        "status": "pending",
        "chat_id": chat_id,
        "Username": username,
        "email_congty": email,
    }

    applicant = update.effective_user
    applicant_label = (
        f"@{applicant.username}" if applicant and applicant.username else ""
    )
    if applicant_label:
        applicant_label = f" {applicant_label}"
    admin_text = (
        f"Yêu cầu đăng ký mới #{req_id}\n"
        f"Telegram:{applicant_label}\n"
        f"Chat ID: {chat_id}\n"
        f"Username đề xuất: {username}\n"
        f"Email công ty: {email}\n\n"
        f"Duyệt hoặc từ chối bằng nút bên dưới."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Duyệt",
                    callback_data=f"reg:approve:{req_id}",
                ),
                InlineKeyboardButton(
                    "Từ chối",
                    callback_data=f"reg:reject:{req_id}",
                ),
            ]
        ]
    )

    sent = 0
    for aid in admins:
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=admin_text,
                reply_markup=keyboard,
            )
            sent += 1
        except Exception as e:
            logger.warning("Gửi yêu cầu đăng ký tới admin %s: %s", aid, e)

    if sent == 0:
        pending.pop(req_id, None)
        await update.message.reply_text(
            "Không gửi được tin cho admin. Thử lại sau hoặc liên hệ quản trị."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"Đã gửi yêu cầu đăng ký tới {sent} quản trị viên. "
        "Bạn sẽ nhận thông báo khi có quyết định duyệt/từ chối."
    )
    return ConversationHandler.END


async def registration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin bấm Duyệt / Từ chối trên tin nhắn yêu cầu đăng ký."""
    query = update.callback_query
    if not query:
        return

    sb = get_supabase_client()
    if not sb:
        await query.answer()
        await query.edit_message_text("Lỗi: chưa cấu hình Supabase.")
        return

    admin_uid = update.effective_user.id if update.effective_user else None
    if admin_uid is None or not is_telegram_admin(sb, admin_uid):
        await query.answer("Bạn không có quyền duyệt đăng ký.", show_alert=True)
        return

    await query.answer()

    data = (query.data or "").strip()
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "reg" or parts[1] not in ("approve", "reject"):
        await query.edit_message_text("Dữ liệu nút không hợp lệ.")
        return

    action = parts[1]
    req_id = parts[2]
    pending = _pending_registrations(context)
    req = pending.get(req_id)
    if not req:
        await query.edit_message_text("Yêu cầu không còn hiệu lực hoặc đã xử lý.")
        return

    status = (req.get("status") or "").strip().lower()
    if status != "pending":
        await query.edit_message_text(f"Yêu cầu đã được xử lý trước đó ({status}).")
        return

    # Giữ chỗ xử lý để tránh hai admin bấm đồng thời (một luồng asyncio tại một thời điểm sau await)
    req["status"] = "processing"

    chat_id = int(req["chat_id"])
    username = (req.get("Username") or "").strip()
    email_congty = (req.get("email_congty") or "").strip()
    admin_name = (update.effective_user.full_name or "").strip() or str(admin_uid)

    if action == "reject":
        req["status"] = "rejected"
        body = (
            f"Đã TỪ CHỐI đăng ký #{req_id} bởi {admin_name}.\n"
            f"Username: {username}\nEmail công ty: {email_congty}\nChat ID: {chat_id}"
        )
        await query.edit_message_text(body)
        for aid in get_admin_telegram_chat_ids(sb):
            if aid == update.effective_chat.id:
                continue
            try:
                await context.bot.send_message(chat_id=aid, text=body)
            except Exception as e:
                logger.warning("notify reject admin %s: %s", aid, e)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Đăng ký của bạn đã bị từ chối bởi quản trị viên. Bạn không được thêm vào hệ thống.",
            )
        except Exception as e:
            logger.warning("notify reject applicant: %s", e)
        return

    if user_row_exists_for_telegram(sb, chat_id):
        req["status"] = "approved"
        msg_dup = (
            f"Đã duyệt #{req_id} nhưng user với telegram_ID={chat_id} đã tồn tại (trùng)."
        )
        await query.edit_message_text(msg_dup)
        return

    if not DEFAULT_REGISTRATION_USEREMAIL:
        await query.edit_message_text(
            "Lỗi cấu hình: thiếu DEFAULT_REGISTRATION_USEREMAIL trong .env. "
            "Admin cần đặt biến môi trường này trước khi duyệt /dk."
        )
        req["status"] = "pending"
        return

    payload = {
        "Username": username,
        "useremail": DEFAULT_REGISTRATION_USEREMAIL,
        "telegram_ID": str(chat_id),
        "gcal_refresh_token": DEFAULT_REGISTRATION_GCAL_REFRESH,
        "email_congty": email_congty,
        "Role": "Member",
    }
    try:
        sb.table(SUPABASE_USER_TABLE).insert(payload).execute()
    except Exception as e:
        logger.exception("insert user registration: %s", e)
        req["status"] = "pending"
        await query.edit_message_text(f"Lỗi khi thêm user: {e}")
        return

    req["status"] = "approved"
    ok_body = (
        f"Đã DUYỆT đăng ký #{req_id} bởi {admin_name}.\n"
        f"User đã được thêm vào bảng user (Role = Member).\n"
        f"Username: {username}\nEmail công ty: {email_congty}\nChat ID: {chat_id}"
    )
    await query.edit_message_text(ok_body)

    for aid in get_admin_telegram_chat_ids(sb):
        if aid == update.effective_chat.id:
            continue
        try:
            await context.bot.send_message(chat_id=aid, text=ok_body)
        except Exception as e:
            logger.warning("notify approve admin %s: %s", aid, e)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Đăng ký của bạn đã được duyệt. Tài khoản đã được thêm vào hệ thống "
                "(Role: Member). Bạn có thể dùng các tính năng bot theo cấu hình."
            ),
        )
    except Exception as e:
        logger.warning("notify approve applicant: %s", e)
