"""Handlers DB: /tables /refresh /query (Text-to-SQL)."""
import json
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from ..clients import get_openai_client, get_supabase_client
from ..config import AI_MODEL, MAX_QUERY_HISTORY
from ..db import execute_sql, fetch_db_schema, refresh_schema_cache
from ..state import query_history, schedule_save_state

logger = logging.getLogger(__name__)


async def cmd_tables(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase.")
        return

    await update.message.chat.send_action("typing")
    schema = fetch_db_schema(sb)
    if len(schema) > 4000:
        for i in range(0, len(schema), 4000):
            await update.message.reply_text(schema[i : i + 4000])
    else:
        await update.message.reply_text(schema)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    refresh_schema_cache()
    await update.message.reply_text("Đã xóa cache schema. Lần truy vấn sau sẽ đọc lại từ DB.")


async def cmd_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Truy vấn CSDL bằng ngôn ngữ tự nhiên (Text-to-SQL) với lịch sử hội thoại."""
    sb = get_supabase_client()
    if not sb:
        await update.message.reply_text("Chưa cấu hình Supabase. Thêm SUPABASE_URL và SUPABASE_KEY vào .env.")
        return
    query_text = context.args or []
    if not query_text:
        await update.message.reply_text(
            "Dùng: /query <câu hỏi>\n"
            "Ví dụ:\n"
            "  /query Doanh thu của Med Bắc Ninh năm 2025\n"
            "  /query Top 10 sản phẩm bán chạy nhất\n"
            "  /query Tổng số đơn hàng tháng 3/2025"
        )
        return
    user_question = " ".join(query_text).strip()
    if not user_question:
        await update.message.reply_text("Vui lòng nhập câu hỏi sau /query.")
        return

    await update.message.chat.send_action("typing")

    chat_id = update.effective_chat.id

    # Bước 1: Lấy schema thật từ DB
    schema = fetch_db_schema(sb)

    # Bước 2: Xây lịch sử query trước đó làm context
    history = query_history.get(chat_id, [])[-MAX_QUERY_HISTORY:]
    history_for_sql = []
    history_for_summary = []
    for h in history:
        history_for_sql.append({"role": "user", "content": h["question"]})
        history_for_sql.append({"role": "assistant", "content": h["sql"]})
        history_for_summary.append({"role": "user", "content": h["question"]})
        history_for_summary.append({"role": "assistant", "content": h["answer"]})

    # Bước 3: AI sinh câu SQL từ câu hỏi + schema + lịch sử
    sql_system = (
        "Bạn là chuyên gia SQL PostgreSQL. Nhiệm vụ: chuyển câu hỏi tiếng Việt thành MỘT câu SQL SELECT.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "- CHỈ trả về câu SQL thuần, không markdown, không giải thích, không ```.\n"
        "- Chỉ dùng SELECT. KHÔNG INSERT/UPDATE/DELETE/DROP.\n"
        "- LUÔN bọc tên bảng và tên cột bằng dấu ngoặc kép (double quotes) để giữ đúng chữ hoa/thường. "
        "Ví dụ: SELECT \"revenue\" FROM \"Revenue\" WHERE \"BranchName\" ILIKE '%abc%'.\n"
        "- Dùng ILIKE thay LIKE để tìm kiếm không phân biệt hoa thường.\n"
        "- Khi tìm theo tên (vd: 'Med Bắc Ninh'), dùng ILIKE '%...%'.\n"
        "- Khi câu hỏi yêu cầu tổng, đếm, trung bình... → LUÔN dùng SUM, COUNT, AVG, GROUP BY "
        "trên TOÀN BỘ dữ liệu (KHÔNG thêm LIMIT). Đây là quy tắc quan trọng nhất.\n"
        "- Chỉ thêm LIMIT khi câu hỏi yêu cầu liệt kê danh sách (top N, N dòng đầu...).\n"
        "- Dùng alias tiếng Việt cho cột kết quả khi có thể (AS \"Doanh thu\", AS \"Số lượng\").\n"
        "- Nếu người dùng hỏi tiếp nối (ví dụ: 'so sánh với năm 2024', 'còn chi nhánh khác thì sao'), "
        "hãy dựa vào lịch sử hội thoại để hiểu ngữ cảnh và sinh SQL phù hợp.\n\n"
        f"SCHEMA CƠ SỞ DỮ LIỆU:\n{schema}"
    )

    try:
        client = get_openai_client()

        sql_messages = [{"role": "system", "content": sql_system}]
        sql_messages.extend(history_for_sql)
        sql_messages.append({"role": "user", "content": user_question})

        resp1 = client.chat.completions.create(
            model=AI_MODEL,
            messages=sql_messages,
        )
        raw_sql = (resp1.choices[0].message.content or "").strip()
        if raw_sql.startswith("```"):
            raw_sql = re.sub(r"^```\w*\n?", "", raw_sql)
            raw_sql = re.sub(r"\n?```\s*$", "", raw_sql)
        raw_sql = raw_sql.strip().rstrip(";")

        logger.info("Text-to-SQL: %s -> %s", user_question, raw_sql)

        # Bước 4: Chạy SQL qua RPC
        data, err = execute_sql(sb, raw_sql)
        if err:
            await update.message.reply_text(f"Lỗi SQL: {err}\n\nCâu SQL đã sinh:\n{raw_sql}")
            return
        if not data:
            await update.message.reply_text(f"Không có kết quả.\n\nSQL: {raw_sql}")
            return

        # Bước 5: AI tổng hợp kết quả thành câu trả lời tự nhiên
        data_str = json.dumps(data[:50], ensure_ascii=False, default=str)
        if len(data_str) > 6000:
            data_str = data_str[:6000] + "..."

        summary_system = (
            "Bạn là trợ lý phân tích dữ liệu. Dựa vào kết quả truy vấn SQL bên dưới, "
            "hãy trả lời câu hỏi của người dùng bằng tiếng Việt, rõ ràng, dễ hiểu. "
            "Nếu có số liệu, format cho dễ đọc (phân cách hàng nghìn, đơn vị). "
            "Nếu có nhiều dòng, trình bày dạng danh sách ngắn gọn. "
            "Hãy tận dụng lịch sử hội thoại trước đó (nếu có) để đưa ra so sánh hoặc nhận xét thêm."
        )
        summary_user = (
            f"Câu hỏi: {user_question}\n\n"
            f"SQL đã chạy:\n{raw_sql}\n\n"
            f"Kết quả ({len(data)} dòng):\n{data_str}"
        )

        await update.message.chat.send_action("typing")

        summary_messages = [{"role": "system", "content": summary_system}]
        summary_messages.extend(history_for_summary)
        summary_messages.append({"role": "user", "content": summary_user})

        resp2 = client.chat.completions.create(
            model=AI_MODEL,
            messages=summary_messages,
        )
        answer = (resp2.choices[0].message.content or "").strip()

        # Lưu vào lịch sử query
        if chat_id not in query_history:
            query_history[chat_id] = []
        query_history[chat_id].append({
            "question": user_question,
            "sql": raw_sql,
            "answer": answer,
        })
        query_history[chat_id] = query_history[chat_id][-MAX_QUERY_HISTORY:]
        schedule_save_state(chat_id)

        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i : i + 4000])
        else:
            await update.message.reply_text(answer)

    except Exception as e:
        logger.exception("cmd_query: %s", e)
        await update.message.reply_text(f"Có lỗi: {str(e)}")
