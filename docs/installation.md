# Installation

This page explains how to run **your own copy** of the bot with your own free accounts, from an empty
computer to the first answer. It takes about an hour. The code is shared as is: read each step
before running it.

## Requirements

| Tool | What for |
| --- | --- |
| git | the code |
| [uv](https://docs.astral.sh/uv/) | Python 3.14 and the pipeline dependencies |
| Node.js 24 (at least 22.18) | the bot in TypeScript and `wrangler`, Cloudflare's command line |
| A **Cloudflare** account, free plan | Workers, Workers AI, Vectorize, D1 (no credit card needed) |
| A **Telegram** account | to create the bot with @BotFather |
| A **GitHub** account | the weekly and daily runs with GitHub Actions |

```bash
git clone <URL of your fork>
cd unirag
uv sync
uv run pre-commit install    # optional: secret scan and lint before every commit
cd bot && npm install && cd ..
cp .env.example .env
```

`.env` holds your secrets and your configuration: fill it in as you go through the steps below, and
never commit it (git already ignores it).

### Two ways into Cloudflare

`wrangler` is installed in `bot/` and is used in two ways:

- **from the main folder**, as `npx --prefix bot wrangler …`: it reads the API token from `.env` and
  manages **Vectorize and D1**;
- **from `bot/`**, as `npx wrangler …`: it uses your login (`wrangler login`) and **publishes the bot**.

Never load `.env` into your terminal (`source .env`): the token would stay active in `bot/` too and
`wrangler` would refuse the login ("You are logged in with an API Token"). If it happens, run
`unset CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID` or open a new terminal.

## 1. Cloudflare

1. **Account ID** (`CLOUDFLARE_ACCOUNT_ID`): in the dashboard, Workers & Pages → Account Details.
2. **API token** (`CLOUDFLARE_API_TOKEN`), used by the pipeline and by the local commands: My Profile →
   API Tokens → Create Custom Token, with three permissions limited to your account:
   `Account · Workers AI · Edit`, `Account · Vectorize · Edit`, `Account · D1 · Edit`. It is shown only once.
   To check it without printing it:
   ```bash
   ( set -a; source .env; curl -s https://api.cloudflare.com/client/v4/user/tokens/verify -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" )
   ```
   The answer must contain `"status":"active"`. The parentheses keep the token out of your terminal.
3. **Vectorize index**, from the main folder:
   ```bash
   bash setup/vectorize.sh
   ```
   It creates the index and its four metadata indexes (`year`, `kind`, `valid_until`, `page`).
   Cloudflare builds them in a queue, a few minutes each: **wait until all four are listed** before the
   first run, otherwise the passages written before are never found by the bot's filters.
   ```bash
   npx --prefix bot wrangler vectorize list-metadata-index unirag-inf
   ```
4. **D1 database**, in the EU jurisdiction, then its schema, from the main folder:
   ```bash
   npx --prefix bot wrangler d1 create unirag-inf --jurisdiction eu
   npx --prefix bot wrangler d1 execute unirag-inf --remote --file=sql/schema.sql
   ```
   The first command prints the database ID: put it in `.env` (`D1_DATABASE_ID`). Then copy
   `bot/wrangler.example.jsonc` to `bot/wrangler.jsonc` (ignored by git) and put the same ID there
   (`database_id`).
5. **Login for the bot**, from `bot/`: `npx wrangler login`. The page that opens asks for permissions:
   grant only User Read, Background Access, Account & Billing (read) and **Workers Scripts Write**. The bot
   reaches Workers AI, Vectorize and D1 through bindings, so it needs nothing else. Workers Logs is
   already off in the configuration; in the dashboard, check that Observability, Traces and Logpush
   are off too.

## 2. Telegram

1. With [@BotFather](https://t.me/BotFather): `/newbot`, a name that makes clear the bot is
   **unofficial**, and the token (`TELEGRAM_BOT_TOKEN` in `.env`). The bot answers only private chats:
   you can also turn off groups (`/setjoingroups` → Disable).
2. A random secret for the webhook (`TELEGRAM_WEBHOOK_SECRET` in `.env`):
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. Save both secrets in the Worker and publish it, from `bot/`:
   ```bash
   cd bot
   npx wrangler secret put TELEGRAM_BOT_TOKEN
   npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
   npx wrangler deploy
   cd ..
   ```
   The first deploy asks you to register a `workers.dev` subdomain, then prints the bot's address:
   `https://unirag-inf.<your-subdomain>.workers.dev`. To check the bot without publishing it, use
   `npx wrangler deploy --dry-run`.
4. Connect Telegram to the Worker and register the command menu, in Italian and English, from the
   main folder:
   ```bash
   uv run python setup/webhook.py https://unirag-inf.<your-subdomain>.workers.dev
   uv run python setup/commands.py
   ```
   `--info` on either command only shows the current state.

**The bot answers anyone who writes to it**: there is no list of allowed chats. Whoever finds its name
can use it, and uses your free quota. If other people use your bot, you are the data controller:
write your own privacy notice and link it in the bot's description with @BotFather.

## 3. The crawler's identity

Before any download, set in `.env` who the crawler is (see [Crawler](crawler.md)):

- `CRAWLER_CONTACT`: an email address you actually read; without it the crawler does not start. The
  site's administrators will write there to ask for changes: apply them at the next run;
- `CRAWLER_NAME`: the name in the User-Agent (default `UniRAG-inf-copy`; keep the `UniRAG-inf` prefix,
  so that the `robots.txt` rule in [Crawler](crawler.md) stops your copy too);
- `CRAWLER_INFO_URL` (optional): a page that explains your crawler, for example your copy of
  [Crawler](crawler.md).

## 4. First run, from your computer

Everything here runs in **reduced mode**: a few pages, at most 25 requests, files saved in `cache/` on
your computer (ignored by git).

```bash
uv run python -m pipeline.crawl              # reduced crawl, saved in cache/
uv run python -m pipeline.run --dry-run      # what the run would write, without writing
```

Options of the crawler: `--use-cache` downloads only what is missing in `cache/`, `--cache-only` makes
no requests at all, `--notices-only` reads only the new notices (the daily run). Options of the run:
`--dry-run` writes nothing and uses no quota, `--accept-absences` when you removed many sources on
purpose (otherwise the run refuses to delete them), `--additions-only` adds without deleting (the daily
run).

When the dry run looks right, the first real run is better done from GitHub (next step), in full mode.

## 5. GitHub Actions

Three workflows live in `.github/workflows/`:

- `pipeline.yml`: the full run, every Sunday at 01:00 UTC;
- `notices.yml`: new notices only, Monday to Saturday at 05:00 UTC;
- `ci.yml`: a secret scan on every push.

In a **fork**, GitHub keeps the workflows disabled until you enable them in the **Actions** tab. In a
**new repository** of yours they are active from the first push: disable `pipeline` and `notices`
(Actions → the workflow → ⋯ → Disable workflow) until everything below is ready.

1. Settings → Environments → **New environment** `pipeline`:
   - *Deployment branches:* only the main branch;
   - *Secrets:* `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (better a separate token with the same
     three permissions), `D1_DATABASE_ID`, `CRAWLER_CONTACT`;
   - *Variables:* `CRAWLER_NAME` and, if you have one, `CRAWLER_INFO_URL`.
2. Actions → enable the workflows.
3. Actions → **pipeline** → Run workflow. It takes a few minutes. Read the log: it must show only
   URLs and kinds of problems, never secrets (`***`) or page text.

GitHub disables scheduled workflows after 60 days without activity in a public repository: if the
runs stop, re-enable them from the Actions tab. Failure emails go to whoever last changed the `cron`
line, so change it yourself once (same time) to receive them.

Do not run `pipeline.run` from your computer while a workflow is writing.

## 6. Try the bot

Write to your bot: `/start`, `/notices`, `/events`, `/updates`, `/sources` (it shows the date of the
last run) and a question.

The same search and answer can be run from your computer, with the same prompt and the same models:

```bash
uv run python -m rag_local.ask "How many credits is Data Mining worth?"
```

| Option | What it does |
| --- | --- |
| `--show` | also prints the passages given to the model |
| `--chunks N` | how many passages to search (default 8) |
| `--model ID` | another Workers AI model, to compare answers |
| `--quoted "..."` | a previous answer of the bot, as if the question were a Telegram **Reply** to it |

## Good to know

**Free plan limits** (Cloudflare and GitHub, October 2026):

| Service | Limit that matters | This project |
| --- | --- | --- |
| Workers AI | 10,000 neurons a day | ~25 per question, a few per normal run |
| Workers | 100,000 requests a day, 10 ms of CPU per request | well within both |
| Vectorize | 5 million stored dimensions (~4,880 passages of 1,024 numbers), 30 million queried a month | ~800 passages |
| D1 | 5 GB, 5 million rows read and 100,000 written a day | a few MB |
| GitHub Actions | free in public repositories; scheduled runs stop after 60 days without activity | ~10 minutes a week |

- **Quota.** The bot, the runs and the questions from your computer share the same 10,000 Workers AI
  "neurons" a day (about 25 per question, a few for a normal run). If you go over, Workers AI refuses
  everything until the next day, the bot included.
- **CPU.** The free plan allows 10 ms of CPU per request. Check it in the dashboard: Workers & Pages →
  `unirag-inf` → Metrics → CPU Time.
- **Trying the bot locally:** `npx wrangler dev` from `bot/`, with the secrets in `bot/.dev.vars`
  (ignored by git). It still calls the real Workers AI and uses quota.
- **Every September** the academic year changes: in the log of the first run, check for warnings about a
  missing regulation or study plan of the new cohort (UniCa may publish them later).
- **Changing the embedding model** (`CF_EMBED_MODEL`) needs a new Vectorize index: the vectors of two
  models cannot be mixed.

## Adapting it to another course

The course is described in `pipeline/sources.yaml`: start pages, notices, excluded pages and sites,
catalogue addresses, limits. The table readers in `pipeline/tables/` are written for the PDFs of this
course and will need changes for other layouts; the texts for students are in `bot/src/bot.ts` and
the prompt in `bot/src/prompt.json`.
