/**
 * The bot's logic, with no dependency on the Cloudflare runtime: tested with `node --test`.
 *
 * Data rules: nothing saved, no printing of messages, answers or IDs; of errors only the
 * type is logged. The only place writing to the logs is `logEvent()`.
 */

export const WEBHOOK_PATH = "/telegram";
export const SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token";
export const MAX_CHARS = 4000; // Telegram's limit: 4096 characters per message
export const MAX_ANSWER_CHARS = 3000; // the LLM's answer, leaving room for the sources
export const MAX_QUOTED = 1500; // the bot message quoted with "Reply", passed to the LLM

import {
  ask,
  MAX_ANSWER_TOKENS,
  OFF_TOPIC_TEXT,
  todayInRome,
  type LlmOptions,
  type Messages,
  type Services,
} from "./rag.ts";

export const COURSE_PAGE = "https://web.unica.it/unica/it/crs_60_73.page";
export const NOTICES_PAGE = "https://web.unica.it/unica/it/crs_60_73_5.page";
export const NOTICES_SHOWN = 5;
export const UPDATES_DAYS = 14; // two weeks: if a Sunday run fails, the updates stay
export const UPDATES_SHOWN = 10;
export const EVENTS_PAGE = "https://web.unica.it/unica/it/crs_60_73_27.page";
export const EVENTS_SHOWN = 5;

/**
 * Language of the bot's fixed texts: Italian if the sender's Telegram app is in Italian, English for
 * every other language and when the app does not send it (`language_code` is optional). Answers to
 * questions follow the question's language instead (the model writes them). `language_code` is read
 * only to choose the text: never saved nor logged.
 */
export type Language = "it" | "en";

export function language(code: unknown): Language {
  return typeof code === "string" && code.toLowerCase().startsWith("it") ? "it" : "en";
}

interface Texts {
  start: string;
  noNotices: string;
  noUpdates: string;
  noEvents: string;
  error: string;
  quota: string;
  overloaded: string;
  textOnly: string;
  offTopic: string;
  sources: string;
  source: string;
  validNotices: (n: number) => string;
  publishedOn: string;
  deadline: string;
  allNotices: string;
  noticesPage: string;
  updatesTitle: string;
  newItems: string;
  changedItems: string;
  eventsTitle: (n: number) => string;
  allEvents: string;
  eventsPage: string;
  whereFrom: string;
  onlyContent: string;
  lastUpdate: string;
  weeklyUpdate: string;
  officialSite: string;
  kinds: [string, string, string][]; // source kind in D1, singular, plural
}

export const TEXTS: Record<Language, Texts> = {
  it: {
    start:
      "Ciao! Sono UniRAG-inf, un bot NON ufficiale, non affiliato all'Università di Cagliari.\n\n" +
      "Rispondo alle domande sulla laurea magistrale in Informatica usando le pagine e i documenti " +
      "pubblici del corso. Le risposte sono generate da un modello di intelligenza artificiale e " +
      "possono contenere errori: indico sempre le fonti, controllale prima di fidarti.\n\n" +
      "Scrivi la domanda per intero, per esempio: Quanti CFU vale Data Mining?\n\n" +
      "Comandi:\n/avvisi - gli avvisi ancora validi\n/eventi - seminari e attività che danno CFU\n" +
      "/novita - cosa è cambiato sul sito di recente\n/fonti - da dove vengono le informazioni\n\n" +
      "Per una domanda di seguito usa «Rispondi» su un mio messaggio: lo uso solo per capire a cosa ti " +
      "riferisci. Senza «Rispondi» ogni domanda è indipendente.\n\n" +
      "Non salvo i tuoi messaggi.",
    noNotices: "Al momento non ci sono avvisi validi.",
    noUpdates: `Nessuna novità negli ultimi ${UPDATES_DAYS} giorni.`,
    noEvents: "Al momento non ci sono seminari o attività in elenco.",
    error: "Al momento non riesco a rispondere. Riprova tra poco.",
    quota:
      "Per oggi ho finito le risposte gratuite a disposizione del progetto. Riprova domani: " +
      "intanto /avvisi e /fonti funzionano.",
    overloaded: "Il servizio che uso per rispondere è sovraccarico. Riprova tra qualche secondo.",
    textOnly: "Per ora capisco solo messaggi di testo.",
    offTopic: OFF_TOPIC_TEXT,
    sources: "Fonti",
    source: "Fonte",
    validNotices: (n) => `<b>Avvisi validi</b> (i ${n} più recenti)`,
    publishedOn: "pubblicato il",
    deadline: "scadenza",
    allNotices: "Tutti gli avvisi",
    noticesPage: "pagina Avvisi del corso",
    updatesTitle: `<b>Novità degli ultimi ${UPDATES_DAYS} giorni</b>`,
    newItems: "Nuovi",
    changedItems: "Modificati sul sito",
    eventsTitle: (n) => `<b>Seminari e attività che danno CFU</b> (i primi ${n} della pagina)`,
    allEvents: "Tutte le attività",
    eventsPage: "pagina Altre attività formative",
    whereFrom: "<b>Da dove vengono le risposte</b>",
    onlyContent:
      "Solo da contenuti pubblici della laurea magistrale in Informatica dell'Università di Cagliari:",
    lastUpdate: "Ultimo aggiornamento",
    weeklyUpdate:
      "Gli avvisi si aggiornano ogni giorno, il resto dei contenuti una volta alla settimana: in caso di dubbio vale il",
    officialSite: "sito ufficiale del corso",
    kinds: [
      ["page", "pagina del corso", "pagine del corso (web.unica.it)"],
      ["pdf", "documento PDF", "documenti PDF (regolamenti, percorsi formativi, orario, calendario esami, bandi)"],
      ["notice", "avviso", "avvisi"],
      ["course", "scheda di insegnamento", "schede degli insegnamenti dell'anno in corso (catalogo corsi)"],
      ["external", "pagina della Facoltà", "pagine della Facoltà"],
    ],
  },
  en: {
    start:
      "Hi! I'm UniRAG-inf, an UNOFFICIAL bot, not affiliated with the University of Cagliari.\n\n" +
      "I answer questions about the Master's degree in Computer Science using the public pages and " +
      "documents of the course. Answers are generated by an artificial intelligence model and may " +
      "contain errors: I always show the sources, check them before relying on an answer.\n\n" +
      "Write the whole question, for example: How many credits is Data Mining worth?\n\n" +
      "Commands:\n/notices - notices still valid\n/events - seminars and activities that give credits\n" +
      "/updates - what changed on the website recently\n/sources - where the information comes from\n\n" +
      "For a follow-up question use “Reply” on one of my messages: I use it only to understand what " +
      "you are referring to. Without “Reply” every question is independent.\n\n" +
      "I don't store your messages.",
    noNotices: "There are no valid notices at the moment.",
    noUpdates: `No changes in the last ${UPDATES_DAYS} days.`,
    noEvents: "There are no seminars or activities listed at the moment.",
    error: "I can't answer right now. Please try again shortly.",
    quota:
      "I've used up the free answers available to the project for today. Please try again tomorrow: " +
      "meanwhile /notices and /sources still work.",
    overloaded: "The service I use to answer is overloaded. Please try again in a few seconds.",
    textOnly: "For now I only understand text messages.",
    offTopic:
      "I found nothing about this in the course pages: I can only help with information about the " +
      "Master's degree in Computer Science at the University of Cagliari.\n\n" +
      "ℹ️ Automatic reply: a filter stops questions that look off-topic. If your question is about the " +
      "course, write it in full: for example “How many credits is Data Mining worth?” instead of " +
      "“data mining credits”.",
    sources: "Sources",
    source: "Source",
    validNotices: (n) => `<b>Valid notices</b> (the ${n} most recent)`,
    publishedOn: "published on",
    deadline: "deadline",
    allNotices: "All notices",
    noticesPage: "course Notices page",
    updatesTitle: `<b>Changes in the last ${UPDATES_DAYS} days</b>`,
    newItems: "New",
    changedItems: "Changed on the website",
    eventsTitle: (n) => `<b>Seminars and activities that give credits</b> (the first ${n} on the page)`,
    allEvents: "All activities",
    eventsPage: "“Altre attività formative” page",
    whereFrom: "<b>Where the answers come from</b>",
    onlyContent:
      "Only from public content of the Master's degree in Computer Science of the University of Cagliari:",
    lastUpdate: "Last update",
    weeklyUpdate:
      "Notices are updated every day, the rest of the content once a week: when in doubt, refer to the",
    officialSite: "official course website",
    kinds: [
      ["page", "course page", "course pages (web.unica.it)"],
      ["pdf", "PDF document", "PDF documents (regulations, study plans, timetable, exam calendar, calls)"],
      ["notice", "notice", "notices"],
      ["course", "course description", "course descriptions of the current year (course catalogue)"],
      ["external", "Faculty page", "Faculty pages"],
    ],
  },
};

/** Sends a message in Telegram's HTML (`parse_mode: "HTML"`): the text must already be safe. */
export type SendMessage = (chatId: number, html: string) => Promise<void>;
/** Shows "typing…" in the chat. */
export type ShowTyping = (chatId: number) => Promise<void>;

/** A notice from D1 (column names; dates as Unix timestamps). */
export interface Notice {
  title: string;
  url: string;
  published_at: number | null;
  deadline: number | null;
}

/** How many sources there are per kind and when the last successful pipeline run ended. */
export interface SourcesState {
  byKind: Record<string, number>;
  lastRun: number | null;
}

/** An event of the "Altre attività formative" page (D1 columns; texts as written on the site). */
export interface Event {
  title: string;
  date_text: string;
  credits: string;
  url: string;
}

/** A source added (`added_at`) or changed by UniCa (`changed_at`) recently (D1 columns). */
export interface Update {
  title: string;
  url: string;
  added_at: number | null;
  changed_at: number | null;
}

/** Reads from D1 for the commands (replaced in the tests). */
export interface Data {
  notices(now: number, howMany: number): Promise<Notice[]>;
  sourcesState(): Promise<SourcesState>;
  updates(since: number, howMany: number): Promise<Update[]>;
  events(howMany: number): Promise<Event[]>;
}

export interface Message {
  chatId: number;
  text: string | null; // null if the message is not text (photo, audio, ...)
  quoted: string | null; // the bot message the student answers with "Reply"
  language: Language; // of the fixed texts, from the Telegram app's language (`from.language_code`)
}

type Obj = Record<string, unknown>;

function obj(value: unknown): Obj {
  return typeof value === "object" && value !== null ? (value as Obj) : {};
}

/** Logs a technical event. Only names and technical fields: never texts, answers or IDs. */
export function logEvent(event: string, fields: Record<string, string | number> = {}): void {
  const details = Object.entries(fields)
    .map(([key, value]) => `${key}=${value}`)
    .join(" ");
  console.warn(`${event} ${details}`.trim());
}

export type ErrorCause = "quota" | "overloaded" | "other";

/**
 * The cause of a Workers AI error from Cloudflare's code in the message: daily free quota used up
 * ("daily free allocation"), 3040 = model overloaded. The message is read only here, never logged.
 */
export function errorCause(error: unknown): ErrorCause {
  const message = error instanceof Error ? error.message : "";
  if (/\b4006\b|daily free allocation/i.test(message)) return "quota";
  if (/\b3040\b|capacity temporarily exceeded/i.test(message)) return "overloaded";
  return "other";
}

function textForCause(cause: ErrorCause, l: Language): string {
  return { quota: TEXTS[l].quota, overloaded: TEXTS[l].overloaded, other: TEXTS[l].error }[cause];
}

/** The type of an error, never its message (it may contain the question or the token). */
export function errorType(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/** True only if Telegram's secret header is there and right (constant-time comparison). */
export function authorised(received: string | null, expected: string): boolean {
  if (!expected || received === null) return false;
  const a = new TextEncoder().encode(received);
  const b = new TextEncoder().encode(expected);
  let different = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) different |= (a[i] ?? 0) ^ b[i];
  return different === 0;
}

/** The bot's fixed texts: quoting them with "Reply" says nothing about the question. */
const FIXED_TEXTS = () =>
  Object.values(TEXTS).flatMap((t) => [t.start, t.error, t.quota, t.overloaded, t.textOnly, t.offTopic]);

/**
 * The bot message the student answers with "Reply": only if the bot wrote it, without the
 * "Fonti"/"Sources" part, shortened. Telegram attaches it to the update: nothing is saved.
 */
function quotedText(message: Obj): string | null {
  const original = obj(message.reply_to_message);
  if (obj(original.from).is_bot !== true || typeof original.text !== "string") return null;
  const text = original.text.split(/\n\n(?:Fonti|Sources)\n/)[0].trim();
  if (!text || FIXED_TEXTS().includes(text)) return null;
  return text.slice(0, MAX_QUOTED);
}

/** The message to handle, only if it comes from a private chat. */
export function readMessage(update: unknown): Message | null {
  const message = obj(obj(update).message);
  const chat = obj(message.chat);
  if (chat.type !== "private" || !Number.isInteger(chat.id)) return null;
  const text = typeof message.text === "string" ? message.text : null;
  const l = language(obj(message.from).language_code);
  return { chatId: chat.id as number, text, quoted: quotedText(message), language: l };
}

/** Text of a Workers AI answer (OpenAI format or `response`). */
export function llmText(result: unknown): string {
  const r = obj(result);
  if (typeof r.response === "string") return r.response.trim();
  const choices = Array.isArray(r.choices) ? r.choices : [];
  const message = obj(obj(choices[0]).message);
  return typeof message.content === "string" ? message.content.trim() : "";
}

/** Removes Markdown bold (Telegram would show it as asterisks) and shortens. */
export function clean(text: string, maximum = MAX_CHARS): string {
  let result = text.replace(/\*\*(.+?)\*\*|__(.+?)__/g, (_, a, b) => a ?? b);
  if (result.length > maximum) result = result.slice(0, maximum - 1).trimEnd() + "…";
  return result;
}

/** Text safe for Telegram's HTML: a "<" in the LLM's answer must not become a tag. */
export function html(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Request to Workers AI (answer or, with the options, question rewrite), with reasoning off (Gemma 4
 * otherwise spends it all and leaves the answer empty). */
export function llmRequest(
  messages: Messages,
  options: LlmOptions = { maxTokens: MAX_ANSWER_TOKENS, temperature: 0.2 },
): Record<string, unknown> {
  return {
    messages,
    max_tokens: options.maxTokens,
    temperature: options.temperature,
    chat_template_kwargs: { enable_thinking: false },
  };
}

/** The message in HTML: answer (plain text, made safe) and the cited sources as links. */
export function answerText(answer: string, sources: { title: string; url: string }[], l: Language = "it"): string {
  const text = html(clean(answer, MAX_ANSWER_CHARS));
  if (!sources.length) return text;
  const list = sources.map((s) => {
    const title = html(s.title || TEXTS[l].source);
    return /^https?:\/\//.test(s.url) ? `• <a href="${html(s.url)}">${title}</a>` : `• ${title}`;
  });
  return `${text}\n\n<b>${TEXTS[l].sources}</b>\n${list.join("\n")}`;
}

/** The Italian command names (Italian menu): same command as the English one. */
export const ITALIAN_COMMANDS: Record<string, string> = {
  avvisi: "notices",
  eventi: "events",
  novita: "updates",
  fonti: "sources",
};
const ENGLISH_COMMANDS = new Set(Object.values(ITALIAN_COMMANDS));

/**
 * The language of the answer to a command: that of the chosen name (`/notices` → English, `/avvisi` →
 * Italian), because Telegram shows the menu in the app's language even when the app does not send it
 * in the message (`language_code` is optional). For the other commands (`/start`) the app's one.
 */
export function commandLanguage(text: string, app: Language): Language {
  const m = /^\/([a-z0-9_]+)/i.exec(text.trim());
  const name = m ? m[1].toLowerCase() : "";
  if (name in ITALIAN_COMMANDS) return "it";
  if (ENGLISH_COMMANDS.has(name)) return "en";
  return app;
}

/** The command at the start of the message ("/avvisi", "/avvisi@BotName", "/notices"), lower case and
 * with its English name; null if there is none. */
export function command(text: string): string | null {
  const m = /^\/([a-z0-9_]+)(@\w+)?(\s|$)/i.exec(text.trim());
  if (!m) return null;
  const name = m[1].toLowerCase();
  return ITALIAN_COMMANDS[name] ?? name;
}

/** Italian date (Rome time) of a Unix timestamp. */
export function date(timestamp: number): string {
  const [year, month, day] = todayInRome(new Date(timestamp * 1000));
  return `${String(day).padStart(2, "0")}/${String(month).padStart(2, "0")}/${year}`;
}

function link(url: string, text: string): string {
  return /^https?:\/\//.test(url) ? `<a href="${html(url)}">${html(text)}</a>` : html(text);
}

/** /avvisi: the latest notices still valid, with dates and links. */
export function noticesText(notices: Notice[], l: Language = "it"): string {
  const t = TEXTS[l];
  const all = `${t.allNotices}: ${link(NOTICES_PAGE, t.noticesPage)}`;
  if (!notices.length) return `${t.noNotices}\n\n${all}`;
  const lines = notices.map((n) => {
    const dates = [
      n.published_at ? `${t.publishedOn} ${date(n.published_at)}` : "",
      n.deadline ? `${t.deadline} ${date(n.deadline)}` : "",
    ].filter(Boolean);
    return `• ${link(n.url, n.title)}` + (dates.length ? `\n   ${dates.join(" · ")}` : "");
  });
  return `${t.validNotices(notices.length)}\n\n${lines.join("\n\n")}\n\n${all}`;
}

/** /eventi: the first events of the "Altre attività formative" page, with date and CFU as written there. */
export function eventsText(events: Event[], l: Language = "it"): string {
  const t = TEXTS[l];
  const all = `${t.allEvents}: ${link(EVENTS_PAGE, t.eventsPage)}`;
  if (!events.length) return `${t.noEvents}\n\n${all}`;
  const lines = events.map((e) => {
    const details = [e.date_text, e.credits].filter((x) => x).map(html);
    return `• ${link(e.url, e.title)}` + (details.length ? `\n   ${details.join(" · ")}` : "");
  });
  return `${t.eventsTitle(events.length)}\n\n${lines.join("\n\n")}\n\n${all}`;
}

/** /novita: sources added and sources changed by UniCa from `since` on, with their date. */
export function updatesText(updates: Update[], since: number, l: Language = "it"): string {
  const t = TEXTS[l];
  const added = updates.filter((u) => u.added_at !== null && u.added_at >= since);
  const changed = updates.filter((u) => !added.includes(u) && u.changed_at !== null && u.changed_at >= since);
  if (!added.length && !changed.length) return t.noUpdates;
  const list = (items: Update[], when: (u: Update) => number) =>
    items.map((u) => `• ${link(u.url, u.title)} (${date(when(u))})`).join("\n");
  const parts = [t.updatesTitle];
  if (added.length) parts.push(`${t.newItems}\n${list(added, (u) => u.added_at!)}`);
  if (changed.length) parts.push(`${t.changedItems}\n${list(changed, (u) => u.changed_at!)}`);
  return parts.join("\n\n");
}

/** /fonti: where the information comes from and when it was updated. */
export function sourcesText(state: SourcesState, l: Language = "it"): string {
  const t = TEXTS[l];
  const lines = t.kinds.filter(([kind]) => state.byKind[kind]).map(([kind, one, many]) => {
    const n = state.byKind[kind];
    return `• ${n} ${n === 1 ? one : many}`;
  });
  const update = state.lastRun ? `${t.lastUpdate}: ${date(state.lastRun)}. ` : "";
  return (
    `${t.whereFrom}\n\n${t.onlyContent}\n${lines.join("\n")}\n\n` +
    `${update}${t.weeklyUpdate} ${link(COURSE_PAGE, t.officialSite)}.`
  );
}

/** Answers a Telegram update. Returns and keeps nothing. */
export async function handle(
  update: unknown,
  options: { services: Services; data: Data; send: SendMessage; typing?: ShowTyping; now?: Date },
): Promise<void> {
  const message = readMessage(update);
  if (message === null) return;
  const l = message.language;
  if (message.text === null) {
    await options.send(message.chatId, TEXTS[l].textOnly);
    return;
  }
  const now = options.now ?? new Date();
  const name = command(message.text);
  if (name !== null) {
    const lc = commandLanguage(message.text, l);
    await options.send(message.chatId, await commandText(name, options.data, now, lc));
    return;
  }
  if (options.typing) {
    // "typing…" while the RAG works (4–5 s); if it fails, the answer is sent anyway
    await options.typing(message.chatId).catch((error) => {
      logEvent("typing_error", { type: errorType(error) });
    });
  }
  let text: string;
  try {
    const r = await ask(message.text, { now, services: options.services, quoted: message.quoted ?? undefined });
    const answer = r.answer === OFF_TOPIC_TEXT ? TEXTS[l].offTopic : r.answer;
    text = answer ? answerText(answer, r.sources, l) : TEXTS[l].error;
  } catch (error) {
    const cause = errorCause(error);
    logEvent("rag_error", { type: errorType(error), cause });
    text = textForCause(cause, l);
  }
  await options.send(message.chatId, text);
}

/** The text of a command; an unknown command (also /start, /help) gets the presentation. */
async function commandText(name: string, data: Data, now: Date, l: Language): Promise<string> {
  try {
    if (name === "notices") return noticesText(await data.notices(Math.floor(now.getTime() / 1000), NOTICES_SHOWN), l);
    if (name === "sources") return sourcesText(await data.sourcesState(), l);
    if (name === "events") return eventsText(await data.events(EVENTS_SHOWN), l);
    if (name === "updates") {
      const since = Math.floor(now.getTime() / 1000) - UPDATES_DAYS * 86400;
      return updatesText(await data.updates(since, UPDATES_SHOWN), since, l);
    }
  } catch (error) {
    logEvent("command_error", { command: name, type: errorType(error) }); // only known commands
    return TEXTS[l].error;
  }
  return TEXTS[l].start;
}
