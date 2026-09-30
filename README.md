# job-hunter

A job-search agent that runs 24/7 on my home NAS. It sweeps Singapore job boards and company career pages, has an LLM judge how well each new posting fits, drafts a tailored resume and cover letter for the good matches, and sends me a Telegram alert. **It never applies to anything.** I read every draft and decide myself.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Claude](https://img.shields.io/badge/LLM-Claude%20(claude%20--p)-D97757?logo=anthropic&logoColor=white)
![Telegram](https://img.shields.io/badge/Alerts-Telegram-26A5E4?logo=telegram&logoColor=white)
![Self-test](https://img.shields.io/badge/selftest-14%20checks-brightgreen)

![Architecture](docs/architecture.drawio.svg)

<sub>Editable source: [`docs/architecture.drawio`](docs/architecture.drawio) · PNG fallback: [`docs/architecture.png`](docs/architecture.png)</sub>

## Why this exists

The infra roles I want are scattered. Some are on MyCareersFuture. Others appear only on company ATS boards (Greenhouse, Ashby, Lever, Workday) and never reach a job board. Checking all of them by hand every few days is slow, and it's easy to miss a posting. Auto-apply bots have the opposite problem: they send low-quality applications without asking you. This project does the tedious part (finding, filtering, ranking, drafting) and leaves the decision to apply with a person.

## Highlights

- **The human stays in control, by design.** The agent can only write local `.docx` drafts and send one Telegram message. No code path submits an application. Every alert ends with "Nothing was submitted — your call." (`build/nas_agent.py`)
- **The LLM can only pick from real data.** The verdict is enforced by `claude -p --json-schema`, and the schema's project list is an `enum` of the real project library, re-checked in code (`judge()` in `build/nas_agent.py`). The prompt also forbids inventing employers, numbers, certifications or years.
- **Deterministic filters run before the LLM.** Regexes cut role shape, seniority, pay floor, excluded employers and public-sector/defence work before a single token is spent (`build/weekly_sweep.py`). The model only sees candidates that already pass. That keeps cost down and makes the hard rules auditable.
- **Cost is capped and nothing gets lost.** `MAX_PER_CYCLE` (default 8) limits LLM calls per cycle. Postings over the cap, and any whose judge call fails, stay *unseen* and are retried next cycle rather than dropped. The first run only records a baseline, so turning it on doesn't flood you with every live posting.
- **Coverage beyond scraping, without scraping.** LinkedIn, JobStreet, Glassdoor and Indeed stay login-walled to a crawler, but they all email you their alerts. With `IMAP_*` set, the watcher reads those emails read-only (`BODY.PEEK`, nothing marked read), has a cheap model (`EXTRACT_MODEL`, default `haiku`) list the postings in each, and runs them through the same regex filters and judge (`build/mail_alerts.py`).
- **Built to get a reply, not just a draft.** Each fit comes with who to contact (likely hiring-manager titles plus a LinkedIn people-search link) and a ≤280-character connection note, also saved as `outreach.md`. Scores ≥ `URGENT_SCORE` are flagged "🔥 Apply today", and sweeps run every `BUSY_INTERVAL_HOURS` (2h) during weekday working hours so you see postings early.
- **Outcomes close the loop.** Every fit gets a ref number. `/applied 12`, `/interview 12`, `/rejected 12`… record what happened; `FOLLOWUP_DAYS` after `/applied` with no update you get a nudge with a drafted follow-up email; `/interview` builds a prep pack; a Sunday digest reports reply rate by source and whether `FIT_THRESHOLD` looks miscalibrated.
- **The model can read, not act.** Every `claude -p` call runs with `--tools ""`, `--strict-mcp-config`, `--setting-sources ""` and an empty temp directory as its working directory. A job posting that tries prompt injection has no tools and no project files (or `.env`) within reach.
- **Failures are classified.** A CLI, auth, rate-limit or network failure (`ClaudeError`) is transient and retried next cycle forever. A reply without a valid structured verdict counts toward `MAX_ATTEMPTS`.
- **The self-test checks behaviour, not syntax.** A `\b` written through a shell heredoc once collapsed into a literal backspace byte, and a filter silently matched nothing. `build/selftest.py` asserts 14 filter behaviours and scans the source for any C0 control character.
- **Personal data stays out of git by default.** `.gitignore` is a *whitelist*: resume content (`content.py`), personal filters (`my_profile.py`), state, `.env` and generated documents are private unless someone deliberately publishes them. Example stand-ins (`*_example.py`) keep the repo runnable.

## How it works

Numbers match the diagram.

1. **Sweep.** `weekly_sweep.collect()` queries the MyCareersFuture public API (20 search terms × 4 pages). It's the only source that publishes a salary band and a minimum-years figure. `job_sources.fetch_all()` adds company boards: 26 Greenhouse, 6 Ashby, 7 Lever, plus amazon.jobs, Workday tenants, NVIDIA's Workday site (Singapore facet) and, optionally, Apple via headless Playwright. The regex filters and the pay floor are applied here.
2. **Dedup.** Each posting's ID is checked against `build/.nas_state.json`. Only IDs not seen before continue.
3. **Cap.** At most `MAX_PER_CYCLE` new postings go to the model.
4. **Judge.** `judge()` fetches the job description (MCF only; career pages are judged on title and company) and calls `claude -p --output-format json --json-schema <verdict schema>`. The verdict covers: suitable, 0–100 score, reason, honest gaps, coding-test risk, tagline, summary, project picks and cover-letter paragraphs.
5. **Inference.** The Claude Code CLI in the container calls Claude (default `sonnet`), authenticated with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (subscription) or `ANTHROPIC_API_KEY`. Each run records its `total_cost_usd`.
6. **Gate.** The posting has to be `suitable`, score ≥ `FIT_THRESHOLD` (70), and come with a letter.
7. **Draft.** `build_docs.py` renders an ATS-plain resume and cover letter plus a `fit.md` into `tailored-auto/<date_company_role>/`.
8. **Alert.** One Telegram message goes to a forum topic with the role, pay, score, reasons, gaps, URL, who to reach out to with a drafted note, the path to the draft folder and a ref number.
9. **Human decides.** You apply (or not) and record it with `/applied <ref>`; follow-up nudges, prep packs and the weekly digest build on that. Nothing is ever sent or submitted for you.

Job-alert emails (when `IMAP_*` is set) join the candidate list at step 1 and go through the same steps 2–9.

## Tech stack

| Layer | Tech |
|---|---|
| Language | Python 3.12, stdlib `urllib` (no HTTP client dependency) |
| Documents | `python-docx`: single-column, no tables or text boxes, real bullet lists (ATS-friendly) |
| LLM | Claude via the Claude Code CLI (`claude -p`, `--json-schema` structured output, tools disabled) |
| Sources | MyCareersFuture API, Greenhouse / Ashby / Lever / Workday JSON endpoints, amazon.jobs, Playwright (Apple, optional) |
| Alerts | Telegram Bot API (forum topics via `message_thread_id`) |
| Runtime | Docker Compose on a home NAS (`python:3.12-slim-bookworm` + Node 22 + `@anthropic-ai/claude-code`, `restart: unless-stopped`) |
| State | JSON file (`build/.nas_state.json`) |

## Getting started

```bash
pip install python-docx
npm install -g @anthropic-ai/claude-code             # then `claude` once to sign in, or set CLAUDE_CODE_OAUTH_TOKEN
cp build/content_example.py build/content.py        # your resume (gitignored)
cp build/my_profile_example.py build/my_profile.py  # pay floor + excluded employers (gitignored)
python build/selftest.py
python build/nas_agent.py --once                    # one cycle; omit --once to loop
```

Environment (put the secrets in `.env`, which is never committed):

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude login for the container (`claude setup-token`); or set `ANTHROPIC_API_KEY` |
| `JOB_MODEL` | `sonnet` | Any `claude --model` value (`haiku`, `sonnet`, `opus` or a full model ID) |
| `CLAUDE_BIN` | `claude` | Path to the CLI |
| `CLAUDE_TIMEOUT` | `300` | Seconds per `claude -p` call |
| `TELEGRAM_BOT_TOKEN` | — | Bot token (if unset, alerts are printed to stdout instead) |
| `TELEGRAM_CHAT_ID` | — | Target chat |
| `TELEGRAM_THREAD_ID` | — | Optional forum topic |
| `SWEEP_INTERVAL_HOURS` | `6` | Loop interval outside working hours |
| `BUSY_INTERVAL_HOURS` | `2` | Loop interval Mon–Fri during `BUSY_HOURS` |
| `BUSY_HOURS` | `8-20` | Local working hours for the faster interval |
| `URGENT_SCORE` | `85` | Alerts at or above this say "🔥 Apply today" |
| `FOLLOWUP_DAYS` | `7` | Days after `/applied` before a follow-up nudge |
| `DIGEST_WEEKDAY` | `6` | Day for the weekly digest (Monday=0, Sunday=6), after 09:00 |
| `EXTRACT_MODEL` | `haiku` | Model that lists the jobs in an alert email |
| `IMAP_USER` / `IMAP_PASSWORD` | — | Mailbox holding your job alerts; for Gmail use an [app password](https://myaccount.google.com/apppasswords) |
| `IMAP_HOST` / `IMAP_FOLDER` | `imap.gmail.com` / `INBOX` | Where to look |
| `MAIL_FROM` | LinkedIn, JobStreet, Glassdoor, Indeed senders | Comma-separated sender substrings to read |
| `MAIL_DAYS` | `3` | How far back to look each sweep |
| `MAX_PER_CYCLE` | `8` | Max LLM judgements per cycle |
| `FIT_THRESHOLD` | `70` | Minimum score before drafting and alerting |
| `HEARTBEAT_MIN` | `30` | How often the idle loop rewrites the heartbeat in the state file |
| `MAX_ATTEMPTS` | `3` | Cycles a posting may fail with unparseable model output before it's given up |
| `SEEN_DAYS` | `180` | Postings older than this are forgotten, keeping the state file small |

### Telegram commands

Between cycles the agent long-polls the bot and answers messages from `TELEGRAM_CHAT_ID` only:

| Command | What it does |
|---|---|
| `/status` | Heartbeat, last cycle, `claude` CLI version, last 5 runs with cost, last error. No LLM call. |
| `/ask <question>` | Asks the model, with the recently judged postings, recent drafts and system status as context. |
| `/judge <url> [pasted text]` | Judges one posting on demand and drafts the resume + cover letter if it fits. MyCareersFuture links are read through the API; for login-walled boards (LinkedIn etc.) paste the job description after the URL. |
| `/applied`, `/interview`, `/offer`, `/rejected`, `/ghosted` `<ref or text>` | Records an outcome for the alert with that ref, or for "Role at Company" you found elsewhere. `/interview` also replies with an interview prep pack (saved as `prep.md`). |
| `/pipeline` | Every tracked application and its status. |
| `/sweep` | Runs a cycle now. |
| `/help` | Lists the commands. |

Only one process may call `getUpdates` per bot token, so nothing else should poll this bot.

### Health

Every cycle, including one with nothing new, writes `last_cycle` and a `runs` entry to `build/.nas_state.json`. The idle loop also refreshes `heartbeat` every `HEARTBEAT_MIN` minutes, and the compose healthcheck marks the container unhealthy once that stamp is over 2h old. If every judge call in a cycle fails (usually the Claude login expired or a usage limit was hit), you get a Telegram warning instead of silent retries.

- **Restarts don't re-sweep.** On start the loop waits until the next cycle is due, based on `last_cycle`, so a NAS reboot doesn't trigger a paid sweep. `/sweep` still forces one.
- **Retries are bounded.** A posting whose verdict can't be parsed is given up after `MAX_ATTEMPTS` cycles. CLI, auth and network errors never count, so nothing is lost to an outage.
- **Watchdog on the PC.** `build/watchdog.py` reads the Syncthing copy of the state file and messages Telegram once when the watcher goes quiet (heartbeat over `WATCHDOG_MAX_H`, default 2h), when it's alive but sweeps are stuck (no finished cycle in 2 × interval + 1h), or when the last cycle crashed. It sends one 🟢 message on recovery. Schedule `python build\watchdog.py` every 15 minutes in Task Scheduler; set `JOB_STATE` if the synced file isn't at `build/.nas_state.json`.

### On a NAS (Docker)

`deploy/nas/compose.yaml` builds an image from `deploy/nas/Dockerfile` (Python 3.12 + `python-docx` + Node 22 + the Claude Code CLI, installed once at build time) and mounts the project folder. Put `compose.yaml`, `Dockerfile`, `.env` and `build/` in the share and create the project. After a code-only change, copy `build/*.py` and restart; rebuild only when the Dockerfile changes. Put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token` on your PC) in `.env`; the container needs outbound internet to reach Claude.

## Project structure

```
build/
  nas_agent.py          # the loop: sweep -> dedup -> judge -> draft -> alert
  weekly_sweep.py       # MyCareersFuture sweep + all regex filters
  job_sources.py        # Greenhouse / Ashby / Lever / amazon.jobs / Workday / NVIDIA / Apple
  mail_alerts.py        # read-only IMAP reader for LinkedIn / JobStreet / Glassdoor / Indeed alert emails
  build_docs.py         # ATS-plain resume + cover letter (.docx)
  selftest.py           # behaviour checks + control-character scan
  test_nas_agent.py     # offline tests: cycles, retries, commands, tracking, alert emails, watchdog
  watchdog.py           # PC-side: alerts when the NAS stops reporting
  *_example.py          # public stand-ins for the gitignored personal files
deploy/nas/            # compose.yaml + Dockerfile for the NAS
.github/workflows/      # CI: both test scripts on every push
docs/architecture.*     # diagram (draw.io source, SVG, PNG)
```

## Testing & quality

- `python build/selftest.py` runs 14 behaviour checks on the filters (e.g. "GovTech is dropped", "Ellipsys is *not* dropped", "Remote-USA is rejected, APAC accepted") and scans every `build/*.py` for stray control characters. Current result: `ok - 14 behaviour checks pass, 10 files clean of control chars`.
- `python build/test_nas_agent.py` runs 61 offline checks: the NAS loop (baseline, quiet cycles, retry cap, pruning, restart scheduling, busy-hours cadence), every Telegram command, outcome tracking, follow-ups and the digest, alert-email parsing over a fake IMAP server, and the watchdog. Telegram, `claude -p`, IMAP and MyCareersFuture are faked.
- GitHub Actions runs both on every push and also builds the NAS image and runs the tests inside it (`.github/workflows/selftest.yml`).

## Design decisions & limitations

- **No login-walled scraping.** LinkedIn, Glassdoor, NodeFlair and JobStreet sit behind logins or bot checks, and this project doesn't try to get around them. Their alert *emails* are read instead; those carry title, company, location and sometimes pay, but not the full description, so they're judged like career-page postings (paste the JD into `/judge` for a full read).
- **Career-page postings have no job description in the prompt.** They're judged on title and company only, so those scores are less reliable than the MCF ones.
- **JSON-file state, single instance.** That's fine for one container on one NAS. Running more than one would need a real store with locking.
- **The regex filters are Singapore- and profile-specific.** They're tuned for one person's search, not written as a general-purpose product.
- **Workday tenants can't be guessed.** Only tenants with verified site IDs are included. The others returned 404/422.
- Roadmap ideas: pull JDs from ATS APIs where available, and track the outcome of each application to calibrate the threshold.

---

James Koh · [GitHub](https://github.com/gcjk768)
