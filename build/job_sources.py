"""Job sources that can be read without a browser or a login.

Each source returns a list of normalised dicts:
    {id, title, company, years, lo, hi, types, posted, url, applications, source}

`lo`/`hi` are SGD per month where the source publishes pay; None where it does not.
Most company ATS boards do not publish salary, which is fine — they are here because
the roles never reach LinkedIn or MyCareersFuture in the first place.

Deliberately NOT here:
  * LinkedIn      — results need an authenticated session; runs in the browser pass instead
  * Glassdoor     — interview/salary research, login-walled
  * NodeFlair, JobStreet — both sit behind **Cloudflare bot verification**. Checked 2026-09-11: a
    headless Playwright load returns "Performing security verification" rather than the page. Getting
    past that means defeating a bot check, which this project does not do. Browse them yourself, or
    add them to the interactive browser pass where a real session loads them normally.
  * Workday: Micron and Citi are wired up (see WORKDAY). Grab and DBS 404 on every host/site
    combination tried; GIC, Standard Chartered, Visa and Sea return 422 — the tenant resolves but the
    site ID is wrong and is not guessable. Those need the ID read off their careers page.
  * Glints, SmartRecruiters, Workable — probed 2026-09-11 and returned nothing usable for Singapore
    infrastructure roles (wrong/absent account slugs, or 400/401).

Apple IS here, via Playwright — its own API rejects unauthenticated calls, but the public search page
loads normally with no bot check to defeat.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

# Companies worth watching that post infra roles on their own ATS rather than the job boards.
# A 404 means the board slug moved or the company left the platform — the sweep skips it and
# says so, it is not an error worth stopping for.
GREENHOUSE = [
    "stripe", "datadog", "gitlab", "databricks", "cloudflare", "anthropic", "figma",
    "mongodb", "elastic", "coinbase", "airbnb", "twilio", "robinhood",
    "samsara", "discord", "asana",
    # verified live 2026-09-11; cockroachlabs and ripple had Singapore openings that day
    "cockroachlabs", "ripple", "circleci", "vercel", "starburst", "gocardless", "affirm", "wise",
    # Firmus / Sustainable Metal Cloud — Singapore AI-infrastructure company, $3.05B raised, Nvidia
    # DGX Cloud partner. Found 2026-09-11 while researching its MyCareersFuture posting; it runs two
    # Greenhouse boards and the roles do not all reach the job boards.
    "firmus", "smc",
]
ASHBY = ["openai", "cohere", "linear", "ramp", "notion", "anysphere"]
LEVER = ["ninjavan", "shopback", "thunes", "nium", "xendit", "aspire", "coda-payments"]

# The ATS boards carry no industry field, but the employer is known, so the industry is too.
INDUSTRY = {
    "firmus": "AI Infrastructure / GPU Cloud", "smc": "AI Infrastructure / GPU Cloud",
    "stripe": "Fintech / Payments", "datadog": "Observability Software",
    "gitlab": "Developer Tools", "databricks": "Data & AI Platform",
    "cloudflare": "Internet Infrastructure", "anthropic": "AI Research",
    "figma": "Design Software", "mongodb": "Database Software",
    "elastic": "Search & Observability", "coinbase": "Crypto Exchange",
    "airbnb": "Travel Marketplace", "twilio": "Communications API",
    "robinhood": "Fintech / Brokerage", "samsara": "IoT / Fleet Software",
    "discord": "Consumer Social", "asana": "Productivity Software",
    "cockroachlabs": "Database Software", "ripple": "Crypto / Payments",
    "circleci": "Developer Tools / CI", "vercel": "Developer Platform",
    "starburst": "Data Analytics", "gocardless": "Fintech / Payments",
    "affirm": "Fintech / Lending", "wise": "Fintech / Remittance",
    "openai": "AI Research", "cohere": "AI Research", "linear": "Developer Tools",
    "ramp": "Fintech / Spend", "notion": "Productivity Software", "anysphere": "AI Developer Tools",
    "ninjavan": "Logistics", "shopback": "E-commerce / Rewards", "thunes": "Cross-border Payments",
    "nium": "Cross-border Payments", "xendit": "Payments", "aspire": "Business Banking",
    "coda-payments": "Digital Payments",
    "micron": "Semiconductors", "citi": "Banking and Finance",
    "amazon / aws": "Cloud / E-commerce", "apple": "Consumer Technology",
    "nvidia": "AI Compute / Semiconductors",
}

TIMEOUT = 25


def _get(url: str, data: dict | None = None) -> dict | list:
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, headers=headers, data=json.dumps(data).encode() if data else None
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def _sg(text: str | None) -> bool:
    return "singapore" in (text or "").lower()


# Roles James could take without moving. The overseas companies on these boards price AI-platform
# work far better than the Singapore market does, and they were being discarded because the location
# said "APAC (Remote)" rather than "Singapore". Americas/EMEA-only remote is excluded — the time zone
# makes it a different job.
REMOTE_OK = re.compile(
    r"remote", re.I
)
REMOTE_REGION = re.compile(
    r"apac|asia|singapore|sea\b|global|anywhere|worldwide|emea/apac", re.I
)
REMOTE_NO = re.compile(
    r"americas|united states|\bus\b|usa|canada|latam|emea only|europe only|\buk\b", re.I
)


def _reachable(text: str | None) -> tuple[bool, bool]:
    """(worth including, is it remote) for a location string."""
    loc = text or ""
    if _sg(loc):
        return True, "remote" in loc.lower()
    if REMOTE_OK.search(loc) and REMOTE_REGION.search(loc) and not REMOTE_NO.search(loc):
        return True, True
    return False, False


def _plain(markup: str | None, limit: int = 6000) -> str:
    """Job-description HTML (Greenhouse double-escapes it) as plain text for the judge prompt."""
    text = html.unescape(html.unescape(markup or ""))
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()[:limit]


def _rec(title, company, url, posted="", source="", years=None) -> dict:
    return {
        "id": url or f"{company}:{title}",
        "title": title.strip(),
        "company": company,
        "years": years,
        "lo": None,
        "hi": None,
        "types": "-",
        "levels": "-",
        "posted": (posted or "")[:10],
        "url": url,
        "applications": None,
        "source": source,
        "remote": False,
        "where": None,
        "industry": INDUSTRY.get(company.lower().strip(), ""),
        "ssic": None,
        "public_sector": False,
    }


def greenhouse(keep: re.Pattern, log=print) -> list[dict]:
    out, missing = [], []
    for slug in GREENHOUSE:
        try:
            data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(slug)
            continue
        except (OSError, ValueError):  # network, dropped connection or a non-JSON reply: skip this board only
            continue
        for job in data.get("jobs", []):
            loc = (job.get("location") or {}).get("name", "")
            reachable, remote = _reachable(loc)
            if not reachable or not keep.search(job.get("title", "")):
                continue
            rec = _rec(job["title"], slug.title(), job.get("absolute_url", ""),
                       job.get("updated_at", ""), "Greenhouse")
            # The list endpoint has no description; fetched per job only when it's about to be judged.
            rec.update(remote=remote, where=loc,
                       jd_url=f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job.get('id')}")
            out.append(rec)
    if missing:
        log(f"  (greenhouse slug not found, skipped: {', '.join(missing)})")
    return out


def ashby(keep: re.Pattern, log=print) -> list[dict]:
    out = []
    for slug in ASHBY:
        try:
            data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        except Exception:
            continue
        for job in data.get("jobs", []):
            reachable, remote = _reachable(job.get("location"))
            if not reachable or not keep.search(job.get("title", "")):
                continue
            rec = _rec(job["title"], slug.title(), job.get("jobUrl", ""),
                       job.get("publishedAt", ""), "Ashby")
            rec.update(remote=remote, where=job.get("location"),
                       desc=_plain(job.get("descriptionPlain") or job.get("descriptionHtml")))
            out.append(rec)
    return out


def lever(keep: re.Pattern, log=print) -> list[dict]:
    out = []
    for slug in LEVER:
        try:
            data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        except Exception:
            continue
        for job in data:
            loc = (job.get("categories") or {}).get("location", "")
            reachable, remote = _reachable(loc)
            if not reachable or not keep.search(job.get("text", "")):
                continue
            rec = _rec(job["text"], slug.replace("-", " ").title(), job.get("hostedUrl", ""),
                       "", "Lever")
            lists = "\n".join(f"{x.get('text', '')}:\n{_plain(x.get('content'))}" for x in job.get("lists") or [])
            rec.update(remote=remote, where=loc,
                       desc=_plain(f"{job.get('descriptionPlain', '')}\n{lists}\n{job.get('additionalPlain', '')}"))
            out.append(rec)
    return out


def amazon(keep: re.Pattern, log=print) -> list[dict]:
    """AWS and Amazon Singapore. Note: Amazon runs coding interviews for SDE titles;
    infrastructure/support titles are usually scenario-based. Flagged, not filtered."""
    out = []
    for query in ("engineer", "devops", "systems"):
        qs = urllib.parse.urlencode(
            {
                "normalized_country_code[]": "SGP",
                "radius": "24km",
                "offset": 0,
                "result_limit": 100,
                "sort": "recent",
                "base_query": query,
                "country": "SGP",
            }
        )
        try:
            data = _get(f"https://www.amazon.jobs/en/search.json?{qs}")
        except Exception:
            continue
        for job in data.get("jobs", []):
            if not keep.search(job.get("title", "")):
                continue
            url = "https://www.amazon.jobs" + (job.get("job_path") or "")
            rec = _rec(job["title"], "Amazon / AWS", url, job.get("posted_date", ""), "amazon.jobs")
            rec["desc"] = _plain("\n".join(job.get(k) or "" for k in
                                             ("description", "basic_qualifications", "preferred_qualifications")))
            if rec["id"] not in {o["id"] for o in out}:
                out.append(rec)
    return out


# Workday tenants, discovered by probing on 2026-09-11. Each needs host/tenant/site/wd-instance and
# they are not guessable — Grab and DBS returned 404 on every combination tried, GIC, Standard
# Chartered, Visa and Sea returned 422 (tenant resolves, site ID wrong). These two answer.
WORKDAY = [
    ("micron", "micron", "External", "wd1"),
    ("citi", "citi", "2", "wd5"),
]
WORKDAY_TERMS = ("devops", "site reliability", "infrastructure engineer", "platform engineer")


def workday(keep: re.Pattern, log=print) -> list[dict]:
    out: list[dict] = []
    for host, tenant, site, wd in WORKDAY:
        url = f"https://{host}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        for term in WORKDAY_TERMS:
            for offset in (0, 20, 40, 60, 80):
                try:
                    data = _get(url, {"appliedFacets": {}, "limit": 20,
                                      "offset": offset, "searchText": term})
                except Exception:
                    break
                postings = data.get("jobPostings", [])
                if not postings:
                    break
                for job in postings:
                    if not _sg(job.get("locationsText")) or not keep.search(job.get("title", "")):
                        continue
                    path = job.get("externalPath", "")
                    link = f"https://{host}.{wd}.myworkdayjobs.com/en-US/{site}{path}"
                    rec = _rec(job["title"], host.title(), link,
                               job.get("postedOn", ""), "Workday")
                    # Workday's list call has no description; nas_agent.description() fetches this.
                    rec["jd_workday"] = f"https://{host}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
                    if rec["id"] not in {o["id"] for o in out}:
                        out.append(rec)
    return out


# NVIDIA runs Workday too, but it cannot go in WORKDAY above: that path filters on
# `locationsText`, and NVIDIA collapses multi-site postings to "2 Locations" / "3 Locations", so a
# Singapore role hides behind a string with no country in it. The location *facet* is exact, so
# this asks for the Singapore country node and takes everything under it — 22 roles on 2026-09-13,
# no search terms needed. Facet ids are stable tenant-side; if this ever returns 0, re-read the
# `locationHierarchy1` facet from an unfaceted call and update the id.
NVIDIA_HOST = "https://nvidia.wd5.myworkdayjobs.com"
NVIDIA_SITE = "NVIDIAExternalCareerSite"
NVIDIA_SG_FACET = "2fcb99c455831013ea52df1adb7432a8"  # locationHierarchy1 → Singapore


def nvidia(keep: re.Pattern, log=print) -> list[dict]:
    out: dict[str, dict] = {}
    url = f"{NVIDIA_HOST}/wday/cxs/nvidia/{NVIDIA_SITE}/jobs"
    # limit is capped at 20 server-side and offset is ignored once the facet is applied, so this
    # walks the pages defensively and leans on the dict to dedupe rather than trusting either.
    for offset in (0, 20, 40):
        try:
            data = _get(url, {"appliedFacets": {"locationHierarchy1": [NVIDIA_SG_FACET]},
                              "limit": 20, "offset": offset, "searchText": ""})
        except Exception:
            break
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for job in postings:
            title = job.get("title", "")
            if not keep.search(title):
                continue
            path = job.get("externalPath", "")
            link = f"{NVIDIA_HOST}/en-US/{NVIDIA_SITE}{path}"
            rec = _rec(title, "NVIDIA", link, "", "NVIDIA (Workday)")
            # postedOn is relative text ("Posted 23 Days Ago"), not a date — keep it as the note
            # rather than pretending it parses.
            rec.update(where=job.get("locationsText"), posted=str(job.get("postedOn") or "")[:24],
                       jd_workday=f"{NVIDIA_HOST}/wday/cxs/nvidia/{NVIDIA_SITE}{path}")
            out.setdefault(rec["id"], rec)
    return list(out.values())


def apple(keep: re.Pattern, log=print) -> list[dict]:
    """Apple Singapore. No usable API — jobs.apple.com/api/v1/search rejects unauthenticated
    POSTs (HTTP 436) and the GET variant returns 401 — so this drives Playwright against the
    public search page instead. Uses the installed Chrome; Playwright cannot download its own
    browser on this machine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("  apple: playwright not installed, skipped")
        return []

    out: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            page = browser.new_page()
            for n in range(1, 7):  # ~110 SG roles, 20 per page
                url = f"https://jobs.apple.com/en-sg/search?location=singapore-SGP&sort=newest&page={n}"
                page.goto(url, timeout=60000)
                page.wait_for_timeout(5000)
                links = page.locator('a[href*="/en-sg/details/"]')
                count = links.count()
                if not count:
                    break
                for idx in range(count):
                    link = links.nth(idx)
                    title = (link.inner_text() or "").strip()
                    if not title or not keep.search(title):
                        continue
                    href = link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = "https://jobs.apple.com" + href
                    out.append(_rec(title, "Apple", href, "", "Apple (Playwright)"))
            browser.close()
    except Exception as exc:
        log(f"  apple: {type(exc).__name__} — {str(exc)[:80]}")
    return out


ALL = [greenhouse, ashby, lever, amazon, workday, nvidia, apple]


def fetch_all(keep: re.Pattern, log=print) -> list[dict]:
    found: dict[str, dict] = {}
    for fn in ALL:
        try:
            got = fn(keep, log)
        except Exception as exc:  # one broken board must not kill the sweep
            log(f"  ! {fn.__name__}: {type(exc).__name__}")
            continue
        log(f"  {fn.__name__}: {len(got)} Singapore infra roles")
        for rec in got:
            found.setdefault(rec["id"], rec)
    return list(found.values())
