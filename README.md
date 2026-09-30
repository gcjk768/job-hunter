# job-hunter

A job-search agent that runs 24/7 on my home NAS. It sweeps Singapore job boards and company career pages, has an LLM judge how well each new posting fits, drafts a tailored resume and cover letter for the good matches, and sends me a Telegram alert. **It never applies to anything.** I read every draft and decide myself.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama-000000?logo=ollama&logoColor=white)
![Telegram](https://img.shields.io/badge/Alerts-Telegram-26A5E4?logo=telegram&logoColor=white)
![Self-test](https://img.shields.io/badge/selftest-14%20checks-brightgreen)

![Architecture](docs/architecture.drawio.svg)

<sub>Editable source: [`docs/architecture.drawio`](docs/architecture.drawio) · PNG fallback: [`docs/architecture.png`](docs/architecture.png)</sub>

## Why this exists

The infra roles I want are scattered. Some are on MyCareersFuture. Others appear only on company ATS boards (Greenhouse, Ashby, Lever, Workday) and never reach a job board. Checking all of them by hand every few days is slow, and it's easy to miss a posting. Auto-apply bots have the opposite problem: they send low-quality applications without asking you. This project does the tedious part (finding, filtering, ranking, drafting) and leaves the decision to apply with a person.

## Highlights

- **The human stays in control, by design.** The agent can only write local `.docx` drafts and send one Telegram message. No code path submits an application. Every alert ends with "Nothing was submitted — your call." (`build/nas_agent.py`)
- **The LLM can only pick from real data.** It returns a strict JSON verdict (`format: json`). The projects it chooses are checked against the real project library, and any name it invents is dropped (`judge()` in `build/nas_agent.py`). The prompt also forbids inventing employers, numbers, certifications or years.
- **Deterministic filters run before the LLM.** Regexes cut role shape, seniority, pay floor, excluded employers and public-sector/defence work before a single token is spent (`build/weekly_sweep.py`). The model only sees candidates that already pass. That keeps cost down and makes the hard rules auditable.
- **Cost is capped and nothing gets lost.** `MAX_PER_CYCLE` (default 8) limits LLM calls per cycle. Postings over the cap, and any whose judge call fails, stay *unseen* and are retried next cycle rather than dropped. The first run only records a baseline, so turning it on doesn't flood you with every live posting.
- **One copy of the cloud credential.** The container joins the Docker network of an Ollama container that's already signed in (`http://trading-ollama:11434`), so the Ollama Cloud key is never copied a second time (`deploy/nas/compose.yaml`).
- **`think: false` on every call.** With `format: json`, reasoning models otherwise spend the whole reply on thinking and return empty content. Found while testing against a local `qwen3.6`.
- **The self-test checks behaviour, not syntax.** A `\b` written through a shell heredoc once collapsed into a literal backspace byte, and a filter silently matched nothing. `build/selftest.py` asserts 14 filter behaviours and scans the source for any C0 control character.
- **Personal data stays out of git by default.** `.gitignore` is a *whitelist*: resume content (`content.py`), personal filters (`my_profile.py`), state, `.env` and generated documents are private unless someone deliberately publishes them. Example stand-ins (`*_example.py`) keep the repo runnable.

## How it works

Numbers match the diagram.

1. **Sweep.** `weekly_sweep.collect()` queries the MyCareersFuture public API (20 search terms × 4 pages). It's the only source that publishes a salary band and a minimum-years figure. `job_sources.fetch_all()` adds company boards: 26 Greenhouse, 6 Ashby, 7 Lever, plus amazon.jobs, Workday tenants, NVIDIA's Workday site (Singapore facet) and, optionally, Apple via headless Playwright. The regex filters and the pay floor are applied here.
2. **Dedup.** Each posting's ID is checked against `build/.nas_state.json`. Only IDs not seen before continue.
3. **Cap.** At most `MAX_PER_CYCLE` new postings go to the model.
4. **Judge.** `judge()` fetches the job description (MCF only; career pages are judged on title and company) and calls Ollama `/api/chat` with `format=json, think=false`. The verdict covers: suitable, 0–100 score, reason, honest gaps, coding-test risk, tagline, summary, project picks and cover-letter paragraphs.
5. **Inference.** The shared Ollama container forwards the call to Ollama Cloud (default model `deepseek-v4.1-flash:cloud`).
6. **Gate.** The posting has to be `suitable`, score ≥ `FIT_THRESHOLD` (70), and come with a letter.
7. **Draft.** `build_docs.py` renders an ATS-plain resume and cover letter plus a `fit.md` into `tailored-auto/<date_company_role>/`.
8. **Alert.** One Telegram message goes to a forum topic with the role, pay, score, reasons, gaps, URL and the path to the draft folder.
9. **Human decides.** Nothing else happens automatically.

## Tech stack

| Layer | Tech |
|---|---|
| Language | Python 3.12, stdlib `urllib` (no HTTP client dependency) |
| Documents | `python-docx`: single-column, no tables or text boxes, real bullet lists (ATS-friendly) |
| LLM | Ollama `/api/chat` with JSON mode, via Ollama Cloud |
| Sources | MyCareersFuture API, Greenhouse / Ashby / Lever / Workday JSON endpoints, amazon.jobs, Playwright (Apple, optional) |
| Alerts | Telegram Bot API (forum topics via `message_thread_id`) |
| Runtime | Docker Compose on a home NAS (`python:3.12-slim`, `restart: unless-stopped`) |
| State | JSON file (`build/.nas_state.json`) |

## Getting started

```bash
pip install python-docx
cp build/content_example.py build/content.py        # your resume (gitignored)
cp build/my_profile_example.py build/my_profile.py  # pay floor + excluded employers (gitignored)
python build/selftest.py
python build/nas_agent.py --once                    # one cycle; omit --once to loop
```

Environment (put the secrets in `.env`, which is never committed):

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `JOB_MODEL` | `deepseek-v4.1-flash:cloud` | Model used as the fit judge |
| `TELEGRAM_BOT_TOKEN` | — | Bot token (if unset, alerts are printed to stdout instead) |
| `TELEGRAM_CHAT_ID` | — | Target chat |
| `TELEGRAM_THREAD_ID` | — | Optional forum topic |
| `SWEEP_INTERVAL_HOURS` | `6` | Loop interval |
| `MAX_PER_CYCLE` | `8` | Max LLM judgements per cycle |
| `FIT_THRESHOLD` | `70` | Minimum score before drafting and alerting |
| `HEARTBEAT_MIN` | `30` | How often the idle loop rewrites the heartbeat in the state file |
| `MAX_ATTEMPTS` | `3` | Cycles a posting may fail with unparseable model output before it's given up |
| `SEEN_DAYS` | `180` | Postings older than this are forgotten, keeping the state file small |

### Telegram commands

Between cycles the agent long-polls the bot and answers messages from `TELEGRAM_CHAT_ID` only:

| Command | What it does |
|---|---|
| `/status` | Heartbeat, last cycle, Ollama reachability, last 5 runs, last error. No LLM call. |
| `/ask <question>` | Asks the model, with the recently judged postings, recent drafts and system status as context. |
| `/judge <url> [pasted text]` | Judges one posting on demand and drafts the resume + cover letter if it fits. MyCareersFuture links are read through the API; for login-walled boards (LinkedIn etc.) paste the job description after the URL. |
| `/sweep` | Runs a cycle now. |
| `/help` | Lists the commands. |

Only one process may call `getUpdates` per bot token, so nothing else should poll this bot.

### Health

Every cycle, including one with nothing new, writes `last_cycle` and a `runs` entry to `build/.nas_state.json`. The idle loop also refreshes `heartbeat` every `HEARTBEAT_MIN` minutes, and the compose healthcheck marks the container unhealthy once that stamp is over 2h old. If every judge call in a cycle fails (usually because `trading-ollama` is down), you get a Telegram warning instead of silent retries.

- **Restarts don't re-sweep.** On start the loop waits until the next cycle is due, based on `last_cycle`, so a NAS reboot doesn't trigger a paid sweep. `/sweep` still forces one.
- **Retries are bounded.** A posting whose verdict can't be parsed is given up after `MAX_ATTEMPTS` cycles. Network errors (Ollama down) never count, so nothing is lost to an outage.
- **Watchdog on the PC.** `build/watchdog.py` reads the Syncthing copy of the state file and messages Telegram once when the watcher goes quiet (heartbeat over `WATCHDOG_MAX_H`, default 2h), when it's alive but sweeps are stuck (no finished cycle in 2 × interval + 1h), or when the last cycle crashed. It sends one 🟢 message on recovery. Schedule `python build\watchdog.py` every 15 minutes in Task Scheduler; set `JOB_STATE` if the synced file isn't at `build/.nas_state.json`.

### On a NAS (Docker)

`deploy/nas/compose.yaml` builds a small image from `deploy/nas/Dockerfile` (`python:3.12-slim` + `python-docx`, installed once at build time) and mounts the project folder. Put `compose.yaml`, `Dockerfile`, `.env` and `build/` in the share and create the project. After a code-only change, copy `build/*.py` and restart; rebuild only when the Dockerfile changes. It expects an existing Ollama container on an external Docker network, so change `networks.desk.name` and `OLLAMA_BASE_URL` to match your setup.

## Project structure

```
build/
  nas_agent.py          # the loop: sweep -> dedup -> judge -> draft -> alert
  weekly_sweep.py       # MyCareersFuture sweep + all regex filters
  job_sources.py        # Greenhouse / Ashby / Lever / amazon.jobs / Workday / NVIDIA / Apple
  build_docs.py         # ATS-plain resume + cover letter (.docx)
  selftest.py           # behaviour checks + control-character scan
  test_nas_agent.py     # offline tests: cycles, retries, Telegram commands, watchdog
  watchdog.py           # PC-side: alerts when the NAS stops reporting
  *_example.py          # public stand-ins for the gitignored personal files
deploy/nas/            # compose.yaml + Dockerfile for the NAS
.github/workflows/      # CI: both test scripts on every push
docs/architecture.*     # diagram (draw.io source, SVG, PNG)
```

## Testing & quality

- `python build/selftest.py` runs 14 behaviour checks on the filters (e.g. "GovTech is dropped", "Ellipsys is *not* dropped", "Remote-USA is rejected, APAC accepted") and scans every `build/*.py` for stray control characters. Current result: `ok - 14 behaviour checks pass, 7 files clean of control chars`.
- `python build/test_nas_agent.py` runs offline checks of the NAS loop (baseline, quiet cycles, retry cap, pruning, restart scheduling), every Telegram command and the watchdog, with Telegram, Ollama and MyCareersFuture faked.
- GitHub Actions runs both on every push (`.github/workflows/selftest.yml`).

## Design decisions & limitations

- **No login-walled sources.** LinkedIn, Glassdoor, NodeFlair and JobStreet sit behind logins or bot checks. This project doesn't try to get around them; they stay a manual pass.
- **Career-page postings have no job description in the prompt.** They're judged on title and company only, so those scores are less reliable than the MCF ones.
- **JSON-file state, single instance.** That's fine for one container on one NAS. Running more than one would need a real store with locking.
- **The regex filters are Singapore- and profile-specific.** They're tuned for one person's search, not written as a general-purpose product.
- **Workday tenants can't be guessed.** Only tenants with verified site IDs are included. The others returned 404/422.
- Roadmap ideas: pull JDs from ATS APIs where available, and track the outcome of each application to calibrate the threshold.

---

James Koh · [GitHub](https://github.com/gcjk768)
