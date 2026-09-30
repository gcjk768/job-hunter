"""Weekly job sweep across every source that works without a browser session.

Sources: the MyCareersFuture public jobs API (salary bands + minimum years of experience),
plus company ATS boards via build/job_sources.py (Greenhouse, Ashby, Lever, amazon.jobs) for
the employers whose infrastructure roles never reach a job board.

Writes a dated note into docs/vault/ listing DevOps / SRE / Platform roles that clear the
salary floor, with the postings that are new since the previous run called out first.

MyCareersFuture leads because it is the only source that publishes a real salary band and a
minimum-years-of-experience figure per posting, with no authentication. LinkedIn, Apple and
Glassdoor need a logged-in browser session, so that half runs as the agentic pass in
.claude/commands/job-sweep.md instead.

Run:  python build/weekly_sweep.py
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_sources

# Personal values (pay floor, excluded employers) live in the gitignored my_profile.py; the
# public repo ships my_profile_example.py. Same for commute.py, which is a home-specific MRT table.
try:
    import my_profile as profile
except ImportError:
    import my_profile_example as profile
try:
    import commute
except ImportError:
    commute = None

API = "https://api.mycareersfuture.gov.sg/v2/jobs"
ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "docs" / "vault"
STATE_FILE = ROOT / "build" / ".sweep_state.json"

# --- the filters from docs/vault/Target Criteria.md -------------------------------------

SALARY_FLOOR = profile.SALARY_FLOOR  # SGD/month base, judged on the bottom of a posted band

SEARCHES = [
    "devops engineer",
    "site reliability engineer",
    "platform engineer",
    "devsecops",
    "cloud infrastructure engineer",
    # AI-infrastructure side: the self-hosted LLM, agentic-CI and MLSecOps work is the rarest
    # thing on the resume, and these titles are where it is the main qualification.
    "ai infrastructure engineer",
    "mlops engineer",
    "machine learning infrastructure",
    "llm platform engineer",
    "gpu infrastructure",
    # adjacent titles that carry the same work under a different name
    "observability engineer",
    "cloud operations engineer",
    "systems engineer linux",
    "reliability engineering",
    # Added 2026-09-13 on James's instruction, after the NVIDIA Solutions Architect postings.
    # These are a *different track* — solution architecture is customer-facing and often pre-sales,
    # which is why filter 5 excluded it until now. Surfaced deliberately, not by accident: the
    # ranker flags them, it does not recommend them. See the NVIDIA section in [[Job Search Tracker]].
    "solution architect",
    "solutions architect",
    "ai solution architect",
    "cloud solution architect",
    # AIOps — ops automation driven by ML, the closest thing to the agentic-CI work under a
    # market-recognised name.
    "aiops",
    "aiops engineer",
]
# Sources that publish no salary (company career pages) get their own section rather than
# being judged against the floor — the whole point of watching them is that the role is rare.
CAREER_PAGE_SOURCES = {"Greenhouse", "Ashby", "Lever", "amazon.jobs", "Workday", "Apple (Playwright)"}
PAGES_PER_SEARCH = 4
PAGE_SIZE = 30

# Filter 5 — role shape. A posting has to look like one of these.
TITLE_KEEP = re.compile(
    r"devops|devsecops|\bsre\b|site reliability|platform engineer|platform & |platform and |"
    r"cloud engineer|cloud infrastructure|infrastructure engineer|release engineer|"
    r"reliability engineer|kubernetes|ci ?/ ?cd|"
    r"mlops|llmops|ai infrastructure|ai infra|ml infrastructure|machine learning infrastructure|"
    r"ai platform|ml platform|llm platform|model platform|gpu|"
    r"observability|cloud operations|cloud ops|systems engineer|automation engineer|"
    # Added 2026-09-13. Architect titles were blanket-dropped below until now; they are admitted
    # here by *specific* prefix only, so "Enterprise Architect" and "Data Architect" still fail the
    # whitelist and never reach the list.
    r"aiops|ai ?ops\b|"
    r"solutions? architect|cloud architect|infrastructure architect|platform architect|"
    r"ai architect|ai solutions? architect",
    re.I,
)
# ...and not like one of these. "Software engineer" routes into an algorithmic loop (filter 4);
# the rest are the wrong level or the wrong job.
#
# 2026-09-13: bare `architect`, `pre-?sales` and bare `sales` came out. TITLE_KEEP is a whitelist,
# so removing them does not open the floodgates — only the architect titles named there get in.
# Bare `sales` had to go with them, or "Pre-Sales Solutions Architect" would be dropped by the very
# word that makes it the role James asked to see; the narrower pattern below still kills the
# account-executive titles.
TITLE_DROP = re.compile(
    r"fresh graduate|fresh grad|no experience|entry level|intern\b|internship|traineeship|"
    r"junior|apprentice|project manager|business development|account manager|"
    r"sales (?:manager|executive|representative|rep|director|lead)|inside sales|account executive|"
    r"director|head of|resident engineer|technician|support engineer|"
    # Admitting architect titles let in 132 rows, most of them enterprise-application work that has
    # nothing to do with the NVIDIA-shaped role James asked for. These are the packaged-software and
    # single-language architect titles — a different profession that happens to share the noun.
    r"\bsap\b|servicenow|salesforce|sharepoint|mulesoft|\bm365\b|microsoft 365|power platform|"
    r"copilot studio|\bgis\b|\bcrm\b|\berp\b|peoplesoft|\bjava\b|\bdotnet\b|\b\.net\b|"
    r"murex|temenos|finacle|\bsas\b|informatica|mainframe",
    re.I,
)

# Filters 2 and 3 — personally excluded employers (from my_profile), government and defence accounts.
COMPANY_DROP = re.compile(
    profile.EXCLUDED_EMPLOYERS + r"|"
    r"GOVTECH|GOVERNMENT TECHNOLOGY|\bCSIT\b|CENTRE FOR STRATEGIC INFOCOMM|"
    r"\bDSTA\b|DEFENCE SCIENCE|MINDEF|SYNAPXE|THALES|ST ENGINEERING|SINGAPORE POOLS|"
    r"HOME TEAM SCIENCE",
    re.I,
)

UA = {"User-Agent": "Mozilla/5.0 (weekly-job-sweep; personal use)"}

# Filter 2 is about the *work*, not the employer: an SI role staffed onto a public-sector account is
# excluded even though the employer is private. The employer name cannot tell you that, but the job
# description usually can.
PUBLIC_SECTOR = re.compile(
    r"\bgovernment\b|\bgovt\b|public sector|public agency|statutory board|whole-of-government|"
    r"\bWOG\b|\bGovTech\b|\bIHiS\b|\bMINDEF\b|\bSAF\b|\bdefence\b|\bdefense\b|"
    r"security clearance|\bCSIT\b|\bDSTA\b|\bHTX\b|\bministry of\b|national agency",
    re.I,
)


def sector_of(job: dict) -> tuple[str, str | None, bool]:
    """(industry, what the company actually does, is this public-sector work?)

    Never returns "not stated" when the answer is recoverable: the posting's own categories come
    first, and where the employer left them blank the company's registered SSIC activity is the
    real industry anyway. Only a posting with neither gets a dash."""
    cats = [c.get("category", "") for c in (job.get("categories") or [])]
    industry = " / ".join(c for c in cats if c)
    ssic = ((job.get("hiringCompany") or job.get("postedCompany") or {})
            .get("ssicDescription2020") or "").title().strip() or None
    if not industry and ssic:
        # e.g. "Information Technology Consultancy (Except Cybersecurity)" -> "IT Consultancy"
        industry = re.sub(r"\s*\([^)]*\)", "", ssic).strip()
    text = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    return industry or "", ssic, bool(PUBLIC_SECTOR.search(text))


def fetch(search: str, page: int) -> dict:
    qs = urllib.parse.urlencode(
        {"search": search, "limit": PAGE_SIZE, "page": page, "sortBy": "new_posting_date"}
    )
    req = urllib.request.Request(f"{API}?{qs}", headers=UA)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)


def monthly_salary(job: dict) -> tuple[int | None, int | None]:
    """Return (min, max) normalised to SGD/month, or (None, None) when not published."""
    sal = job.get("salary") or {}
    lo, hi = sal.get("minimum"), sal.get("maximum")
    if lo is None and hi is None:
        return None, None
    kind = ((sal.get("type") or {}).get("salaryType") or "Monthly").lower()
    if kind.startswith("annual"):
        lo = round(lo / 12) if lo else lo
        hi = round(hi / 12) if hi else hi
    return lo, hi


def company_of(job: dict) -> str:
    for key in ("hiringCompany", "postedCompany"):
        c = job.get(key) or {}
        if c.get("name"):
            return c["name"].strip()
    return "(employer not named)"


# Agencies rarely name the client: of 207 postings scanned on 2026-09-11, exactly 2 filled in
# `hiringCompany`. The description sometimes *describes* one instead ("Our client is a global
# technology, defence and engineering group..."), which is enough to identify the employer — and
# occasionally enough to disqualify it, since that example is a defence group under filter 2.
CLIENT_HINT = re.compile(
    r"(our client(?:\s+is)?[,:]?\s+(?:an?|the)\s[^.]{25,170})", re.I
)
BOILERPLATE = re.compile(
    r"clients and partners|clients' brands|shareholders|are empowered|suitability for job", re.I
)


def client_of(job: dict) -> tuple[str | None, str | None]:
    """Return (named client, described client) where the posting gives either."""
    named = None
    hiring = job.get("hiringCompany") or {}
    posted = (job.get("postedCompany") or {}).get("name", "")
    if hiring.get("name") and hiring["name"].strip().lower() != posted.strip().lower():
        named = hiring["name"].strip()

    described = None
    text = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    match = CLIENT_HINT.search(text)
    if match and not BOILERPLATE.search(match.group(1)):
        described = re.sub(r"\s+", " ", match.group(1)).strip()
    return named, described


def collect() -> dict[str, dict]:
    """uuid -> normalised record, deduped across the searches."""
    found: dict[str, dict] = {}
    for search in SEARCHES:
        for page in range(PAGES_PER_SEARCH):
            try:
                data = fetch(search, page)
            except (OSError, ValueError) as exc:  # URLError, timeouts, dropped connections, non-JSON replies
                print(f"  ! {search} p{page}: {exc}", file=sys.stderr)
                break
            results = data.get("results") or []
            if not results:
                break
            for job in results:
                uuid = job.get("uuid")
                if not uuid or uuid in found:
                    continue
                title = (job.get("title") or "").strip()
                company = company_of(job)
                if not TITLE_KEEP.search(title) or TITLE_DROP.search(title):
                    continue
                if COMPANY_DROP.search(company):
                    continue
                lo, hi = monthly_salary(job)
                meta = job.get("metadata") or {}
                named_client, described_client = client_of(job)
                addr = job.get("address") or {}
                districts = addr.get("districts") or []
                region = districts[0].get("region") if districts else None
                km = commute.distance(addr.get("lat"), addr.get("lng")) if commute else None
                minutes, hub = (commute.travel_minutes(addr.get("lat"), addr.get("lng"))
                                if commute else (None, None))
                industry, ssic, public = sector_of(job)
                found[uuid] = {
                    "uuid": uuid,
                    "title": title,
                    "company": company,
                    "years": job.get("minimumYearsExperience"),
                    "lo": lo,
                    "hi": hi,
                    "types": ", ".join(
                        t.get("employmentType", "") for t in (job.get("employmentTypes") or [])
                    )
                    or "-",
                    "levels": ", ".join(
                        p.get("position", "") for p in (job.get("positionLevels") or [])
                    )
                    or "-",
                    "posted": meta.get("newPostingDate") or meta.get("originalPostingDate") or "",
                    "url": meta.get("jobDetailsUrl") or "",
                    "applications": meta.get("totalNumberJobApplication"),
                    "source": "MyCareersFuture",
                    "posted_by": (job.get("postedCompany") or {}).get("name", "").strip(),
                    "client": named_client,
                    "client_hint": described_client,
                    "region": region,
                    "building": (addr.get("building") or addr.get("street") or "").title() or None,
                    "km": km,
                    "minutes": minutes,
                    "hub": hub,
                    "industry": industry,
                    "ssic": ssic,
                    "public_sector": public,
                }
    return found


def row(rec: dict, mark_new: bool = False) -> str:
    if rec["lo"] is None:
        pay = "not published"
    else:
        pay = f"${rec['lo']:,}" + (f"–${rec['hi']:,}" if rec["hi"] else "+")
    years = "n/s" if rec["years"] is None else str(rec["years"])
    title = f"[{rec['title']}]({rec['url']})" if rec["url"] else rec["title"]
    if mark_new:
        title = "🆕 " + title
    apps = "" if rec["applications"] is None else f" · {rec['applications']} applied"
    return (
        f"| {title} | {rec['company'].title()} | {pay} | {years} | "
        f"{rec['types']} | {rec['posted']}{apps} |"
    )


HEADER = "| Role | Employer | Salary/mo | Yrs | Type | Posted |\n|---|---|---|---|---|---|"


# A logon-triggered run passes --if-stale so it only does work when the day's sweep has not already
# happened. That makes the catch-up guarantee depend on this script rather than on Task Scheduler's
# missed-run semantics, which only fire when the machine was genuinely unavailable.
MIN_AGE_HOURS = 20


def hours_since_last_run() -> float | None:
    if not STATE_FILE.exists():
        return None
    try:
        stamp = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_run")
    except (json.JSONDecodeError, OSError):
        return None
    if not stamp:
        return None
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return None


def main() -> int:
    today = dt.date.today().isoformat()

    if "--if-stale" in sys.argv:
        age = hours_since_last_run()
        if age is not None and age < MIN_AGE_HOURS:
            print(f"last run {age:.1f}h ago, under {MIN_AGE_HOURS}h — skipping")
            return 0
        print(f"last run {'never' if age is None else f'{age:.1f}h ago'} — running")
    print(f"Sweeping MyCareersFuture ({len(SEARCHES)} searches)…")
    found = collect()
    print(f"  {len(found)} MyCareersFuture postings matched the role shape")
    print("Checking company career pages…")
    career = job_sources.fetch_all(TITLE_KEEP)
    career = [r for r in career if not TITLE_DROP.search(r["title"])
              and not COMPANY_DROP.search(r["company"])]
    for rec in career:
        found.setdefault(rec["id"], rec)
    print(f"  {len(career)} from company career pages")

    state = {"seen": {}, "runs": []}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    seen: dict = state.get("seen", {})
    first_run = not seen

    clears, below, unknown, pages = [], [], [], []
    for rec in found.values():
        if rec.get("source") in CAREER_PAGE_SOURCES:
            pages.append(rec)
        elif rec["lo"] is None:
            unknown.append(rec)
        elif rec["lo"] >= SALARY_FLOOR:
            clears.append(rec)
        else:
            below.append(rec)

    pages.sort(key=lambda r: (r["company"], r["title"]))
    for bucket in (clears, below, unknown):
        bucket.sort(key=lambda r: (-(r["lo"] or 0), r["company"]))

    new_ids = {r["uuid"] for r in clears if r["uuid"] not in seen}
    new_ids |= {r["id"] for r in pages if r["id"] not in seen}
    fresh = [r for r in clears if r["uuid"] in new_ids]

    lines = [
        "---",
        "tags: [active]",
        f"updated: {today}",
        "---",
        "",
        f"# Job Leads — {today} (automated sweep)",
        "",
        "Generated by `build/weekly_sweep.py` from the **MyCareersFuture** public jobs API.",
        "LinkedIn and Glassdoor are *not* covered — they need a logged-in browser session, so run",
        "that pass by hand when something here is worth chasing.",
        "",
        f"Filters applied: salary floor **${SALARY_FLOOR:,}/month** (on the *bottom* of the posted band),",
        "DevOps / SRE / Platform / DevSecOps / Cloud-infra titles only, excluding fresh-grad and",
        f"entry bands, software-engineer and architect titles, {profile.EXCLUDED_EMPLOYERS_TEXT}, and government or defence",
        "employers. Full rules in [[Target Criteria]]; shortlist in [[Job Search Tracker]].",
        "",
        f"**{len(clears)} clear the floor** · {len(below)} below it · {len(unknown)} hide the salary "
        f"· {len(pages)} from company career pages.",
        "",
    ]

    if first_run:
        lines += ["_First run — everything below is new; next week only genuine additions are flagged._", ""]
    elif fresh:
        lines += [f"## New since the last run ({len(fresh)})", "", HEADER]
        lines += [row(r, mark_new=True) for r in fresh]
        lines += [""]
    else:
        lines += ["## New since the last run", "", "_Nothing new cleared the floor this week._", ""]

    lines += [f"## Everything clearing ${SALARY_FLOOR:,} ({len(clears)})", "", HEADER]
    lines += [row(r, mark_new=r["uuid"] in new_ids and not first_run) for r in clears]
    lines += [""]

    if unknown:
        lines += [
            f"## Salary not published ({len(unknown)}) — worth a look, band unknown",
            "",
            HEADER,
        ]
        lines += [row(r) for r in unknown]
        lines += [""]

    if pages:
        lines += [
            f"## Company career pages ({len(pages)}) — roles that never reach the job boards",
            "",
            "Greenhouse / Ashby / Lever / amazon.jobs. These rarely publish a salary; they are here",
            "because the posting is otherwise invisible. Apple and LinkedIn need the browser pass.",
            "",
            "| Role | Employer | Source | Posted |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| [{r['title']}]({r['url']}) | {r['company']} | {r['source']} | {r['posted'] or '-'} |"
            for r in pages
        ]
        lines += [""]

    if below:
        lines += [
            f"<details><summary>Below the floor ({len(below)}) — kept for the record</summary>",
            "",
            HEADER,
        ]
        lines += [row(r) for r in below]
        lines += ["", "</details>", ""]

    lines += ["Linked: [[Job Search Tracker]] · [[Target Criteria]] · [[Weekly Sweep]]", ""]

    note = VAULT / f"Job Leads {today} (auto).md"
    note.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {note.relative_to(ROOT)}")

    # Index note, so the dated files stay discoverable without touching Home every week.
    index = VAULT / "Weekly Sweep.md"
    entry = (
        f"- [[Job Leads {today} (auto)]] — {len(clears)} clearing the floor, "
        f"{len(fresh) if not first_run else len(clears)} new"
    )
    if index.exists():
        body = index.read_text(encoding="utf-8")
        body = re.sub(r"^updated: .*$", f"updated: {today}", body, count=1, flags=re.M)
        if entry not in body:
            body = body.rstrip() + "\n" + entry + "\n"
        index.write_text(body, encoding="utf-8")
    else:
        index.write_text(
            "---\ntags: [active]\n"
            f"updated: {today}\n---\n\n# Weekly Sweep\n\n"
            "Automated MyCareersFuture sweeps, newest last. Script: `build/weekly_sweep.py`,\n"
            "run weekly by Windows Task Scheduler. Rules in [[Target Criteria]].\n\n"
            + entry
            + "\n",
            encoding="utf-8",
        )

    for rec in clears + unknown + pages:
        seen[rec.get("uuid") or rec["id"]] = {"t": rec["title"], "c": rec["company"], "first_seen": today}
    state["seen"] = seen
    state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    state["runs"] = (state.get("runs") or [])[-25:] + [
        {"date": today, "matched": len(found), "cleared": len(clears), "new": len(fresh)}
    ]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"  {len(fresh)} new · state at {STATE_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
