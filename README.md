# Telegram AI Bot

Bot Telegram tích hợp AI đa năng dành cho doanh nghiệp: chat thông minh, nhắc lịch Google Calendar, tóm tắt biên bản cuộc họp, trích xuất công việc, truy vấn CSDL bằng tiếng Việt, và hỏi đáp tài liệu nội bộ (RAG).

---

## Mục lục

- [Kiến trúc tổng quan](#kiến-trúc-tổng-quan)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [Các chức năng chính](#các-chức-năng-chính)
- [Cơ sở dữ liệu Supabase](#cơ-sở-dữ-liệu-supabase)
- [Cài đặt & chạy](#cài-đặt--chạy)
- [Biến môi trường](#biến-môi-trường)
- [Lệnh Telegram](#lệnh-telegram)

---

## Kiến trúc tổng quan

```
Telegram User
     │  (HTTP long-polling)
     ▼
python-telegram-bot (Application)
     │
     ├── TypeHandler (capture_incoming_update) ──► Supabase: telegram_chat_logs
     │
     ├── ConversationHandler /dk ─────────────────► Supabase: user (admin approval)
     │
     ├── CallbackQueryHandler
     │     ├── reg:(approve|reject) ─────────────► Supabase: user
     │     └── meetpick:<idx> ────────────────────► Google Calendar API
     │
     ├── CommandHandlers
     │     ├── /start  /clear  /id  /model  /think
     │     ├── /lich   /tomtat ──────────────────► Google Calendar API
     │     ├── /tables /refresh /query ──────────► Supabase RPC (Text-to-SQL)
     │     └── /rag_index /ask ──────────────────► Supabase Storage + pgvector
     │
     └── MessageHandler (free text)
           ├── Intent: meeting detail? ──────────► Google Calendar API
           ├── Intent: calendar? ────────────────► Google Calendar API
           └── Fallback: LLM chat ───────────────► OpenAI / Deepseek API

JobQueue (hàng ngày lúc 07:00 Asia/Ho_Chi_Minh)
     └── daily_calendar_reminder ─────────────────► Google Calendar API → Supabase: user
```

### Các thành phần bên ngoài

| Thành phần | Vai trò |
|---|---|
| **Telegram Bot API** | Giao tiếp với người dùng qua long-polling |
| **OpenAI / Deepseek** | Chat AI, Text-to-SQL, intent detection, tóm tắt biên bản |
| **OpenAI Embedding** | Tạo vector cho RAG (có thể dùng key riêng) |
| **Supabase (PostgreSQL)** | Lưu user, state, log chat, RAG chunks, meeting tasks |
| **Supabase Storage** | Lưu file tài liệu để index RAG |
| **Google Calendar API** | Đọc lịch (Service Account, OAuth cá nhân, hoặc master aggregator) |

---

## Cấu trúc thư mục

```
jkv/
├── bot/
│   ├── main.py                  # Entrypoint: đăng ký handlers, chạy polling
│   ├── config.py                # Load .env, expose hằng số toàn cục
│   ├── clients.py               # Factory: OpenAI client, Embedding client, Supabase client
│   ├── db.py                    # Schema introspection + execute read-only SQL (RPC)
│   ├── state.py                 # State per chat_id (RAM + sync Supabase bot_state)
│   ├── chat_log.py              # LoggingBot: ghi toàn bộ tin nhắn vào Supabase
│   ├── rate_limit.py            # Token bucket rate limiter chống spam
│   ├── async_utils.py           # run_blocking: chạy hàm sync trong thread pool
│   ├── telegram_utils.py        # reply_safe: gửi tin nhắn an toàn
│   │
│   ├── handlers/
│   │   ├── basic.py             # /start /clear /id /think /model
│   │   ├── chat.py              # Free-text: intent routing → LLM fallback
│   │   ├── calendar_cmd.py      # /lich /tomtat + answer_calendar/meeting_detail
│   │   ├── db_cmd.py            # /tables /refresh /query (Text-to-SQL)
│   │   ├── rag_cmd.py           # /rag_index /ask (RAG)
│   │   ├── registration.py      # /dk ConversationHandler + admin approval
│   │   └── capture.py           # TypeHandler ghi log mọi update
│   │
│   ├── calendar/
│   │   ├── auth.py              # Xác thực Google (Service Account / OAuth)
│   │   ├── fetch.py             # Lấy events từ Google Calendar API
│   │   ├── format.py            # Format lịch/chi tiết cuộc họp thành text
│   │   ├── intent.py            # NLU: nhận diện intent lịch, parse ngày tiếng Việt, chọn event bằng AI
│   │   ├── profile.py           # Lấy thông tin Google Calendar của user từ Supabase
│   │   ├── reminder.py          # JobQueue nhắc lịch hàng ngày + đăng ký bot commands
│   │   └── tasks.py             # LLM tóm tắt biên bản + trích task + lưu meeting_tasks
│   │
│   └── rag/
│       ├── chunker.py           # Chia văn bản thành chunks
│       ├── embedding.py         # Tạo + encode embedding vector
│       ├── extractors.py        # Trích text từ PDF / file text trong Supabase Storage
│       ├── indexer.py           # Quét Storage, chunk, embed, insert rag_chunks
│       └── search.py            # Vector search + keyword fallback trên rag_chunks
│
├── get_gcal_refresh_token.py    # Script lấy OAuth refresh token cho Google Calendar
├── list_telegram_chat_ids.py    # Script liệt kê chat ID Telegram
├── QUERY_SETUP.sql              # SQL khởi tạo Supabase: functions, tables, indexes
├── requirements.txt             # Python dependencies
├── .env.example                 # Mẫu biến môi trường
└── telegram_ai_bot.py           # Entrypoint đơn giản (gọi bot.main)
```

---

## Các chức năng chính

### 1. Chat AI tự do
- Người dùng nhắn tin bình thường → bot trả lời bằng OpenAI hoặc Deepseek.
- Lưu lịch sử hội thoại per chat_id (tối đa 20 lượt, sync Supabase).
- **Chế độ Suy nghĩ** (`/think`): AI trình bày bước suy luận trước khi trả lời.

### 2. Tích hợp Google Calendar
Bot hỗ trợ **3 cách xác thực** Google Calendar:
- **Service Account** (Google Workspace, domain-wide delegation)
- **OAuth cá nhân** (Gmail thông thường, refresh token per user)
- **Master aggregator** (1 tài khoản gom lịch công ty, lọc theo `email_congty`)

**Nhắc lịch hàng ngày** (mặc định 07:00 ICT): Bot tự động gửi lịch trong ngày cho mọi user có `telegram_ID` trong bảng `user`.

### 3. Xem lịch và chi tiết cuộc họp
- `/lich [nay|mai|dd/mm|thứ N]` — xem danh sách sự kiện theo ngày.
- Hỏi tự nhiên: *"lịch hôm nay"*, *"thứ 3 tuần sau có gì"* → bot nhận diện intent và trả lịch.
- Hỏi chi tiết: *"cuộc họp sáng nay ai tham dự"*, *"tài liệu buổi họp chiều thứ 4"* → bot lấy chi tiết từ Google Calendar.
- Khi có nhiều cuộc họp cùng ngày, bot hiển thị **inline keyboard** để user chọn.

**NLU parse ngày tiếng Việt** hỗ trợ:
- Từ khoá: `hôm nay`, `mai`, `ngày kia`, `hôm qua`, `hôm kia`
- Thứ trong tuần: `thứ 2`…`thứ 7`, `t2`…`t7`, `chủ nhật`
- Tuần: `tuần này`, `tuần sau`, `tuần trước`, `đầu tuần`, `cuối tuần`
- Ngày cụ thể: `26/03`, `26-03-2026`
- Buổi trong ngày: `sáng`, `chiều`, `tối` (lọc theo khung giờ)

### 4. Tóm tắt biên bản + trích xuất công việc
`/tomtat [nay|mai|sáng nay|chiều mai|26/03]`

Khi sự kiện Google Calendar có **biên bản họp** trong phần mô tả (description):
1. LLM đọc toàn bộ biên bản → **tóm tắt** 3 nhóm: thảo luận, quyết định, lưu ý.
2. LLM **trích xuất** mọi công việc được phân công: tên CV, chi tiết, người thực hiện, deadline.
3. Bot **resolve assignees**: khớp tên người trong biên bản với bảng `members` (normalize tiếng Việt, strip kính ngữ, fallback theo phòng/ban).
4. Bot **lưu vào** bảng `meeting_tasks` (dedup theo `meeting_event_id`).
5. Hiển thị bảng tổng hợp công việc từ tất cả cuộc họp trong ngày.

### 5. Truy vấn CSDL bằng ngôn ngữ tự nhiên (Text-to-SQL)
`/query <câu hỏi>`

Pipeline 2 bước:
1. **AI sinh SQL** từ câu hỏi + schema CSDL thực + lịch sử hội thoại.
2. **Chạy SQL** qua RPC `execute_readonly_sql` (chỉ SELECT, read-only transaction, timeout 5s).
3. **AI tổng hợp** kết quả thành câu trả lời tiếng Việt tự nhiên có so sánh context.

Bảo mật:
- SQL được strip comment, kiểm tra chỉ cho phép SELECT/WITH.
- Role `bot_readonly` chỉ có quyền SELECT.
- `SET LOCAL transaction_read_only = on` — Postgres từ chối mọi thao tác ghi.

### 6. RAG — Hỏi đáp tài liệu nội bộ
- `/rag_index` — quét Supabase Storage (PDF + text), chunk, tạo embedding, lưu `rag_chunks`.
- `/ask <câu hỏi>` — tìm chunks liên quan (vector search ưu tiên, fallback keyword), AI trả lời dựa trên context tài liệu.

Pipeline:
1. Tải file từ Supabase Storage.
2. Trích text (PDF qua PyMuPDF, text decode tự động encoding).
3. Chia chunks (configurable: size 800, overlap 100).
4. Tạo embedding (OpenAI `text-embedding-3-small`, batch 50).
5. Insert vào `rag_chunks` (pgvector + HNSW index).
6. Tìm kiếm: cosine similarity hoặc keyword fallback (`pg_trgm`).

### 7. Đăng ký người dùng (`/dk`)
ConversationHandler 2 bước (chỉ trong chat riêng):
1. Nhập Username → nhập email công ty.
2. Bot gửi yêu cầu tới tất cả admin (inline button Duyệt/Từ chối).
3. Admin bấm Duyệt → bot insert user vào bảng `user` (Role = Member).

Kiểm tra: chống đăng ký trùng, validate email, chỉ admin mới duyệt được.

### 8. Quản lý trạng thái
- **RAM cache** per chat_id: lịch sử hội thoại, lịch sử query SQL, chế độ thinking.
- **Sync Supabase** (`bot_state`): upsert fire-and-forget mỗi khi state thay đổi.
- **Load on demand**: lần đầu chat sẽ nạp state từ DB, các lần sau dùng RAM.

### 9. Logging & Rate limiting
- Mọi tin nhắn vào/ra đều được ghi vào `telegram_chat_logs` (LoggingBot).
- Token bucket rate limiter: mặc định 20 tin/phút, burst 5.

---

## Cơ sở dữ liệu Supabase

Schema khởi tạo bằng `QUERY_SETUP.sql`:

| Bảng / Function | Mô tả |
|---|---|
| `user` | Thông tin user: username, email công ty, telegram_ID, gcal_refresh_token, Role |
| `members` | Danh sách nhân sự: họ tên, chức vụ, nơi làm việc, email |
| `bot_state` | State per chat_id: conversation history, query history, thinking mode |
| `telegram_chat_logs` | Log toàn bộ tin nhắn vào/ra bot |
| `rag_chunks` | Chunks tài liệu + embedding vector (pgvector 1536 chiều) |
| `meeting_tasks` | Công việc trích từ biên bản cuộc họp |
| `get_schema_info()` | RPC: trả schema thực (bảng, cột, kiểu dữ liệu) |
| `execute_readonly_sql(query)` | RPC: chạy SELECT an toàn (read-only) |
| `search_rag_by_embedding(emb, k)` | RPC: vector similarity search |
| `search_rag_chunks(keywords, k)` | RPC: keyword fulltext search (fallback) |
| `truncate_rag_chunks()` | RPC: xóa toàn bộ chunks trước khi re-index |

---

## Cài đặt & chạy

### Yêu cầu
- Python 3.8+
- Tài khoản Telegram Bot (từ @BotFather)
- OpenAI API key (hoặc Deepseek)
- Supabase project

### Bước 1: Cài dependencies

```bash
pip install -r requirements.txt
```

### Bước 2: Tạo file `.env`

```bash
cp .env.example .env
# Điền TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY
```

### Bước 3: Khởi tạo Supabase

Chạy toàn bộ nội dung `QUERY_SETUP.sql` trong **Supabase SQL Editor**.

### Bước 4: Chạy bot

```bash
python telegram_ai_bot.py
```

### Cấu hình Google Calendar (tuỳ chọn)

**Cách 1 — Service Account (Google Workspace):**
```env
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/sa.json
```

**Cách 2 — OAuth cá nhân (Gmail):**
```bash
python get_gcal_refresh_token.py
```
```env
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
# gcal_refresh_token lưu vào cột gcal_refresh_token trong bảng user
```

**Cách 3 — Master aggregator (1 Gmail gom lịch công ty):**
```env
GCAL_MASTER_EMAIL=company@gmail.com
GCAL_MASTER_REFRESH_TOKEN=...
```

---

## Biến môi trường

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token từ @BotFather |
| `OPENAI_API_KEY` | ✅ | OpenAI hoặc Deepseek API key |
| `OPENAI_BASE_URL` | | Đặt nếu dùng Deepseek (`https://api.deepseek.com`) |
| `AI_MODEL` | | Model chat (mặc định: `gpt-4o-mini` / `deepseek-chat`) |
| `SUPABASE_URL` | ✅ | URL project Supabase |
| `SUPABASE_KEY` | ✅ | Service role key Supabase |
| `OPENAI_EMBEDDING_API_KEY` | | API key riêng cho embedding (RAG) |
| `EMBEDDING_MODEL` | | `text-embedding-3-small` (mặc định) |
| `GCALENDAR_TZ` | | Timezone (mặc định: `Asia/Ho_Chi_Minh`) |
| `DAILY_CALENDAR_HOUR` | | Giờ gửi nhắc lịch (mặc định: `7`) |
| `DAILY_CALENDAR_MINUTE` | | Phút gửi nhắc lịch (mặc định: `0`) |
| `DEFAULT_REGISTRATION_USEREMAIL` | | Email mặc định khi admin duyệt `/dk` |
| `RATE_LIMIT_PER_MINUTE` | | Giới hạn tin/phút (mặc định: `20`) |
| `RAG_CHUNK_SIZE` | | Kích thước chunk (mặc định: `800`) |
| `RAG_TOP_K` | | Số chunks trả về khi search (mặc định: `8`) |

---

## Lệnh Telegram

| Lệnh | Mô tả |
|---|---|
| `/start` | Lời chào + danh sách lệnh khả dụng |
| `/clear` | Xóa lịch sử hội thoại và lịch sử query |
| `/id` | Xem Chat ID (để thêm vào bảng `user`) |
| `/model` | Xem model AI và base URL đang dùng |
| `/think` | Bật/tắt chế độ suy nghĩ từng bước |
| `/lich [ngày]` | Xem lịch Google Calendar theo ngày |
| `/tomtat [ngày]` | Tóm tắt biên bản + tổng hợp công việc từ cuộc họp |
| `/query <câu hỏi>` | Truy vấn Supabase bằng ngôn ngữ tự nhiên |
| `/tables` | Xem cấu trúc CSDL (bảng, cột) |
| `/refresh` | Cập nhật lại cache schema |
| `/rag_index` | Index tài liệu từ Supabase Storage vào RAG |
| `/ask <câu hỏi>` | Hỏi đáp dựa trên tài liệu đã index |
| `/dk` | Đăng ký tham gia hệ thống (chờ admin duyệt) |
| `/cancel` | Hủy đang ký `/dk` |
