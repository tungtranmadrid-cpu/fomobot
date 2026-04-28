/**
 * Supabase Edge Function: nhận webhook từ Google Calendar push notifications.
 * Deploy: supabase functions deploy webhook-gcal
 *
 * Đăng ký channel (gọi 1 lần, renew định kỳ):
 *   POST https://www.googleapis.com/calendar/v3/calendars/<calendarId>/events/watch
 *   Authorization: Bearer <access_token>
 *   {
 *     "id": "<uuid>",
 *     "type": "web_hook",
 *     "address": "https://<project>.supabase.co/functions/v1/webhook-gcal",
 *     "token": "<GCAL_WEBHOOK_SECRET>",
 *     "expiration": <unix_ms>
 *   }
 *
 * Env vars cần đặt trong Supabase Dashboard → Edge Functions → Secrets:
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (tự có)
 *   GCAL_WEBHOOK_SECRET    — token để verify request
 *   GOOGLE_SERVICE_ACCOUNT_JSON — JSON của Service Account (stringify)
 *   GCALENDAR_TZ           — vd. Asia/Ho_Chi_Minh
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL    = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY     = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const WEBHOOK_SECRET  = Deno.env.get("GCAL_WEBHOOK_SECRET") ?? "";
const SA_JSON_RAW     = Deno.env.get("GOOGLE_SERVICE_ACCOUNT_JSON") ?? "";
const TZ              = Deno.env.get("GCALENDAR_TZ") ?? "Asia/Ho_Chi_Minh";

// Lưu syncToken và channelId theo calendarId để incremental sync
const syncTokenStore: Record<string, string> = {};

// ---------- Lấy access token bằng Service Account (JWT) ----------
async function getGCalToken(sa: Record<string, string>): Promise<string> {
  const header  = btoa(JSON.stringify({ alg: "RS256", typ: "JWT" })).replace(/=/g, "");
  const now     = Math.floor(Date.now() / 1000);
  const payload = btoa(JSON.stringify({
    iss:   sa.client_email,
    scope: "https://www.googleapis.com/auth/calendar.readonly",
    aud:   "https://oauth2.googleapis.com/token",
    iat:   now,
    exp:   now + 3600,
  })).replace(/=/g, "");

  // Ký bằng private key — Deno hỗ trợ crypto.subtle với RSA-PSS / RSASSA-PKCS1-v1_5
  const pemKey = sa.private_key.replace(/\\n/g, "\n");
  const keyData = pemKey
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\n/g, "");
  const binaryKey = Uint8Array.from(atob(keyData), (c) => c.charCodeAt(0));
  const cryptoKey = await crypto.subtle.importKey(
    "pkcs8", binaryKey,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false, ["sign"]
  );
  const signed = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    cryptoKey,
    new TextEncoder().encode(`${header}.${payload}`)
  );
  const sig = btoa(String.fromCharCode(...new Uint8Array(signed))).replace(/=/g, "");
  const jwt = `${header}.${payload}.${sig}`;

  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion:  jwt,
    }),
  });
  const data = await res.json();
  if (!data.access_token) throw new Error(`GCal token error: ${JSON.stringify(data)}`);
  return data.access_token;
}

// ---------- Lấy events mới/thay đổi theo calendarId + syncToken ----------
async function fetchChangedEvents(
  token: string,
  calendarId: string,
  syncToken?: string
): Promise<{ events: Record<string, unknown>[]; nextSyncToken: string }> {
  let url = `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(calendarId)}/events?singleEvents=true&maxResults=50`;
  if (syncToken) {
    url += `&syncToken=${encodeURIComponent(syncToken)}`;
  } else {
    // Lần đầu: lấy từ hôm nay trở đi
    const since = new Date();
    since.setHours(0, 0, 0, 0);
    url += `&timeMin=${since.toISOString()}`;
  }

  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (res.status === 410) {
    // Sync token hết hạn: full sync lại
    console.warn("GCal syncToken expired, doing full sync for", calendarId);
    delete syncTokenStore[calendarId];
    return fetchChangedEvents(token, calendarId);
  }
  if (!res.ok) throw new Error(`GCal events error ${res.status}: ${await res.text()}`);
  const data = await res.json();
  return {
    events:        (data.items ?? []) as Record<string, unknown>[],
    nextSyncToken: data.nextSyncToken ?? "",
  };
}

// ---------- Convert GCal event → upsert params ----------
function gcalEventToParams(ev: Record<string, unknown>) {
  const start    = ev.start as Record<string, string> | undefined;
  const end      = ev.end   as Record<string, string> | undefined;
  const org      = ev.organizer as Record<string, string> | undefined;
  const isAllDay = Boolean(start?.date && !start?.dateTime);

  const attendees = ((ev.attendees as unknown[]) ?? []).map((a: unknown) => {
    const att = a as Record<string, string>;
    return {
      name:            att.displayName ?? "",
      email:           att.email ?? "",
      responseStatus:  att.responseStatus ?? "none",
      type:            att.optional ? "optional" : "required",
    };
  });

  // Lấy meeting URL từ conferenceData
  const cd  = ev.conferenceData as Record<string, unknown> | undefined;
  const eps = (cd?.entryPoints as Record<string, string>[] | undefined) ?? [];
  const videoEp = eps.find((e) => e.entryPointType === "video");
  const meetUrl = (ev.hangoutLink as string | undefined) ?? videoEp?.uri ?? null;

  return {
    p_source:           "google",
    p_external_id:      String(ev.id ?? ""),
    p_title:            String(ev.summary ?? "(Không tiêu đề)"),
    p_description:      (ev.description as string | undefined) ?? null,
    p_location:         (ev.location as string | undefined) ?? null,
    p_start_time:       isAllDay ? null : (start?.dateTime ?? null),
    p_end_time:         isAllDay ? null : (end?.dateTime ?? null),
    p_is_all_day:       isAllDay,
    p_timezone:         start?.timeZone ?? TZ,
    p_organizer_name:   org?.displayName ?? null,
    p_organizer_email:  org?.email ?? null,
    p_meeting_url:      meetUrl,
    p_meeting_platform: meetUrl ? (meetUrl.includes("meet.google") ? "meet" : "teams") : null,
    p_status:           ev.status === "cancelled" ? "cancelled" : "confirmed",
    p_is_recurring:     Boolean(ev.recurringEventId || ev.recurrence),
    p_attendees:        attendees,
  };
}

// ---------- Handler ----------
serve(async (req) => {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });

  // Verify webhook token
  const token = req.headers.get("X-Goog-Channel-Token");
  if (WEBHOOK_SECRET && token !== WEBHOOK_SECRET) {
    return new Response("Forbidden", { status: 403 });
  }

  const state      = req.headers.get("X-Goog-Resource-State") ?? "";
  const calendarId = req.headers.get("X-Goog-Resource-Id") ?? "";

  // Bỏ qua tin "sync" (confirmation khi đăng ký channel)
  if (state === "sync") return new Response("OK", { status: 200 });

  // Parse Service Account
  if (!SA_JSON_RAW) {
    console.error("Missing GOOGLE_SERVICE_ACCOUNT_JSON");
    return new Response("Config error", { status: 500 });
  }
  let sa: Record<string, string>;
  try {
    sa = JSON.parse(SA_JSON_RAW);
  } catch {
    console.error("Invalid GOOGLE_SERVICE_ACCOUNT_JSON");
    return new Response("Config error", { status: 500 });
  }

  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);

  try {
    const accessToken = await getGCalToken(sa);
    const { events, nextSyncToken } = await fetchChangedEvents(
      accessToken,
      calendarId,
      syncTokenStore[calendarId]
    );
    if (nextSyncToken) syncTokenStore[calendarId] = nextSyncToken;

    for (const ev of events) {
      const status = String(ev.status ?? "");
      if (status === "cancelled") {
        await supabase.rpc("cancel_calendar_event", {
          p_source: "google",
          p_external_id: String(ev.id ?? ""),
        });
        continue;
      }
      const params = gcalEventToParams(ev);
      const { error } = await supabase.rpc("upsert_calendar_event", params);
      if (error) console.error("upsert error:", ev.id, error);
    }
  } catch (err) {
    console.error("webhook-gcal error:", err);
    return new Response("Internal error", { status: 500 });
  }

  return new Response("OK", { status: 200 });
});
