-- Chạy script này trong Supabase SQL Editor (Dashboard -> SQL Editor)
--
-- ============================================================
-- 0. Google Calendar — cột OAuth refresh token (bảng user mặc định)
--    Bot đọc useremail, gcal_refresh_token, Username, telegram_ID.
--    Chạy phần này nếu gặp lỗi: column user.gcal_refresh_token does not exist
-- ============================================================
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'user'
      AND column_name = 'gcal_refresh_token'
  ) THEN
    ALTER TABLE public."user" ADD COLUMN gcal_refresh_token TEXT;
  END IF;
END $$;

-- Tạo 2 hàm RPC để bot có thể:
--   1. Đọc schema thật của CSDL (bảng, cột, kiểu dữ liệu)
--   2. Chạy câu SQL SELECT an toàn (chỉ đọc, không sửa/xóa)

-- ============================================================
-- 1. Hàm lấy schema: trả về tất cả bảng + cột trong schema public
-- ============================================================
CREATE OR REPLACE FUNCTION get_schema_info()
RETURNS TABLE(
  table_name text,
  column_name text,
  data_type text,
  is_nullable text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT
    c.table_name::text,
    c.column_name::text,
    c.data_type::text,
    c.is_nullable::text
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON c.table_name = t.table_name
    AND c.table_schema = t.table_schema
  WHERE c.table_schema = 'public'
    AND t.table_type = 'BASE TABLE'
  ORDER BY c.table_name, c.ordinal_position;
$$;

REVOKE ALL ON FUNCTION get_schema_info() FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION get_schema_info() TO service_role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION get_schema_info() TO authenticated';
  END IF;
END $$;

-- ============================================================
-- 2. Hàm chạy SQL read-only
--    Lớp phòng thủ:
--    (a) Chỉ chấp nhận câu bắt đầu SELECT / WITH (sau khi strip comment).
--    (b) Chặn keyword ghi/DDL (belt & suspenders).
--    (c) SET LOCAL transaction_read_only = on  -> Postgres tự từ chối mọi
--        thao tác ghi (kể cả ẩn trong CTE, function SECURITY DEFINER khác,
--        subquery, v.v.).
--    (d) SET LOCAL statement_timeout = 5s       -> tránh query treo DB.
--    (e) Hàm chạy với SECURITY INVOKER — quyền của caller (bot dùng role
--        riêng `bot_readonly`, xem mục 2b). Không còn escalate privilege.
-- ============================================================

-- 2a. Role chỉ đọc dành riêng cho bot
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot_readonly') THEN
    CREATE ROLE bot_readonly NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO bot_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO bot_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO bot_readonly;

-- Cho service_role (Supabase) "mượn" quyền của bot_readonly khi cần:
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT bot_readonly TO service_role';
  END IF;
END $$;

-- 2b. Hàm
CREATE OR REPLACE FUNCTION execute_readonly_sql(query text)
RETURNS json
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  result json;
  stripped text;
  normalized text;
BEGIN
  -- Strip block comment /* ... */ và line comment -- ...
  stripped := regexp_replace(query, '/\*.*?\*/', '', 'gs');
  stripped := regexp_replace(stripped, '--[^\n]*', '', 'g');
  normalized := lower(btrim(stripped));

  IF NOT (normalized LIKE 'select %'
          OR normalized LIKE 'select('
          OR normalized LIKE 'with %') THEN
    RAISE EXCEPTION 'Chỉ cho phép câu lệnh SELECT / WITH.';
  END IF;

  IF normalized ~ '\y(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|reindex|vacuum|analyze|refresh|listen|notify|lock|begin|commit|rollback|savepoint|set)\y' THEN
    RAISE EXCEPTION 'Câu lệnh chứa từ khoá bị cấm.';
  END IF;

  -- Lớp bảo vệ cuối: Postgres từ chối mọi write trong transaction này.
  SET LOCAL transaction_read_only = on;
  SET LOCAL statement_timeout = '5s';
  SET LOCAL lock_timeout = '2s';

  EXECUTE format(
    'SELECT coalesce(json_agg(row_to_json(t)), ''[]''::json) FROM (%s) t',
    query
  ) INTO result;

  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION execute_readonly_sql(text) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION execute_readonly_sql(text) TO service_role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION execute_readonly_sql(text) TO authenticated';
  END IF;
END $$;

-- ============================================================
-- 3. Bảng RAG: lưu chunk text + embedding (vector) để tìm kiếm ngữ nghĩa
-- ============================================================

-- Bật extension vector (embedding) và tìm kiếm gần đúng (fallback)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS rag_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source TEXT,
  content TEXT NOT NULL,
  embedding vector(1536),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Thêm cột embedding nếu bảng đã tồn tại từ trước (chạy migration)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'rag_chunks' AND column_name = 'embedding'
  ) THEN
    ALTER TABLE rag_chunks ADD COLUMN embedding vector(1536);
  END IF;
END $$;

-- Index cho tìm kiếm vector (cosine distance)
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx
  ON rag_chunks USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS rag_chunks_content_trgm_idx
  ON rag_chunks USING gin (content gin_trgm_ops);

-- ============================================================
-- 4. Hàm tìm chunk theo embedding (vector similarity)
-- query_embedding_text: dạng '[0.1, -0.2, ...]' (1536 số) từ client
-- ============================================================
CREATE OR REPLACE FUNCTION search_rag_by_embedding(
  query_embedding_text text,
  match_count int DEFAULT 10
)
RETURNS TABLE(id uuid, source text, content text, similarity float)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  q vector(1536);
BEGIN
  q := query_embedding_text::vector(1536);
  RETURN QUERY
  SELECT
    c.id, c.source, c.content,
    (1 - (c.embedding <=> q))::float AS similarity
  FROM rag_chunks c
  WHERE c.embedding IS NOT NULL
  ORDER BY c.embedding <=> q
  LIMIT match_count;
END;
$$;

REVOKE ALL ON FUNCTION search_rag_by_embedding(text, int) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION search_rag_by_embedding(text, int) TO service_role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION search_rag_by_embedding(text, int) TO authenticated';
  END IF;
END $$;

-- Xóa toàn bộ chunk (gọi trước khi re-index)
CREATE OR REPLACE FUNCTION truncate_rag_chunks()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  TRUNCATE TABLE rag_chunks;
$$;

REVOKE ALL ON FUNCTION truncate_rag_chunks() FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION truncate_rag_chunks() TO service_role';
  END IF;
END $$;

-- ============================================================
-- 5. Hàm tìm chunk theo từ khóa (fallback khi chưa có embedding)
-- ============================================================
CREATE OR REPLACE FUNCTION search_rag_chunks(
  keywords text[],
  match_count int DEFAULT 10
)
RETURNS TABLE(id uuid, source text, content text, relevance bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  RETURN QUERY
  SELECT
    c.id, c.source, c.content,
    (SELECT count(*) FROM unnest(keywords) k WHERE c.content ILIKE '%' || k || '%') AS relevance
  FROM rag_chunks c
  WHERE EXISTS (
    SELECT 1 FROM unnest(keywords) k WHERE c.content ILIKE '%' || k || '%'
  )
  ORDER BY relevance DESC, c.created_at DESC
  LIMIT match_count;
END;
$$;

REVOKE ALL ON FUNCTION search_rag_chunks(text[], int) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION search_rag_chunks(text[], int) TO service_role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION search_rag_chunks(text[], int) TO authenticated';
  END IF;
END $$;

-- ============================================================
-- 6. Bảng log chat Telegram: lưu toàn bộ tin nhắn vào/ra bot
-- ============================================================
CREATE TABLE IF NOT EXISTS telegram_chat_logs (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
  message_text TEXT NOT NULL DEFAULT '',
  message_type TEXT NOT NULL DEFAULT 'text',
  telegram_user_id BIGINT,
  telegram_username TEXT,
  telegram_full_name TEXT,
  update_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS telegram_chat_logs_chat_id_idx
  ON telegram_chat_logs (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS telegram_chat_logs_created_at_idx
  ON telegram_chat_logs (created_at DESC);

-- ============================================================
-- 7.5. Bảng meeting_tasks: công việc trích từ biên bản cuộc họp.
--      Bot (LLM) đọc description của event Google Calendar → ra
--      danh sách task (tên, chi tiết, người thực hiện, deadline).
-- ============================================================
CREATE TABLE IF NOT EXISTS meeting_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_event_id TEXT NOT NULL,
  meeting_summary TEXT,
  meeting_start TIMESTAMPTZ,
  task_name TEXT NOT NULL,
  task_detail TEXT,
  assignee_name TEXT,
  assignee_email TEXT,
  assignee_chat_id BIGINT,
  deadline DATE,
  deadline_raw TEXT,
  created_by_chat_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS meeting_tasks_event_idx
  ON meeting_tasks (meeting_event_id);
CREATE INDEX IF NOT EXISTS meeting_tasks_deadline_idx
  ON meeting_tasks (deadline);
CREATE INDEX IF NOT EXISTS meeting_tasks_assignee_chat_idx
  ON meeting_tasks (assignee_chat_id);

-- ============================================================
-- 8. Bảng bot_state: persistent state per chat_id (conversation,
--    query_history, thinking). Bot tự upsert khi state thay đổi.
-- ============================================================
CREATE TABLE IF NOT EXISTS bot_state (
  chat_id BIGINT PRIMARY KEY,
  conversation JSONB NOT NULL DEFAULT '[]'::jsonb,
  query_history JSONB NOT NULL DEFAULT '[]'::jsonb,
  thinking BOOLEAN NOT NULL DEFAULT false,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION bot_state_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS bot_state_touch ON bot_state;
CREATE TRIGGER bot_state_touch
  BEFORE UPDATE ON bot_state
  FOR EACH ROW EXECUTE FUNCTION bot_state_touch_updated_at();
