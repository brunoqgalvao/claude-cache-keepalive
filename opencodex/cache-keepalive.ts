/**
 * Prompt-cache keep-alive for Anthropic OAuth conversations in opencodex.
 * https://github.com/brunoqgalvao/claude-cache-keepalive
 *
 * With cacheRetention "long", Anthropic cache entries live 1 hour after their last use. When a
 * conversation goes idle, the next turn after that hour rewrites the whole context at 2x input.
 * Shortly before expiry this module re-sends the conversation's last request and aborts at
 * message_start. That reads the cached prefix (0.05x on Opus 5.5) and restarts its 1-hour clock.
 *
 * Policy comes from ~/.opencodex/cache-keepalive/policy.json, written by tune.py from your own
 * usage log. monitor.py re-tunes daily and creates the file "disabled" if keep-alives stop paying.
 * Every decision is logged to keepalive.jsonl. Disable at runtime with OCX_CACHE_KEEPALIVE=0.
 */
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import { getConfigDir } from "../config/paths";
import { loadAuthStore } from "../oauth/store";

type Hazard = { size_edges: number[]; step_s: number; p: number[][] };

type Policy = {
  mode: "hazard" | "rule";
  max_idle_s: number;
  min_prompt_tokens: number;
  daily_cap: number;
  shared_floor_tokens: number;
  hazard?: Hazard;
};

const READ_MULT: Record<string, number> = { "claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05, "claude-fable-5-1": 0.025 };
const readMult = (model: string) => READ_MULT[model] ?? 0.1;

type Conv = {
  key: string;
  model: string;
  url: string;
  headers: Record<string, string>;
  bodyGz?: Buffer;
  token: string;
  tokenExpires?: number;
  accountId?: string;
  lastSentAt: number;
  lastDoneAt: number;
  prompt: number;
  out: number;
  timer?: ReturnType<typeof setTimeout>;
  pings: number;
};

export type KeepaliveCapture = {
  key: string;
  model: string;
  url: string;
  headers: Record<string, string>;
  body: string;
  token: string;
  sentAt: number;
  done?: boolean;
};

const DEFAULT_POLICY: Policy = { mode: "rule", max_idle_s: 4 * 3600, min_prompt_tokens: 40_000, daily_cap: 400, shared_floor_tokens: 30_000 };
const MAX_CONVS = 300;
const LEAD_S = 60;
const TTL_S = 3600;
const convs = new Map<string, Conv>();
let policyCache: { mtime: number; policy: Policy } | undefined;
let today = { day: "", pings: 0 };

function dir(): string {
  const d = join(getConfigDir(), "cache-keepalive");
  if (!existsSync(d)) mkdirSync(d, { recursive: true, mode: 0o700 });
  return d;
}

function disabled(): boolean {
  return process.env.OCX_CACHE_KEEPALIVE === "0" || existsSync(join(dir(), "disabled"));
}

function log(event: Record<string, unknown>): void {
  try {
    appendFileSync(join(dir(), "keepalive.jsonl"), JSON.stringify({ ts: Date.now(), ...event }) + "\n", { mode: 0o600 });
  } catch { /* logging never breaks a request */ }
}

function policy(): Policy {
  try {
    const path = join(dir(), "policy.json");
    const mtime = statSync(path).mtimeMs;
    if (policyCache?.mtime === mtime) return policyCache.policy;
    const p = JSON.parse(readFileSync(path, "utf8")) as Partial<Policy>;
    const num = (v: unknown, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
    const hz = p.hazard;
    const hazardOk = !!hz && Array.isArray(hz.size_edges) && Array.isArray(hz.p) && hz.p.length === hz.size_edges.length;
    const merged: Policy = {
      mode: p.mode === "hazard" && hazardOk ? "hazard" : "rule",
      max_idle_s: num(p.max_idle_s, DEFAULT_POLICY.max_idle_s),
      min_prompt_tokens: num(p.min_prompt_tokens, DEFAULT_POLICY.min_prompt_tokens),
      daily_cap: num(p.daily_cap, DEFAULT_POLICY.daily_cap),
      shared_floor_tokens: num(p.shared_floor_tokens, DEFAULT_POLICY.shared_floor_tokens),
      hazard: hazardOk ? hz : undefined,
    };
    policyCache = { mtime, policy: merged };
    return merged;
  } catch {
    return DEFAULT_POLICY;
  }
}

/** Chance of a return within the next hour (from your history) vs the chance needed for a re-read to pay. */
function decide(c: Conv, p: Policy, idleS: number): { ok: boolean; reason: string; p_return?: number; p_needed?: number } {
  const r = readMult(c.model);
  const reuse = Math.max(0, 0.97 * (c.prompt + c.out) - p.shared_floor_tokens);
  const cost = r * (c.prompt + c.out) + 2 * 30 + 5 * 5;
  const needed = reuse > 0 ? cost / ((2 - r) * reuse) : 1;
  if (p.mode !== "hazard" || !p.hazard) {
    const ok = idleS + TTL_S <= p.max_idle_s + 60 && c.prompt >= p.min_prompt_tokens;
    return { ok, reason: ok ? "rule" : "max_idle", p_needed: round4(needed) };
  }
  const hz = p.hazard;
  let b = -1;
  for (let i = 0; i < hz.size_edges.length; i++) if (c.prompt >= hz.size_edges[i]!) b = i;
  if (b < 0) return { ok: false, reason: "too_small", p_needed: round4(needed) };
  const row = hz.p[b]!;
  const k = Math.max(1, Math.min(row.length, Math.round(idleS / hz.step_s)));
  const pr = row[k - 1] ?? 0;
  const ok = pr > needed && idleS + TTL_S <= p.max_idle_s + 60;
  return { ok, reason: ok ? "worth_it" : (pr > needed ? "max_idle" : "not_worth_it"), p_return: round4(pr), p_needed: round4(needed) };
}

function round4(x: number): number { return Math.round(x * 1e4) / 1e4; }

function underDailyCap(p: Policy): boolean {
  const day = new Date().toISOString().slice(0, 10);
  if (today.day !== day) today = { day, pings: 0 };
  return today.pings < p.daily_cap;
}

/** Conversation identity: model + Codex thread id + system + the first two messages, which stay fixed as it grows.
 *  The thread id matters: many Codex chats open with the same instructions, so without it different
 *  chats shared a key and the one that finished last replaced the others, which were then never kept
 *  alive. A compaction inside one chat changes the opening, so it still gets its own key. */
export function keepaliveKey(model: string, body: Record<string, unknown>, thread?: string): string | undefined {
  const messages = Array.isArray(body.messages) ? body.messages : [];
  if (messages.length === 0) return undefined;
  const parts = thread ? [model, thread, body.system ?? null, messages.slice(0, 2)] : [model, body.system ?? null, messages.slice(0, 2)];
  return createHash("sha256").update(JSON.stringify(parts)).digest("hex").slice(0, 32);
}

/** Called by the Anthropic adapter right before a request is sent. */
export function keepaliveCapture(input: {
  retention: string | undefined;
  model: string;
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
  serialized: string;
  token: string | undefined;
  thread?: string;
}): KeepaliveCapture | undefined {
  try {
    if (disabled() || input.retention !== "long" || !input.token) return undefined;
    const key = keepaliveKey(input.model, input.body, typeof input.thread === "string" && input.thread ? input.thread : undefined);
    if (!key) return undefined;
    const existing = convs.get(key);
    if (existing?.timer) { clearTimeout(existing.timer); existing.timer = undefined; }
    return { key, model: input.model, url: input.url, headers: { ...input.headers }, body: input.serialized, token: input.token, sentAt: Date.now() };
  } catch {
    return undefined;
  }
}

function accountFor(token: string): { id?: string; expires?: number } {
  try {
    for (const set of Object.values(loadAuthStore())) {
      for (const account of set?.accounts ?? []) {
        if (account.credential?.access === token) return { id: account.id, expires: account.credential.expires };
      }
    }
  } catch { /* fall through */ }
  return {};
}

function currentToken(c: Conv): string | undefined {
  const now = Date.now();
  if (c.tokenExpires === undefined || c.tokenExpires > now + 120_000) return c.token;
  if (!c.accountId) return undefined;
  try {
    for (const set of Object.values(loadAuthStore())) {
      const account = set?.accounts?.find(a => a.id === c.accountId);
      const cred = account?.credential;
      if (cred?.access && cred.expires > now + 120_000 && !account?.paused && !account?.needsReauth) {
        c.token = cred.access;
        c.tokenExpires = cred.expires;
        return cred.access;
      }
    }
  } catch { /* fall through */ }
  return undefined;
}

function schedule(c: Conv): void {
  if (c.timer) clearTimeout(c.timer);
  const delay = Math.max(1_000, c.lastSentAt + (TTL_S - LEAD_S) * 1000 - Date.now());
  c.timer = setTimeout(() => { void fire(c.key); }, delay);
  (c.timer as { unref?: () => void }).unref?.();
}

function evict(): void {
  if (convs.size <= MAX_CONVS) return;
  const oldest = [...convs.values()].sort((a, b) => a.lastDoneAt - b.lastDoneAt).slice(0, convs.size - MAX_CONVS);
  for (const c of oldest) { if (c.timer) clearTimeout(c.timer); convs.delete(c.key); }
}

/** Called by the Anthropic adapter when a streamed response completes. */
export function keepaliveDone(capture: KeepaliveCapture | undefined, usage: unknown, _stopReason?: string): void {
  try {
    if (!capture || capture.done) return;
    capture.done = true;
    const u = (usage && typeof usage === "object" ? usage : {}) as Record<string, unknown>;
    const n = (k: string) => (typeof u[k] === "number" && Number.isFinite(u[k]) ? (u[k] as number) : 0);
    const prompt = n("input_tokens") + n("cache_read_input_tokens") + n("cache_creation_input_tokens");
    const prev = convs.get(capture.key);
    if (prev?.timer) clearTimeout(prev.timer);
    if (prompt < policy().min_prompt_tokens) { convs.delete(capture.key); return; }
    const account = prev && prev.token === capture.token ? { id: prev.accountId, expires: prev.tokenExpires } : accountFor(capture.token);
    const c: Conv = {
      key: capture.key, model: capture.model, url: capture.url, headers: capture.headers,
      bodyGz: gzipSync(Buffer.from(capture.body)), token: capture.token,
      tokenExpires: account.expires, accountId: account.id,
      lastSentAt: capture.sentAt, lastDoneAt: Date.now(), prompt, out: n("output_tokens"), pings: 0,
    };
    convs.set(c.key, c);
    schedule(c);
    evict();
  } catch { /* never break the response path */ }
}

async function fire(key: string): Promise<void> {
  const c = convs.get(key);
  if (!c) return;
  c.timer = undefined;
  const p = policy();
  const idleS = (Date.now() - c.lastDoneAt) / 1000;
  const verdict = decide(c, p, idleS);
  const base = { v: 2, mode: p.mode, conv: key, model: c.model, prompt: c.prompt, idle_s: Math.round(idleS), pings_before: c.pings, p_return: verdict.p_return, p_needed: verdict.p_needed };
  const stop = (reason: string) => { log({ event: "stop", reason, ...base }); convs.delete(key); };
  if (disabled()) return stop("disabled");
  if (!verdict.ok) return stop(verdict.reason);
  if (!underDailyCap(p)) return stop("daily_cap");
  const token = currentToken(c);
  if (!token || !c.bodyGz) return stop("no_valid_token");
  const started = Date.now();
  const result = await ping(c, token);
  today.pings++;
  log({ event: "ping", reason: verdict.reason, ms: Date.now() - started, ...base, ...result });
  if (result.status !== 200 || typeof result.read !== "number") return void convs.delete(key);
  // If the conversation's opening changed upstream, the old prefix no longer matches what is sent.
  if ((result.read as number) < 0.5 * c.prompt) return stop("cache_changed");
  c.pings++;
  c.lastSentAt = started;
  if (convs.get(key) === c) schedule(c);
}

async function ping(c: Conv, token: string): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90_000);
  try {
    const body = JSON.parse(gunzipSync(c.bodyGz!).toString("utf8")) as Record<string, unknown>;
    body.stream = true;
    const thinking = body.thinking as { type?: string; budget_tokens?: number } | undefined;
    body.max_tokens = thinking?.type === "enabled" && typeof thinking.budget_tokens === "number" ? thinking.budget_tokens + 64 : 16;
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(c.headers)) {
      const lower = k.toLowerCase();
      if (lower === "content-length") continue;
      if (lower === "authorization") headers[k] = `Bearer ${token}`;
      else if (lower === "x-client-request-id") headers[k] = crypto.randomUUID();
      else headers[k] = v;
    }
    headers["Accept"] = "text/event-stream";
    const res = await fetch(c.url, { method: "POST", headers, body: JSON.stringify(body), signal: controller.signal });
    if (!res.ok || !res.body) {
      return { status: res.status, error: (await res.text().catch(() => "")).slice(0, 200) };
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let seen = 0;
    while (seen < 200_000) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      seen += chunk.length;
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        let frame: { type?: string; message?: { usage?: Record<string, unknown> } };
        try { frame = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (frame.type !== "message_start") continue;
        controller.abort();
        const usage = frame.message?.usage ?? {};
        const num = (k: string) => (typeof usage[k] === "number" ? (usage[k] as number) : 0);
        return { status: 200, read: num("cache_read_input_tokens"), write: num("cache_creation_input_tokens"), input: num("input_tokens") };
      }
    }
    controller.abort();
    return { status: 200, error: "no_message_start" };
  } catch (error) {
    return { status: 0, error: error instanceof Error ? `${error.name}: ${error.message.slice(0, 120)}` : "error" };
  } finally {
    clearTimeout(timeout);
  }
}
