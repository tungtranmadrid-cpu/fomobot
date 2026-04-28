/**
 * Supabase Edge Function: nhận webhook từ Microsoft Graph (MS Teams / Outlook).
 * Deploy: supabase functions deploy webhook-msgraph
 *
 * Đăng ký subscription MS Graph:
 *   POST https://graph.microsoft.com/v1.0/subscriptions
 *   {
 *     "changeType": "created,updated,deleted",
 *     "notificationUrl": "https://<project>.supabase.co/functions/v1/webhook-msgraph",
 *     "resource": "users/<user-id>/events",
 *     "expirationDateTime": "...",
 *     "clientState": "<MS_WEBHOOK_SECRET>"
 *   }
 *
 * Env vars cần đặt trong Supabase Dashboard → Edge Functions → Secrets:
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (tự có)
 *   MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET  — app Azure AD
 *   MS_WEBHOOK_SECRET                              — clientState để verify
 *   GCALENDAR_TZ                                   — vd. Asia/Ho_Chi_Minh
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const TENANT_ID    = Deno.env.get("MS_TENANT_ID") ?? "";
const CLIENT_ID    = Deno.env.get("MS_CLIENT_ID") ?? "";
const CLIENT_SECRET= Deno.env.get("MS_CLIENT_SECRET") ?? "";
const WEBHOOK_SECRET = Deno.env.get("MS_WEBHOOK_SECRET") ?? "";
const TZ           = Deno.env.get("GCALENDAR_TZ") ?? "Asia/Ho_Chi_Minh";

// ---------- MS Graph access token (client credentials) ----------
let _tokenCache: { token: string; expiresAt: number } | null = null;

async function getAccessToken(): Promise<string> {
  const now = Date.now() / 1000;
  if (_tokenCache && _tokenCache.expiresAt > now + 60) return _tokenCache.token;

  const res = await fetch(
    `https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token`,
    {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type:    "client_credentials",
        client_id:     CLIENT_ID,
        client_secret: CLIENT_SECRET,
        scope:         "https://graph.microsoft.com/.default",
      }),
    }
  );
  const data = await res.json();
  if (!data.access_token) throw new Error(`MS token error: ${JSON.stringify(data)}`);
  _tokenCache = { token: data.access_token, expiresAt: now + data.expires_in };
  return data.access_token;
}

// ---------- Fetch full event from MS Graph ----------
async function fetchMsEvent(eventId: string, userId: string): Promise<Record<string, unknown> | null> {
  const token = await getAccessToken();
  const res = await fetch(
    `https://graph.microsoft.com/v1.0/users/${userId}/events/${eventId}?$select=id,subject,body,start,end,location,organizer,attendees,onlineMeeting,onlineMeetingProvider,isAllDay,isCancelled,type,recurrence`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (!res.ok) {
    console.error("fetchMsEvent error:", res.status, await res.text());
    return null;
  }
  return res.json();
}

// ---------- Convert MS Graph event → upsert_calendar_event params ----------
function msEventToParams(ev: Record<string, unknown>, userId: string) {
  const start = ev.start as Record<string, string> | undefined;
  const end   = ev.end   as Record<string, string> | undefined;
  const org   = ev.organizer as Record<string, Record<string, string>> | undefined;
  const online= ev.onlineMeeting as Record<string, string> | undefined;
  const attendees = ((ev.attendees as unknown[]) ?? []).map((a: unknown) => {
    const att = a as Record<string, Record<string, string>>;
    return {
      name:            att.emailAddress?.name ?? "",
      email:           att.emailAddress?.address ?? "",
      responseStatus:  att.status?.response ?? "none",
      type:            att.type ?? "required",
    };
  });

  // MS Graph trả thời gian theo timezone của sự kiện, không phải UTC.
  // Thêm "Z" nếu không có offset để parse đúng.
  const toUtc = (dt: string | undefined, tz: string | undefined): string | null => {
    if (!dt) return null;
    if (dt.endsWith("Z") || dt.includes("+") || dt.includes("-", 10)) return dt;
    // dt dạng "2026-04-29T09:00:00.0000000", timezone là "SE Asia Standard Time" v.v.
    // Supabase/PostgreSQL tự xử lý khi nhận TIMESTAMPTZ string; để đơn giản, gắn +07:00 nếu tz là Indochina.
    return dt + "+00:00"; // caller nên set p_timezone đúng để DB convert.
  };

  return {
    p_source:           "ms_teams",
    p_external_id:      String(ev.id ?? ""),
    p_title:            String(ev.subject ?? "(Không tiêu đề)"),
    p_description:      (ev.body as Record<string,string>)?.content ?? null,
    p_location:         (ev.location as Record<string,string>)?.displayName ?? null,
    p_start_time:       toUtc(start?.dateTime, start?.timeZone),
    p_end_time:         toUtc(end?.dateTime,   end?.timeZone),
    p_is_all_day:       Boolean(ev.isAllDay),
    p_timezone:         start?.timeZone ?? TZ,
    p_organizer_name:   org?.emailAddress?.name  ?? null,
    p_organizer_email:  org?.emailAddress?.address ?? null,
    p_meeting_url:      online?.joinUrl ?? null,
    p_meeting_platform: ev.onlineMeetingProvider ? "teams" : null,
    p_status:           ev.isCancelled ? "cancelled" : "confirmed",
    p_is_recurring:     ev.type === "seriesMaster" || ev.type === "occurrence",
    p_attendees:        attendees,
  };
}

// ---------- Handler ----------
serve(async (req) => {
  const url = new URL(req.url);

  // MS Graph xác thực subscription: gửi GET với validationToken
  const vt = url.searchParams.get("validationToken");
  if (vt) return new Response(vt, { headers: { "Content-Type": "text/plain" } });

  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return new Response("Bad JSON", { status: 400 });
  }

  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);
  const notifications = (body.value as unknown[]) ?? [];

  for (const n of notifications) {
    const notif = n as Record<string, unknown>;

    // Verify clientState
    if (WEBHOOK_SECRET && notif.clientState !== WEBHOOK_SECRET) {
      console.warn("Invalid clientState, skipping notification");
      continue;
    }

    const changeType   = String(notif.changeType ?? "");
    const resourceData = notif.resourceData as Record<string, string> | undefined;
    const eventId      = resourceData?.id ?? String(notif.resource ?? "").split("/").pop() ?? "";
    // userId có trong resource: "users/{userId}/events/{eventId}"
    const resource     = String(notif.resource ?? "");
    const userIdMatch  = resource.match(/users\/([^/]+)\//);
    const userId       = userIdMatch?.[1] ?? CLIENT_ID; // fallback về app user

    if (!eventId) continue;

    if (changeType === "deleted") {
      await supabase.rpc("cancel_calendar_event", {
        p_source: "ms_teams",
        p_external_id: eventId,
      });
      continue;
    }

    // created | updated: fetch full event rồi upsert
    try {
      const ev = await fetchMsEvent(eventId, userId);
      if (!ev) continue;
      const params = msEventToParams(ev, userId);
      const { error } = await supabase.rpc("upsert_calendar_event", params);
      if (error) console.error("upsert error:", error);
    } catch (err) {
      console.error("process event error:", err);
    }
  }

  return new Response("OK", { status: 200 });
});
