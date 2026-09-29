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

### On a NAS (Docker)

`deploy/nas/compose.yaml` runs the agent on `python:3.12-slim` with the project folder mounted. Put `compose.yaml`, `.env` and `build/` in the share and create the project. It expects an existing Ollama container on an external Docker network, so change `networks.desk.name` and `OLLAMA_BASE_URL` to match your setup.

## Project structure

```
build/
  nas_agent.py          # the loop: sweep -> dedup -> judge -> draft -> alert
  weekly_sweep.py       # MyCareersFuture sweep + all regex filters
  job_sources.py        # Greenhouse / Ashby / Lever / amazon.jobs / Workday / NVIDIA / Apple
  build_docs.py         # ATS-plain resume + cover letter (.docx)
  selftest.py           # behaviour checks + control-character scan
  *_example.py          # public stand-ins for the gitignored personal files
deploy/nas/compose.yaml # NAS deployment
docs/architecture.*     # diagram (draw.io source, SVG, PNG)
```

## Testing & quality

- `python build/selftest.py` runs 14 behaviour checks on the filters (e.g. "GovTech is dropped", "Ellipsys is *not* dropped", "Remote-USA is rejected, APAC accepted") and scans every `build/*.py` for stray control characters. Current result: `ok - 14 behaviour checks pass, 7 files clean of control chars`.
- There's no unit-test suite or CI yet. The self-test is the gate before trusting a sweep.

## Design decisions & limitations

- **No login-walled sources.** LinkedIn, Glassdoor, NodeFlair and JobStreet sit behind logins or bot checks. This project doesn't try to get around them; they stay a manual pass.
- **Career-page postings have no job description in the prompt.** They're judged on title and company only, so those scores are less reliable than the MCF ones.
- **JSON-file state, single instance.** That's fine for one container on one NAS. Running more than one would need a real store with locking.
- **The regex filters are Singapore- and profile-specific.** They're tuned for one person's search, not written as a general-purpose product.
- **Workday tenants can't be guessed.** Only tenants with verified site IDs are included. The others returned 404/422.
- Roadmap ideas: pull JDs from ATS APIs where available, add a unit-test suite and CI, and track the outcome of each application to calibrate the threshold.

---

James Koh · [GitHub](https://github.com/gcjk768)
