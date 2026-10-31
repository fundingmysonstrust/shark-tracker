/**
 * Shark Tracker website (Cloudflare Worker).
 *
 * Jobs:
 *   - Password gate: every page and API call needs the login cookie.
 *   - Serves the single-page site (index.html) and the latest data.
 *   - Saves Settings and "same market / not the same" decisions in KV.
 *   - Two endpoints for the collector (GitHub Actions), protected by a
 *     separate secret key (INGEST_KEY):
 *       GET  /api/collector/input   -> settings, decisions
 *       GET/PUT /api/collector/state -> saved state (gzip JSON)
 *       PUT  /api/collector/data    -> new website data (gzip JSON)
 *       POST /api/collector/report  -> ok / failure note for the run log
 *   - If a refresh fails, the last good data stays and the failure is noted.
 *
 * Secrets (set with `wrangler secret put`): SITE_PASSWORD, INGEST_KEY.
 * KV namespace binding: STORE.
 */
import INDEX_HTML from "./index.html";
import LOGIN_HTML from "./login.html";

const COOKIE = "st_auth";
const RUN_LOG_MAX = 60;

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function authToken(env) {
  return sha256("shark-tracker-cookie|" + env.SITE_PASSWORD);
}

function getCookie(req, name) {
  const h = req.headers.get("Cookie") || "";
  for (const part of h.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

async function isAuthed(req, env) {
  return timingSafeEqual(getCookie(req, COOKIE) || "", await authToken(env));
}

function isCollector(req, env) {
  const h = req.headers.get("Authorization") || "";
  return !!env.INGEST_KEY && timingSafeEqual(h, "Bearer " + env.INGEST_KEY);
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });

const html = (body, status = 200) =>
  new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "no-referrer",
    },
  });

async function readJSON(env, key, fallback) {
  const v = await env.STORE.get(key);
  if (!v) return fallback;
  try { return JSON.parse(v); } catch { return fallback; }
}

async function handleLogin(req, env) {
  const form = await req.formData();
  const pw = String(form.get("password") || "").trim();
  if (!timingSafeEqual(pw, env.SITE_PASSWORD || "\u0000")) {
    await new Promise((r) => setTimeout(r, 800)); // slow down guessing
    return html(LOGIN_HTML.replace("<!--ERR-->", '<p class="err">Wrong password. Try again.</p>'), 401);
  }
  return new Response(null, {
    status: 303,
    headers: {
      Location: "/",
      "Set-Cookie": `${COOKIE}=${await authToken(env)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${90 * 86400}`,
    },
  });
}

/**
 * Large blobs (data, state) arrive gzip-compressed from the collector and
 * are stored and served as-is, never unpacked here: the free plan only
 * allows a few milliseconds of computing per request.
 */
async function putBlob(req, env, key) {
  const buf = await req.arrayBuffer();
  if (buf.byteLength < 2) return json({ error: "empty" }, 400);
  await env.STORE.put(key, buf);
  return json({ saved: true, bytes: buf.byteLength });
}

function blobResponse(buf) {
  if (!buf) return new Response("null", { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
  return new Response(buf, {
    encodeBody: "manual",
    headers: { "Content-Type": "application/json; charset=utf-8", "Content-Encoding": "gzip", "Cache-Control": "no-store" },
  });
}

/** Small end-of-run report: ok/failed + message, kept in a run log. */
async function handleReport(req, env) {
  const body = await req.json();
  const now = Math.floor(Date.now() / 1000);
  const meta = await readJSON(env, "meta", { runs: [] });
  const entry = { at: now, ok: !!body.ok, seconds: body.seconds ?? null,
                  trigger: body.trigger || null, message: String(body.message || "").slice(0, 300) };
  meta.runs = [entry, ...(meta.runs || [])].slice(0, RUN_LOG_MAX);
  if (body.ok) meta.last_success = now;
  else { meta.last_failure = now; meta.last_error = entry.message; }
  await env.STORE.put("meta", JSON.stringify(meta));
  return json({ saved: true });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    // --- collector (machine) endpoints -----------------------------------
    if (p.startsWith("/api/collector/")) {
      if (!isCollector(req, env)) return json({ error: "unauthorized" }, 401);
      if (p === "/api/collector/input" && req.method === "GET") {
        const [settings, decisions] = await Promise.all([readJSON(env, "settings", {}), readJSON(env, "decisions", {})]);
        return json({ settings, decisions });
      }
      if (p === "/api/collector/state" && req.method === "GET")
        return blobResponse(await env.STORE.get("state_gz", "arrayBuffer"));
      if (p === "/api/collector/state" && req.method === "PUT") return putBlob(req, env, "state_gz");
      if (p === "/api/collector/data" && req.method === "PUT") return putBlob(req, env, "data_gz");
      if (p === "/api/collector/report" && req.method === "POST") return handleReport(req, env);
      return json({ error: "not found" }, 404);
    }

    // --- login ------------------------------------------------------------
    if (p === "/login" && req.method === "POST") return handleLogin(req, env);
    if (p === "/logout") {
      return new Response(null, { status: 303, headers: {
        Location: "/", "Set-Cookie": `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0` } });
    }
    if (!(await isAuthed(req, env))) {
      if (p.startsWith("/api/")) return json({ error: "login required" }, 401);
      return html(LOGIN_HTML.replace("<!--ERR-->", ""));
    }

    // --- signed-in user -----------------------------------------------------
    if (p === "/api/data") return blobResponse(await env.STORE.get("data_gz", "arrayBuffer"));
    if (p === "/api/meta") return json(await readJSON(env, "meta", {}));
    if (p === "/api/settings") {
      if (req.method === "POST") {
        const s = await req.json();
        if (typeof s !== "object" || s === null || Array.isArray(s)) return json({ error: "bad" }, 400);
        await env.STORE.put("settings", JSON.stringify(s).slice(0, 20000));
        return json({ saved: true });
      }
      return json(await readJSON(env, "settings", {}));
    }
    if (p === "/api/decisions") {
      if (req.method === "POST") {
        const { pair, decision } = await req.json();
        if (typeof pair !== "string" || !["same", "not_same", "clear"].includes(decision))
          return json({ error: "bad" }, 400);
        const d = await readJSON(env, "decisions", {});
        if (decision === "clear") delete d[pair]; else d[pair] = decision;
        await env.STORE.put("decisions", JSON.stringify(d));
        return json({ saved: true });
      }
      return json(await readJSON(env, "decisions", {}));
    }
    if (p === "/" || p === "/index.html") return html(INDEX_HTML);
    return html("Not found", 404);
  },

  // Optional: Cloudflare's clock asks GitHub to start a refresh on time
  // (GitHub's own timer can run late). Only active if GH_DISPATCH_TOKEN is set.
  async scheduled(event, env, ctx) {
    if (!env.GH_DISPATCH_TOKEN || !env.GH_REPO) return;
    ctx.waitUntil(fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/refresh.yml/dispatches`, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`, Accept: "application/vnd.github+json",
                 "User-Agent": "shark-tracker-worker" },
      body: JSON.stringify({ ref: "main", inputs: { trigger: "cloudflare-cron" } }),
    }));
  },
};
