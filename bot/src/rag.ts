/**
 * The bot's RAG: question → embedding → Vectorize → tables and titles from D1 → LLM → answer and sources.
 *
 * Same logic as `rag_local/rag.py` (used by `rag_local/ask.py`): the prompt is in `prompt.json`, read
 * by both.
 *
 * No logs in here: question, excerpts and answer stay in memory only for the answer.
 */

import PROMPT from "./prompt.json" with { type: "json" };

export const EXCERPTS = 8; // chunks searched in Vectorize (8 better than 6)
export const MAX_ANSWER_TOKENS = 600;
/** Telegram's "Reply": the follow-up question is rewritten by the LLM so that it stands alone, then
 * searched and answered like any other question (threshold included). As in `rag_local/rag.py`. */
export const MAX_REWRITE_TOKENS = 80;
export const MAX_REWRITTEN = 500; // characters: a longer rewritten question is a model mistake
/** Below this score of the first result the question is off topic and gets `PROMPT.off_topic` without
 * the LLM. 0 = off. Measured on test questions (see `rag_local/rag.py`). Same as `rag_local/rag.py`. */
export const RELEVANCE_THRESHOLD = 0.4;
export const OFF_TOPIC_TEXT: string = PROMPT.off_topic;
export const SYSTEM: string = PROMPT.system;

type Obj = Record<string, unknown>;

/** A chunk found by Vectorize (`returnMetadata: "all"`). */
export interface Found {
  id: string;
  score?: number;
  metadata?: Obj;
}

/** A row of the `tables` table in D1. */
export interface Table {
  context: string;
  markdown: string;
}

/** A block for the LLM: the text of a chunk or, if it comes from a table, the whole table. */
export interface Excerpt {
  number: number;
  page: string;
  url: string;
  year: string;
  text: string;
  title: string;
  chunks: string[];
  score: number;
  /** The same text holds for the other cohort (`copies` table in D1, e.g. identical articles in the
   * 2025/26 and 2026/27 regulations): its years in the heading and, if cited, its source. */
  copies: Excerpt[];
}

/** The other document with the same text of a chunk (row of `copies` joined with `sources` in D1). */
export interface Copy {
  code: string;
  title: string;
  url: string;
  year: string;
}

const ofOneCohort = (year: string): boolean => year !== "" && year !== "all";

/** The excerpt's year, or the years of all the cohorts with the same text. */
export function excerptYears(e: Excerpt): string {
  return [...new Set([e.year, ...e.copies.map((c) => c.year)])].sort().join(" e ");
}

// --- Years ---

const HOUR_MS = 3_600_000;

/** The last Sunday of the month (0 = January) at 01:00 UTC, in milliseconds. */
function lastSundayAt1Utc(year: number, month: number): number {
  const last = new Date(Date.UTC(year, month + 1, 0)); // the last day of the month
  return Date.UTC(year, month, last.getUTCDate() - last.getUTCDay(), 1);
}

/**
 * Hours between Rome and UTC: 2 with summer time, 1 without. EU rule: summer time goes from the
 * last Sunday of March to the last Sunday of October, at 01:00 UTC.
 *
 * Computed by hand and not with Intl's DateTimeFormat: in the Worker the first use of Intl with a
 * time zone loads the zone table and cost several milliseconds of CPU on every new instance
 * (median CPU 8.65 ms against the 10 ms limit). The tests compare this function with `Intl`.
 */
export function hoursFromUtcToRome(moment: Date): number {
  const year = moment.getUTCFullYear();
  const t = moment.getTime();
  return t >= lastSundayAt1Utc(year, 2) && t < lastSundayAt1Utc(year, 9) ? 2 : 1;
}

/** Today's date in Rome: [year, month, day]. */
export function todayInRome(now: Date): [number, number, number] {
  const rome = new Date(now.getTime() + hoursFromUtcToRome(now) * HOUR_MS);
  return [rome.getUTCFullYear(), rome.getUTCMonth() + 1, rome.getUTCDate()];
}

/** Today's academic year: from September the one starting (October 2026 → 2026/2027). */
export function currentYear([year, month]: [number, number, number]): string {
  const start = month >= 9 ? year : year - 1;
  return `${start}/${start + 1}`;
}

export function previousYear(year: string): string {
  const start = Number(year.slice(0, 4)) - 1;
  return `${start}/${start + 1}`;
}

/** The two running cohorts: the bot does not ask the enrolment year and searches both. */
export function filterYears(current: string): string[] {
  return [previousYear(current), current];
}

/** Only chunks of these years or valid for all, and not expired (notices). */
export function searchFilter(years: string[], now: number): Obj {
  return { year: { $in: [...years, "all"] }, valid_until: { $gte: now } };
}

// --- Excerpts and messages ---

/**
 * From the chunks found to numbered excerpts, in order of similarity. A chunk from a table is
 * replaced by the whole table (from D1), once.
 */
export function excerpts(
  found: Found[],
  tables: Record<string, Table>,
  titles: Record<string, string>,
  copies: Record<string, Copy> = {},
): Excerpt[] {
  const list: Excerpt[] = [];
  const byTable = new Map<string, Excerpt>();
  for (const f of found) {
    const m = f.metadata ?? {};
    const tableId = typeof m.table === "string" ? m.table : "";
    const seen = byTable.get(tableId);
    if (seen) {
      seen.chunks.push(f.id);
      continue;
    }
    let text = typeof m.text === "string" ? m.text : "";
    const table = tableId ? tables[tableId] : undefined;
    if (table) {
      text = table.markdown.trim();
      if (table.context && !text.includes(table.context)) text = `${table.context}\n\n${text}`;
    }
    const page = typeof m.page === "string" ? m.page : "";
    const e: Excerpt = {
      number: list.length + 1,
      page,
      url: typeof m.url === "string" ? m.url : "",
      year: typeof m.year === "string" ? m.year : "",
      text,
      title: titles[page] ?? "",
      chunks: [f.id],
      score: Number(f.score ?? 0),
      copies: [],
    };
    const c = copies[f.id];
    if (c) {
      e.copies.push({ number: e.number, page: c.code, url: c.url, year: c.year, text: "", title: c.title, chunks: [], score: 0, copies: [] });
    }
    list.push(e);
    if (table) byTable.set(tableId, e);
  }
  return list;
}

function fill(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => values[name] ?? "");
}

/** The sentence about years for the LLM (`prompt.json`): if the two cohorts have different rules,
 * both with their year. */
export function yearsContext(current: string): string {
  return fill(PROMPT.years, { previous: previousYear(current), current });
}

export type Messages = { role: "system" | "user"; content: string }[];

/** With "Reply": asks the LLM to rewrite the question so that it stands without the quoted message. */
export function rewriteMessages(question: string, quoted: string): Messages {
  return [
    { role: "system", content: PROMPT.rewrite },
    { role: "user", content: fill(PROMPT.rewrite_question, { quoted, question }) },
  ];
}

// --- Course codes ---

/** A word of 2–7 letters and digits, not inside a longer one (ASCII only, as in `rag_local/rag.py`). */
const WORD = /(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{1,6}(?![A-Za-z0-9])/g;

/** Course codes of the timetables written in the question get the course name after them ("quanti
 * cfu vale RAL?" → "quanti cfu vale RAL (Reinforcement and Adversarial Learning)?"): a code alone
 * means nothing to the embedding. Codes of 3 or more characters in any case, codes of 2 only in
 * upper case ("no", "ds" are words); not if the name is already in the question. As in `rag_local/rag.py`. */
export function expandAcronyms(question: string, codes: Record<string, string>): string {
  const lower = question.toLowerCase();
  return question.replace(WORD, (word) => {
    const name = codes[word.toUpperCase()];
    if (!name || (word.length < 3 && word !== word.toUpperCase()) || lower.includes(name.toLowerCase())) return word;
    return `${word} (${name})`;
  });
}

const QUOTES = /^["'«»“”]+|["'«»“”]+$/g;

/** The first line of the rewrite, without quotes; the original question if it is empty or too long. */
export function rewrittenQuestion(text: string, question: string): string {
  const line = text.split(/\r?\n/).map((r) => r.trim().replace(QUOTES, "").trim()).find((r) => r) ?? "";
  return line.length > 0 && line.length <= MAX_REWRITTEN ? line : question;
}

/** System prompt and message with date, years and numbered excerpts. With "Reply" the question is
 * already the rewritten one. */
export function messages(question: string, list: Excerpt[], today: [number, number, number]): Messages {
  const blocks = list.map((e) => {
    let heading = `[${e.number}] ${e.title || e.url}`;
    if (ofOneCohort(e.year)) heading += ` (anno ${excerptYears(e)})`;
    return `${heading}\n${e.text}`;
  });
  const [year, month, day] = today;
  const date = `${String(day).padStart(2, "0")}/${String(month).padStart(2, "0")}/${year}`;
  const user =
    `Oggi è il ${date}. ${yearsContext(currentYear(today))}\n\n` +
    "Estratti:\n\n" +
    blocks.join("\n\n") +
    `\n\nDomanda: ${question}\n\n` +
    PROMPT.closing;
  return [
    { role: "system", content: SYSTEM },
    { role: "user", content: user },
  ];
}

// --- Sources ---

// "Fonti: [1] [3]" at the start of a line or after the end of a sentence; also "Fonte:" and
// "Fonti utilizzate:". As in `rag_local/rag.py`.
const SOURCES_LINE =
  /(?:^|(?<=[.!?)]))[ \t*_]*(font[ei]|sources?)([ \t]+(utilizzate|usate|citate|used|cited))?[*_]*[ \t]*:[^\n]*$/gim;
const CITATION = /\[(\d{1,2})\]/g;
// The refusal of rule 1 of the prompt: no sources even if the model writes "Fonti:". As in `rag_local/rag.py`.
const REFUSAL = /posso aiutarti solo|can only help/i;

/**
 * Removes the "Fonti:" line and references like "[2]" from the text and returns the cited excerpts
 * (valid numbers, each URL once). The sources are those of the "Fonti:" line; without it, the
 * references in the text. With neither (an "I don't know" answer) no source.
 */
export function splitSources(text: string, list: Excerpt[]): [string, Excerpt[]] {
  const lines = text.match(SOURCES_LINE) ?? [];
  const citations = lines.length ? lines.join(" ") : text;
  const numbers = new Set([...citations.matchAll(CITATION)].map((m) => Number(m[1])));
  const clean = text
    .replace(SOURCES_LINE, "")
    .replace(/[ \t]*(\[\d{1,2}\][ \t]*)+(?=[.,;:!?)]|$)/gm, "")
    .replace(/[ \t]*\[\d{1,2}\]/g, "")
    .trim();
  if (REFUSAL.test(clean)) return [clean, []];
  const sources: Excerpt[] = [];
  const seen = new Set<string>();
  for (const e of list) {
    if (!numbers.has(e.number)) continue;
    for (const source of [e, ...e.copies]) {
      // the same text in two cohorts: both sources
      if (!seen.has(source.url)) {
        sources.push(source);
        seen.add(source.url);
      }
    }
  }
  return [clean, sources];
}

/** True if no result reaches the threshold (never with the threshold off). */
export function offTopic(found: Found[], threshold: number): boolean {
  const best = Math.max(0, ...found.map((f) => Number(f.score ?? 0)));
  return threshold > 0 && best < threshold;
}

// --- The whole round ---

/** The Cloudflare services used, with the shapes of the bindings (replaced in the tests). */
export interface Services {
  embedding(text: string): Promise<number[]>;
  search(vector: number[], topK: number, filter: Obj): Promise<Found[]>;
  tables(ids: string[]): Promise<Record<string, Table>>;
  /** Course code → course name, from the `acronyms` table of D1. */
  acronyms(): Promise<Record<string, string>>;
  /** Titles of the sources, the codes of those absent at the last run (`absent_since`) and, for the
   * chunk IDs, the other document with the same text: from D1, in one request. */
  sources(
    codes: string[],
    chunks: string[],
  ): Promise<{ titles: Record<string, string>; copies: Record<string, Copy>; absent?: string[] }>;
  /** Without options: the answer (`MAX_ANSWER_TOKENS`, temperature 0.2). */
  answer(messages: Messages, options?: LlmOptions): Promise<string>;
}

export interface LlmOptions {
  maxTokens: number;
  temperature: number;
}

export interface Result {
  answer: string;
  sources: Excerpt[];
  excerpts: Excerpt[];
  years: string[];
}

function distinct(found: Found[], key: string): string[] {
  const values = found.map((f) => f.metadata?.[key]).filter((v): v is string => typeof v === "string" && v !== "");
  return [...new Set(values)].sort();
}

/** The whole round for a question. */
export async function ask(
  question: string,
  options: { now: Date; services: Services; quoted?: string; threshold?: number },
): Promise<Result> {
  const { now, services, quoted, threshold = RELEVANCE_THRESHOLD } = options;
  const today = todayInRome(now);
  const years = filterYears(currentYear(today));
  const [standalone, acronyms] = await Promise.all([
    quoted
      ? services
          .answer(rewriteMessages(question, quoted), { maxTokens: MAX_REWRITE_TOKENS, temperature: 0 })
          .then((text) => rewrittenQuestion(text, question))
      : question,
    services.acronyms(),
  ]);
  const searched = expandAcronyms(standalone, acronyms);
  const vector = await services.embedding(searched);
  const found = await services.search(vector, EXCERPTS, searchFilter(years, Math.floor(now.getTime() / 1000)));
  if (offTopic(found, threshold)) return { answer: OFF_TOPIC_TEXT, sources: [], excerpts: [], years };
  const ids = distinct(found, "table");
  const codes = distinct(found, "page");
  const [tables, { titles, copies, absent = [] }] = await Promise.all([
    ids.length ? services.tables(ids) : Promise.resolve({}),
    codes.length ? services.sources(codes, found.map((f) => f.id)) : Promise.resolve({ titles: {}, copies: {}, absent: [] }),
  ]);
  // A source absent at the last run (e.g. a PDF replaced by a new version) stays in Vectorize until
  // it is deleted: its chunks are not shown, or the bot cites the old and the new version.
  const present = found.filter((f) => !absent.includes(String(f.metadata?.page ?? "")));
  if (!present.length) return { answer: OFF_TOPIC_TEXT, sources: [], excerpts: [], years };
  const list = excerpts(present, tables, titles, copies);
  const text = await services.answer(messages(searched, list, today));
  const [answer, sources] = splitSources(text, list);
  return { answer, sources, excerpts: list, years };
}
