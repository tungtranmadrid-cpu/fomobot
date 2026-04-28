-- ============================================================
-- calendar_db_setup.sql
-- Chạy trong Supabase SQL Editor.
-- Bảng trung tâm cho tất cả lịch họp từ MS Teams + Google Calendar.
-- ============================================================

-- ============================================================
-- 1. Bảng calendar_events — sự kiện từ mọi nguồn
-- ============================================================
CREATE TABLE IF NOT EXISTS calendar_events (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Nguồn & ID ngoài để sync
  source        TEXT        NOT NULL DEFAULT 'manual', -- 'ms_teams' | 'google' | 'manual'
  external_id   TEXT,       -- ID trong MS Graph hoặc Google Calendar

  -- Nội dung cuộc họp
  title         TEXT        NOT NULL,
  description   TEXT,       -- body / biên bản / agenda
  location      TEXT,

  -- Thời gian (lưu UTC, hiển thị theo timezone)
  start_time    TIMESTAMPTZ NOT NULL,
  end_time      TIMESTAMPTZ NOT NULL,
  is_all_day    BOOLEAN     NOT NULL DEFAULT false,
  timezone      TEXT        NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',

  -- Người tổ chức
  organizer_name  TEXT,
  organizer_email TEXT,

  -- Link họp trực tuyến (Teams / Meet / Zoom)
  meeting_url      TEXT,
  meeting_platform TEXT,    -- 'teams' | 'meet' | 'zoom' | ...

  -- Trạng thái
  status        TEXT        NOT NULL DEFAULT 'confirmed', -- confirmed | cancelled | tentative

  -- Lặp lịch
  is_recurring  BOOLEAN     NOT NULL DEFAULT false,
  recurrence_rule TEXT,     -- RRULE (RFC 5545)

  -- Metadata
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ          -- soft delete
);

-- Unique: mỗi nguồn chỉ có 1 bản ghi cho 1 external_id
CREATE UNIQUE INDEX IF NOT EXISTS calendar_events_source_ext_idx
  ON calendar_events (source, external_id)
  WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS calendar_events_start_idx
  ON calendar_events (start_time) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS calendar_events_date_tz_idx
  ON calendar_events (DATE(start_time AT TIME ZONE 'Asia/Ho_Chi_Minh'))
  WHERE deleted_at IS NULL;

-- ============================================================
-- 2. Bảng event_attendees — danh sách tham dự
-- ============================================================
CREATE TABLE IF NOT EXISTS event_attendees (
  id              UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id        UUID  NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,

  name            TEXT,
  email           TEXT  NOT NULL,
  response_status TEXT  NOT NULL DEFAULT 'none', -- accepted | declined | tentative | none
  attendee_type   TEXT  NOT NULL DEFAULT 'required', -- required | optional

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (event_id, email)
);

CREATE INDEX IF NOT EXISTS event_attendees_event_idx ON event_attendees (event_id);
CREATE INDEX IF NOT EXISTS event_attendees_email_idx ON event_attendees (lower(email));

-- ============================================================
-- 3. Trigger tự cập nhật updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION _touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS calendar_events_updated ON calendar_events;
CREATE TRIGGER calendar_events_updated
  BEFORE UPDATE ON calendar_events
  FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();

DROP TRIGGER IF EXISTS event_attendees_updated ON event_attendees;
CREATE TRIGGER event_attendees_updated
  BEFORE UPDATE ON event_attendees
  FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();

-- ============================================================
-- 4. Hàm get_events_for_user_date — lấy lịch của 1 user trong ngày
--    Bot dùng hàm này thay vì gọi API Google/MS trực tiếp.
-- ============================================================
CREATE OR REPLACE FUNCTION get_events_for_user_date(
  p_email TEXT,
  p_date  DATE,
  p_tz    TEXT DEFAULT 'Asia/Ho_Chi_Minh'
)
RETURNS TABLE (
  id              UUID,
  source          TEXT,
  external_id     TEXT,
  title           TEXT,
  description     TEXT,
  location        TEXT,
  start_time      TIMESTAMPTZ,
  end_time        TIMESTAMPTZ,
  is_all_day      BOOLEAN,
  timezone        TEXT,
  organizer_name  TEXT,
  organizer_email TEXT,
  meeting_url     TEXT,
  meeting_platform TEXT,
  status          TEXT,
  attendees       JSONB
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT
    e.id, e.source, e.external_id,
    e.title, e.description, e.location,
    e.start_time, e.end_time, e.is_all_day, e.timezone,
    e.organizer_name, e.organizer_email,
    e.meeting_url, e.meeting_platform, e.status,
    COALESCE(
      (SELECT jsonb_agg(jsonb_build_object(
          'name',            a.name,
          'email',           a.email,
          'response_status', a.response_status,
          'attendee_type',   a.attendee_type
        ) ORDER BY a.attendee_type, a.name)
       FROM event_attendees a WHERE a.event_id = e.id),
      '[]'::jsonb
    ) AS attendees
  FROM calendar_events e
  WHERE
    e.deleted_at IS NULL
    AND e.status <> 'cancelled'
    AND DATE(e.start_time AT TIME ZONE p_tz) = p_date
    AND (
      lower(e.organizer_email) = lower(p_email)
      OR EXISTS (
        SELECT 1 FROM event_attendees a
        WHERE a.event_id = e.id AND lower(a.email) = lower(p_email)
      )
    )
  ORDER BY e.is_all_day DESC, e.start_time ASC;
$$;

REVOKE ALL ON FUNCTION get_events_for_user_date(TEXT, DATE, TEXT) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION get_events_for_user_date(TEXT,DATE,TEXT) TO service_role';
  END IF;
END $$;

-- ============================================================
-- 5. Hàm upsert_calendar_event — gọi từ webhook Edge Function
--    Tạo mới hoặc cập nhật event + attendees theo (source, external_id).
-- ============================================================
CREATE OR REPLACE FUNCTION upsert_calendar_event(
  p_source          TEXT,
  p_external_id     TEXT,
  p_title           TEXT,
  p_description     TEXT        DEFAULT NULL,
  p_location        TEXT        DEFAULT NULL,
  p_start_time      TIMESTAMPTZ DEFAULT NULL,
  p_end_time        TIMESTAMPTZ DEFAULT NULL,
  p_is_all_day      BOOLEAN     DEFAULT false,
  p_timezone        TEXT        DEFAULT 'Asia/Ho_Chi_Minh',
  p_organizer_name  TEXT        DEFAULT NULL,
  p_organizer_email TEXT        DEFAULT NULL,
  p_meeting_url     TEXT        DEFAULT NULL,
  p_meeting_platform TEXT       DEFAULT NULL,
  p_status          TEXT        DEFAULT 'confirmed',
  p_is_recurring    BOOLEAN     DEFAULT false,
  p_recurrence_rule TEXT        DEFAULT NULL,
  p_attendees       JSONB       DEFAULT '[]'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_id  UUID;
  v_att JSONB;
BEGIN
  -- Tìm bản ghi hiện có
  SELECT id INTO v_id
  FROM calendar_events
  WHERE source = p_source AND external_id = p_external_id;

  IF v_id IS NULL THEN
    -- Insert mới
    INSERT INTO calendar_events (
      source, external_id, title, description, location,
      start_time, end_time, is_all_day, timezone,
      organizer_name, organizer_email,
      meeting_url, meeting_platform,
      status, is_recurring, recurrence_rule,
      deleted_at
    ) VALUES (
      p_source, p_external_id, p_title, p_description, p_location,
      p_start_time, p_end_time, p_is_all_day, p_timezone,
      p_organizer_name, p_organizer_email,
      p_meeting_url, p_meeting_platform,
      p_status, p_is_recurring, p_recurrence_rule,
      CASE WHEN p_status = 'cancelled' THEN now() ELSE NULL END
    ) RETURNING id INTO v_id;
  ELSE
    -- Cập nhật
    UPDATE calendar_events SET
      title            = p_title,
      description      = p_description,
      location         = p_location,
      start_time       = COALESCE(p_start_time, start_time),
      end_time         = COALESCE(p_end_time, end_time),
      is_all_day       = p_is_all_day,
      timezone         = p_timezone,
      organizer_name   = p_organizer_name,
      organizer_email  = p_organizer_email,
      meeting_url      = p_meeting_url,
      meeting_platform = p_meeting_platform,
      status           = p_status,
      is_recurring     = p_is_recurring,
      recurrence_rule  = p_recurrence_rule,
      deleted_at       = CASE WHEN p_status = 'cancelled' THEN now() ELSE NULL END,
      updated_at       = now()
    WHERE id = v_id;
  END IF;

  -- Đồng bộ attendees: xóa cũ, insert mới
  DELETE FROM event_attendees WHERE event_id = v_id;

  FOR v_att IN SELECT * FROM jsonb_array_elements(p_attendees) LOOP
    INSERT INTO event_attendees (event_id, name, email, response_status, attendee_type)
    VALUES (
      v_id,
      v_att->>'name',
      COALESCE(v_att->>'email', ''),
      COALESCE(v_att->>'response_status', v_att->>'responseStatus', 'none'),
      COALESCE(v_att->>'attendee_type', v_att->>'type', 'required')
    )
    ON CONFLICT (event_id, email) DO UPDATE SET
      name            = EXCLUDED.name,
      response_status = EXCLUDED.response_status,
      attendee_type   = EXCLUDED.attendee_type,
      updated_at      = now();
  END LOOP;

  RETURN v_id;
END;
$$;

REVOKE ALL ON FUNCTION upsert_calendar_event FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION upsert_calendar_event TO service_role';
  END IF;
END $$;

-- ============================================================
-- 6. Hàm soft-delete event theo (source, external_id)
--    Gọi khi nhận webhook "deleted" / "cancelled"
-- ============================================================
CREATE OR REPLACE FUNCTION cancel_calendar_event(
  p_source      TEXT,
  p_external_id TEXT
)
RETURNS VOID
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  UPDATE calendar_events
  SET status     = 'cancelled',
      deleted_at = now(),
      updated_at = now()
  WHERE source = p_source AND external_id = p_external_id;
$$;

REVOKE ALL ON FUNCTION cancel_calendar_event(TEXT, TEXT) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION cancel_calendar_event(TEXT,TEXT) TO service_role';
  END IF;
END $$;
