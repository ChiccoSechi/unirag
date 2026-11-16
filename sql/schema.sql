-- D1 schema of UniRAG-inf.
-- Database "unirag-inf" in the EU jurisdiction. Only the pipeline writes; the bot reads.
-- D1 is the source of truth on what is in Vectorize: it is always updated last.
-- Dates as Unix timestamps (Europe/Rome time zone). No table about the bot's users.
-- It can be run again: it creates only what is missing.

-- Registry of what is in Vectorize: one row per source (page, notice, course sheet, PDF).
create table if not exists sources (
  code text primary key,           -- p21, d543188, i23489.2026: prefix of the chunk IDs (p21-0, p21-1…)
  url text not null unique,
  title text not null,             -- to cite the source and for /sources
  kind text not null,              -- page, external, notice, course, pdf
  year text not null,              -- "2026/2027" or "all" (all years)
  hash text not null,              -- fingerprint of the chunks as written in Vectorize: decides what to rewrite
  text_hash text not null,         -- fingerprint of the extracted text only: tells if UniCa changed the page
  chunk_count integer not null,    -- chunks in Vectorize: IDs from <code>-0 to <code>-(chunk_count-1)
  updated_at integer not null,     -- last write of the chunks to Vectorize
  absent_since integer,            -- null if the source exists; absent for 2 runs in a row → deleted
  added_at integer,                -- first time the source appeared (for /updates); null if older
  changed_at integer               -- last time UniCa changed its text (for /updates)
);

-- Notices for the /notices command (valid ones: valid_until >= now).
create table if not exists notices (
  code text primary key references sources (code),
  title text not null,
  url text not null,
  published_at integer,            -- null if the notice has no date
  deadline integer,                -- date found in the text; null if none
  valid_until integer not null     -- deadline + 7 days, or 31 December, or NEVER
);

-- Identical parts in the documents of two cohorts (e.g. equal articles in the 2025/26 and 2026/27
-- regulations, `pipeline/cohorts.py`): the chunk is in Vectorize once, in the most recent document;
-- here the link to the other document, to show its source too.
create table if not exists copies (
  chunk text primary key,                         -- e.g. d543188-12
  source text not null references sources (code), -- the chunk's document (the most recent)
  also_for text not null                          -- the other document (e.g. d508638); no constraint:
                                                  -- if it disappears, the next run rewrites the chunk
);

-- Whole tables of the PDFs, in Markdown: the bot passes them to the LLM when a chunk comes from a
-- table (`table` metadata of the chunk in Vectorize).
create table if not exists tables (
  id text primary key,             -- d547249-t0: code of the PDF and number of the table
  source text not null references sources (code),
  pdf_page integer,                -- to cite the page
  context text not null,           -- e.g. "indirizzo Cloud And Security, 1° ANNO 2026/2027"
  year text not null,
  markdown text not null,
  hash text not null               -- rewritten only if it changes
);

-- Events of the event pages ("Altre attività formative"), in the page's order, for /events.
create table if not exists events (
  id text primary key,             -- p27-v0: code of the page and position
  source text not null references sources (code),
  position integer not null,       -- order on the page
  title text not null,
  date_text text not null,         -- as written on the site ("16 e 17 ottobre 2026"), "" if missing
  credits text not null,           -- as written on the site ("1 CFU"), "" if missing
  url text not null                -- link of the event, "" if missing
);

-- Course codes of the lesson timetables (legend of the PDF): the bot adds the name after a code
-- written in the question ("RAL" → "RAL (Reinforcement and Adversarial Learning)") before the search.
create table if not exists acronyms (
  source text not null references sources (code),  -- the timetable PDF
  code text not null,              -- e.g. RAL
  name text not null,              -- e.g. Reinforcement and Adversarial Learning (without teachers)
  primary key (source, code)
);

-- Log of the pipeline runs: only technical data, never texts nor personal data.
create table if not exists runs (
  id integer primary key autoincrement,
  started_at integer not null,
  ended_at integer,
  new_sources integer not null default 0,
  changed_sources integer not null default 0,    -- different chunk hash
  modified_sources integer not null default 0,   -- different text_hash: changed by UniCa
  absent_sources integer not null default 0,
  deleted_sources integer not null default 0,
  chunks_written integer not null default 0,
  chunks_deleted integer not null default 0,
  neurons real not null default 0,
  outcome text,                    -- "ok" (full run), "ok_notices" (daily notices run,
                                   -- additions only) or "error"
  error text,                      -- only the error type (e.g. ConnectTimeout), never the message
  extraction_version text          -- fingerprint of the extraction code: if it changes, different
                                   -- texts do not count as "changed by UniCa"
);

create index if not exists sources_absent on sources (absent_since);
create index if not exists notices_valid on notices (valid_until);
create index if not exists tables_of_source on tables (source);
create index if not exists events_of_source on events (source, position);
create index if not exists sources_updates on sources (added_at, changed_at);
