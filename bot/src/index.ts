/**
 * The Worker's entry point: Telegram's webhook.
 *
 * Here the bindings (Workers AI, Vectorize, D1) are connected to the RAG services (`rag.ts`). It
 * answers 200 to Telegram at once and works in `waitUntil`: so Telegram does not repeat the request
 * if the LLM is slow (duplicates cannot be filtered: no `update_id` is saved).
 */

import {
  type Data,
  type Event,
  errorCause,
  errorType,
  handle,
  llmRequest,
  llmText,
  logEvent,
  type Notice,
  authorised,
  SECRET_HEADER,
  type Update,
  WEBHOOK_PATH,
} from "./bot.ts";
import type { Copy, Found, Services, Table } from "./rag.ts";

interface Statement {
  bind(...values: unknown[]): Statement;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
}

export interface Env {
  AI: { run(model: string, data: Record<string, unknown>): Promise<unknown> };
  VECTORIZE: { query(vector: number[], options: Record<string, unknown>): Promise<{ matches?: Found[] }> };
  DB: { prepare(sql: string): Statement; batch(statements: Statement[]): Promise<{ results: unknown[] }[]> };
  CF_EMBED_MODEL: string;
  CF_LLM_MODEL: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
}

/** Error of a Cloudflare service: only the service's name, never data. */
export class ServiceError extends Error {
  constructor(service: string) {
    super(service);
    this.name = `${service}Error`;
  }
}

export const RETRY_WAIT_MS = 1500;

/** Workers AI with a second try if the model is overloaded (code 3040): waiting costs no CPU. The
 * other errors (quota used up included) go through at once. */
async function runAI(env: Env, model: string, data: Record<string, unknown>): Promise<unknown> {
  try {
    return await env.AI.run(model, data);
  } catch (error) {
    if (errorCause(error) !== "overloaded") throw error;
    await new Promise((done) => setTimeout(done, RETRY_WAIT_MS));
    return env.AI.run(model, data);
  }
}

function placeholders(n: number): string {
  return Array.from({ length: n }, () => "?").join(", ");
}

/** The RAG services on the bindings: Workers AI, Vectorize, D1. */
export function cloudflareServices(env: Env): Services {
  return {
    async embedding(text) {
      const r = (await runAI(env, env.CF_EMBED_MODEL, { text: [text] })) as { data?: number[][] };
      const vector = r.data?.[0];
      if (!Array.isArray(vector)) throw new ServiceError("Embedding");
      return vector;
    },
    async search(vector, topK, filter) {
      const r = await env.VECTORIZE.query(vector, { topK, returnMetadata: "all", filter });
      return r.matches ?? [];
    },
    async tables(ids) {
      const sql = `select id, context, markdown from tables where id in (${placeholders(ids.length)})`;
      const { results } = await env.DB.prepare(sql).bind(...ids).all<Table & { id: string }>();
      return Object.fromEntries(results.map((r) => [r.id, { context: r.context, markdown: r.markdown }]));
    },
    async acronyms() {
      // The same code in two timetables (two years or semesters): the first name in code order
      const sql =
        "select a.code, a.name from acronyms a join sources s on s.code = a.source " +
        "where s.absent_since is null order by a.code, a.source";
      const codes: Record<string, string> = {};
      try {
        const { results } = await env.DB.prepare(sql).all<{ code: string; name: string }>();
        for (const r of results) codes[r.code] ??= r.name;
      } catch (error) {
        // Without the codes the question is searched as written: never a lost answer
        logEvent("acronyms_error", { type: errorType(error) });
      }
      return codes;
    },
    async sources(codes, chunks) {
      const titles = `select code, title, absent_since from sources where code in (${placeholders(codes.length)})`;
      const copies =
        "select c.chunk, s.code, s.title, s.url, s.year from copies c join sources s on s.code = c.also_for " +
        `where s.absent_since is null and c.chunk in (${placeholders(chunks.length)})`;
      // One request to D1 for both reads
      const [t, c] = await env.DB.batch([env.DB.prepare(titles).bind(...codes), env.DB.prepare(copies).bind(...chunks)]);
      const titleRows = t.results as { code: string; title: string; absent_since: number | null }[];
      const copyRows = c.results as (Copy & { chunk: string })[];
      return {
        titles: Object.fromEntries(titleRows.map((r) => [r.code, r.title])),
        copies: Object.fromEntries(copyRows.map(({ chunk: id, ...copy }) => [id, copy])),
        absent: titleRows.filter((r) => r.absent_since != null).map((r) => r.code),
      };
    },
    async answer(messages, options) {
      return llmText(await runAI(env, env.CF_LLM_MODEL, llmRequest(messages, options)));
    },
  };
}

interface Context {
  waitUntil(promise: Promise<unknown>): void;
}

const TELEGRAM_API = "https://api.telegram.org";

/** Telegram refused the message. It holds only the HTTP status. */
export class TelegramError extends Error {
  constructor(status: number) {
    super(`HTTP ${status}`);
    this.name = "TelegramError";
  }
}

/** The D1 reads for the commands. */
export function d1Data(env: Env): Data {
  return {
    async notices(now, howMany) {
      const sql =
        "select n.title, n.url, n.published_at, n.deadline from notices n join sources s on s.code = n.code " +
        "where n.valid_until >= ? and s.absent_since is null order by n.published_at desc limit ?";
      return (await env.DB.prepare(sql).bind(now, howMany).all<Notice>()).results;
    },
    async updates(since, howMany) {
      const sql =
        "select title, url, added_at, changed_at from sources " +
        "where absent_since is null and (added_at >= ? or changed_at >= ?) " +
        "order by max(coalesce(added_at, 0), coalesce(changed_at, 0)) desc limit ?";
      return (await env.DB.prepare(sql).bind(since, since, howMany).all<Update>()).results;
    },
    async events(howMany) {
      const sql =
        "select e.title, e.date_text, e.credits, e.url from events e join sources s on s.code = e.source " +
        "where s.absent_since is null order by e.source, e.position limit ?";
      return (await env.DB.prepare(sql).bind(howMany).all<Event>()).results;
    },
    async sourcesState() {
      // One request to D1 for both reads
      const [kinds, run] = await env.DB.batch([
        env.DB.prepare("select kind, count(*) as n from sources where absent_since is null group by kind"),
        env.DB.prepare("select max(ended_at) as ended_at from runs where outcome = 'ok'"),
      ]);
      const kindRows = kinds.results as { kind: string; n: number }[];
      const runRows = run.results as { ended_at: number | null }[];
      return {
        byKind: Object.fromEntries(kindRows.map((r) => [r.kind, r.n])),
        lastRun: runRows[0]?.ended_at ?? null,
      };
    },
  };
}

async function telegram(env: Env, method: string, body: Record<string, unknown>): Promise<void> {
  const response = await fetch(`${TELEGRAM_API}/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new TelegramError(response.status);
}

function send(env: Env, chatId: number, text: string): Promise<void> {
  return telegram(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: true },
  });
}

async function work(update: unknown, env: Env): Promise<void> {
  try {
    await handle(update, {
      services: cloudflareServices(env),
      data: d1Data(env),
      send: (chatId, text) => send(env, chatId, text),
      typing: (chatId) => telegram(env, "sendChatAction", { chat_id: chatId, action: "typing" }),
    });
  } catch (error) {
    logEvent("bot_error", { type: errorType(error) });
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: Context): Promise<Response> {
    if (request.method !== "POST" || new URL(request.url).pathname !== WEBHOOK_PATH) {
      return new Response(null, { status: 404 });
    }
    const secret = request.headers.get(SECRET_HEADER);
    if (!authorised(secret, env.TELEGRAM_WEBHOOK_SECRET ?? "")) {
      return new Response(null, { status: 403 });
    }
    let update: unknown;
    try {
      update = await request.json();
    } catch {
      return new Response(null, { status: 400 });
    }
    ctx.waitUntil(work(update, env));
    return new Response("ok");
  },
};
