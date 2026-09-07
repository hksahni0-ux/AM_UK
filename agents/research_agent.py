"""
ResearchAgent: visits a company website, extracts mission/vision/focus,
finds the careers page, and identifies the best matching role for Prateek's CV
in the target country (UK or Ireland, driven by each row's Country column).

Role priority:
  1. Additive manufacturing roles
  2. Process engineering / process development
  3. Other relevant manufacturing/engineering roles
  Filters: target-country-based only (or remote/hybrid open to that country's applicants)
"""

import logging
import re
import threading
import time
from typing import Optional, Tuple, List
from urllib.parse import urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup
import trafilatura
from playwright.sync_api import sync_playwright

from agents import llm_client
from config.settings import get_cv_context, get_country_config, DEFAULT_COUNTRY

log = logging.getLogger(__name__)

_local = threading.local()  # holds playwright_page per thread

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 15

_JD_SECTION_MARKERS = (
    "responsibilities", "requirements", "about you", "what you'll do", "what you will do",
    "you will", "key skills", "essential criteria", "desirable", "qualifications",
    "person specification", "the role", "your role", "job summary", "duties", "skills and experience",
)


def _trim_jd(text: str, max_chars: int) -> str:
    """
    Truncating a JD to N chars from the start keeps whatever boilerplate (company
    blurb, office perks) happens to be first and can cut off before the actual
    requirements ever appear. Find the earliest requirements-like section marker
    and truncate from there instead, when it's worth skipping past.
    """
    if not text:
        return ""
    lower = text.lower()
    earliest = None
    for marker in _JD_SECTION_MARKERS:
        idx = lower.find(marker)
        if idx != -1 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is not None and earliest > 200:
        start = max(0, earliest - 80)
        return text[start:start + max_chars]
    return text[:max_chars]
MAX_CONTENT_CHARS = 6000

CAREERS_PATTERNS = [
    r"career", r"job", r"vacanc", r"work.with.us",
    r"join.us", r"hiring", r"opportunit", r"recruit",
    r"position", r"opening", r"current.role",
]

_BASE_CAREERS_PATHS = [
    "/careers", "/jobs", "/vacancies", "/work-with-us", "/join-us",
    "/join", "/hiring", "/recruitment", "/about/careers", "/about/jobs",
    "/company/careers", "/company/jobs", "/pages/careers",
    "/en/careers", "/en/jobs", "/us/careers", "/uk/careers",
    "/about-us/careers", "/about-us/jobs", "/opportunities",
    "/current-opportunities", "/job-opportunities", "/open-positions",
    "/working-here", "/work-here", "/people/careers",
]
# Also try .html variants for static sites
COMMON_CAREERS_PATHS = _BASE_CAREERS_PATHS + [p + ".html" for p in _BASE_CAREERS_PATHS]

# Per-ATS slug extraction — each captures the company identifier from raw HTML
_ATS_SLUGS = {
    "greenhouse": re.compile(
        r'boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([^/"\s<>?#]+)', re.I),
    "lever":      re.compile(r'jobs\.lever\.co/([^/"\s<>?#]+)', re.I),
    "workable":   re.compile(r'apply\.workable\.com/([^/"\s<>?#]+)', re.I),
    "smartrec":   re.compile(
        r'careers\.smartrecruiters\.com/([^/"\s<>?#]+)', re.I),
    "recruitee":  re.compile(
        r'https?://([^/"\s<>?#]+)\.recruitee\.com', re.I),
    "ashby":      re.compile(r'jobs\.ashbyhq\.com/([^/"\s<>?#]+)', re.I),
    "bamboohr":   re.compile(
        r'https?://([^/"\s<>?#]+)\.bamboohr\.com', re.I),
    "pinpoint":   re.compile(
        r'https?://([^/"\s<>?#]+)\.pinpointhq\.com', re.I),
}

# ConnectID ATS detection (e.g. careers.rolls-royce.com) — URL only appears in XHR, not HTML
_CONNECTID_RE = re.compile(r'https?://([a-z0-9\-]+\.connectid\.cloud)', re.I)

# Per-ATS slug extraction for Workday (slug + wd-number + site extracted together)
_WORKDAY_RE = re.compile(
    r'https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com'
    r'(?:/(?:en-[A-Z]{2}/|[a-z]{2}-[a-z]{2}/)?([a-zA-Z0-9_\-]+))?',
    re.IGNORECASE,
)

# Fallback pattern — catches any ATS URL in raw HTML for careers_url field
_ATS_URL_PATTERN = re.compile(
    r'https?://[^\s"\'<>]*(greenhouse\.io|lever\.co|workable\.com|'
    r'bamboohr\.com|smartrecruiters\.com|jobvite\.com|taleo\.net|'
    r'icims\.com|recruitee\.com|teamtailor\.com|ashby\.com|'
    r'myworkdayjobs\.com|successfactors\.com|pinpointhq\.com)[^\s"\'<>]*',
    re.IGNORECASE,
)

ABOUT_PATTERNS = [
    r"about", r"mission", r"vision", r"who.we.are",
    r"our.story", r"company", r"what.we.do",
]


def _parse_content(raw: str) -> str:
    content = trafilatura.extract(raw)
    if not content or len(content) < 100:
        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        content = soup.get_text(separator="\n", strip=True)
    return content or ""


def _fetch_raw_html(url: str) -> Tuple[Optional[str], Optional[str]]:
    # Try playwright first (handles JS-rendered pages)
    page = getattr(_local, "playwright_page", None)
    if page is not None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            page.wait_for_timeout(1000)
            raw = page.content()
            content = _parse_content(raw)
            if raw:
                return raw, content
        except Exception as exc:
            log.debug("Playwright fetch failed for %s: %s", url, exc)
    # Fallback: plain requests
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        raw = resp.text
        return raw, _parse_content(raw)
    except Exception as exc:
        log.debug("Requests fetch failed for %s: %s", url, exc)
        return None, None


def _extract_job_listings(raw_html: str, base_url: str = "", country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict]:
    """
    Parse job listings from careers page HTML using <a> tags.
    Returns (formatted_text, {title: url}) — text is None if no listings found.
    """
    cfg = get_country_config(country)
    soup = BeautifulSoup(raw_html, "lxml")
    lines = []
    url_map = {}  # title → absolute URL
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if "/jobs/" not in href and "/job/" not in href and "/vacancy/" not in href:
            continue
        title = a.get_text(strip=True)
        if not title or title in seen:
            continue
        seen.add(title)
        full_href = urljoin(base_url, href) if base_url else href
        url_map[title] = full_href
        # Try to find location in nearby elements (parent div, siblings)
        location = ""
        parent = a.find_parent(["li", "div", "article", "tr"])
        if parent:
            for tag in parent.find_all(True):
                t = tag.get_text(strip=True)
                if t and t != title and len(t) < 60 and t not in title:
                    if any(kw in t.lower() for kw in cfg["location_keywords"]):
                        location = t
                        break
        if location:
            lines.append(f"{title} ({location}, {cfg['location_suffix']})")
        else:
            lines.append(title)
    if not lines:
        return None, {}
    return "\n".join(lines), url_map


def _fetch_jd_text(role_url: str) -> str:
    """Fetch a job description page and return its text content (up to 2000 chars)."""
    raw, _ = _fetch_raw_html(role_url)
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    # Prefer <main> or <article> which contains the actual JD, not cookie banners
    for selector in ["main", "article", "[class*='description']", "[class*='content']"]:
        els = soup.select(selector)
        for el in els:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 300 and "cookie" not in text[:200].lower():
                return _trim_jd(text, 2000)
    return _trim_jd(soup.get_text(separator="\n", strip=True), 2000)


# ── ATS API handlers ─────────────────────────────────────────────────────────
# Each returns (listings_text | None, url_map, jd_map)

_BAD_SLUGS = {"jobs", "careers", "api", "www", "embed", "apply", "hiring"}


def _jd_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return _trim_jd(soup.get_text(separator="\n", strip=True), 2000)


def _fmt_listing(title: str, loc: str, country: str = DEFAULT_COUNTRY) -> str:
    """
    Append the target country's label to a location string ONLY when that
    location plausibly IS the target country (matches its known keywords) —
    never force-label a location that already names somewhere else. ATS
    platforms without their own country-level API filtering (Greenhouse, Lever,
    Workable, SmartRecruiters, Recruitee, Ashby, BambooHR, Pinpoint) return jobs
    from every country the company hires in, so blindly appending the suffix
    whenever the target country wasn't already mentioned would mislabel a
    genuinely foreign role (e.g. "Berlin, Germany") as domestic. Leaving an
    unmatched location exactly as reported lets the role-matching prompt's own
    "must be based in {country}" rule judge it correctly instead.
    """
    cfg = get_country_config(country)
    if loc and cfg["location_suffix"].lower() not in loc.lower():
        if any(kw in loc.lower() for kw in cfg["location_keywords"]):
            loc = loc + f", {cfg['location_suffix']}"
    return f"{title} ({loc})" if loc else title


def _api_greenhouse(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    lines, url_map, jd_map = [], {}, {}
    for job in jobs:
        title = (job.get("title") or "").strip()
        loc   = (job.get("location") or {}).get("name", "")
        href  = job.get("absolute_url", "")
        jd_html = job.get("content", "")
        if not title:
            continue
        lines.append(_fmt_listing(title, loc, country))
        url_map[title] = href
        if jd_html:
            jd_map[title] = _jd_from_html(jd_html)
    return ("\n".join(lines) or None), url_map, jd_map


def _api_lever(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json()
    if not isinstance(jobs, list):
        return None, {}, {}
    lines, url_map, jd_map = [], {}, {}
    for job in jobs:
        title = (job.get("text") or "").strip()
        loc   = (job.get("categories") or {}).get("location", "")
        href  = job.get("hostedUrl", "")
        jd    = (job.get("descriptionPlain") or "").strip()
        if not title:
            continue
        lines.append(_fmt_listing(title, loc, country))
        url_map[title] = href
        if jd:
            jd_map[title] = _trim_jd(jd, 2000)
    return ("\n".join(lines) or None), url_map, jd_map


def _api_workable(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    url = f"https://apply.workable.com/api/v1/companies/{slug}/jobs"
    resp = requests.get(
        url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("results", [])
    lines, url_map = [], {}
    for job in jobs:
        title = (job.get("title") or "").strip()
        city  = job.get("city") or job.get("country_code", "")
        code  = job.get("shortcode", "")
        href  = f"https://apply.workable.com/{slug}/j/{code}" if code else ""
        if not title:
            continue
        lines.append(_fmt_listing(title, city, country))
        url_map[title] = href
    return ("\n".join(lines) or None), url_map, {}


def _api_smartrecruiters(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("content", [])
    lines, url_map = [], {}
    for job in jobs:
        title = (job.get("name") or "").strip()
        city  = (job.get("location") or {}).get("city", "")
        jid   = job.get("id", "")
        href  = f"https://careers.smartrecruiters.com/{slug}/{jid}" if jid else ""
        if not title:
            continue
        lines.append(_fmt_listing(title, city, country))
        url_map[title] = href
    return ("\n".join(lines) or None), url_map, {}


def _api_recruitee(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    url = f"https://careers.recruitee.com/api/o/{slug}/offers/"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("offers", [])
    lines, url_map, jd_map = [], {}, {}
    for job in jobs:
        title    = (job.get("title") or "").strip()
        loc      = job.get("location", "")
        jslug    = job.get("slug", "")
        href     = f"https://careers.recruitee.com/o/{slug}/{jslug}" if jslug else ""
        jd_html  = job.get("description", "") or ""
        if not title:
            continue
        lines.append(_fmt_listing(title, loc, country))
        url_map[title] = href
        if jd_html:
            jd_map[title] = _jd_from_html(jd_html)
    return ("\n".join(lines) or None), url_map, jd_map


def _api_ashby(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    # Ashby has a public posting API — company slug is used directly
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        if resp.ok:
            jobs = resp.json().get("jobPostings", [])
            lines, url_map, jd_map = [], {}, {}
            for job in jobs:
                title = (job.get("title") or "").strip()
                loc   = (job.get("location") or {}).get("locationStr", "")
                href  = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{job.get('id','')}"
                jd    = (job.get("descriptionPlain") or "").strip()
                if not title:
                    continue
                lines.append(_fmt_listing(title, loc, country))
                url_map[title] = href
                if jd:
                    jd_map[title] = _trim_jd(jd, 2000)
            if lines:
                return "\n".join(lines), url_map, jd_map
    except Exception:
        pass
    # Fallback: scrape the Ashby hosted jobs page
    raw, _ = _fetch_raw_html(f"https://jobs.ashbyhq.com/{slug}")
    if not raw:
        return None, {}, {}
    listings, url_map = _extract_job_listings(raw, f"https://jobs.ashbyhq.com/{slug}", country)
    return listings, url_map, {}


def _api_bamboohr(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    # BambooHR public API — returns JSON list
    url = f"https://{slug}.bamboohr.com/jobs/embed2.php"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.ok and resp.text.strip().startswith("{"):
            jobs = resp.json().get("result", [])
            lines, url_map = [], {}
            for job in jobs:
                title = (job.get("title", {}).get("label") or "").strip()
                loc   = (job.get("location", {}).get("label") or "").strip()
                href  = job.get("url", "")
                if not title:
                    continue
                lines.append(_fmt_listing(title, loc, country))
                url_map[title] = href
            if lines:
                return "\n".join(lines), url_map, {}
    except Exception:
        pass
    # Fallback: scrape BambooHR careers page
    raw, _ = _fetch_raw_html(f"https://{slug}.bamboohr.com/careers")
    if not raw:
        return None, {}, {}
    listings, url_map = _extract_job_listings(raw, f"https://{slug}.bamboohr.com/careers", country)
    return listings, url_map, {}


def _api_workday(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    """
    Workday: CXS JSON API (target-country-filtered) → RSS feed fallback.
    slug format: "{company_slug}|{wd_number}|{site}" (joined by |)
    """
    parts = slug.split("|")
    if len(parts) < 3:
        return None, {}, {}
    company, wdn, site = parts[0], parts[1], parts[2]
    base = f"https://{company}.{wdn}.myworkdayjobs.com"
    cxs_url = f"{base}/wday/cxs/{company}/{site}/jobs"
    cxs_headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    cfg = get_country_config(country)
    codes_alt = "|".join(re.escape(c) for c in cfg["workday_codes"])

    # Primary: CXS JSON API via fresh Playwright context (clean session, no moog.com cookies)
    pw_instance = getattr(_local, "playwright_instance", None)
    if pw_instance is not None:
        try:
            wd_home = f"{base}/en-US/{site}/"
            fresh_ctx = pw_instance.chromium.launch(headless=True).new_context(
                user_agent=HEADERS["User-Agent"]
            )
            wd_page = fresh_ctx.new_page()
            wd_page.goto(wd_home, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            wd_page.wait_for_timeout(500)
            result = wd_page.evaluate("""async ({cxsUrl, codesAlt}) => {
                const post = (body) => fetch(cxsUrl, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const r1 = await post({appliedFacets: {}, limit: 100, offset: 0, searchText: ''});
                if (!r1.ok) return null;
                const d1 = await r1.json();
                const total = d1.total || 0;
                let jobs = [...(d1.jobPostings || [])];
                for (let offset = 100; offset < Math.min(total, 500); offset += 100) {
                    const rn = await post({appliedFacets: {}, limit: 100, offset, searchText: ''});
                    if (!rn.ok) break;
                    jobs = jobs.concat((await rn.json()).jobPostings || []);
                }
                const filterRe = new RegExp(',\\\\s*(' + codesAlt + ')$');
                const countryJobs = jobs.filter(j => filterRe.test(j.locationsText || ''));
                return {total, fetched: jobs.length, jobs: countryJobs};
            }""", {"cxsUrl": cxs_url, "codesAlt": codes_alt})
            fresh_ctx.browser.close()
            if result and result.get("jobs"):
                lines, url_map = [], {}
                for j in result["jobs"]:
                    title = (j.get("title") or "").strip()
                    loc   = (j.get("locationsText") or "").strip()
                    path  = j.get("externalPath") or ""
                    href  = f"{base}{path}" if path.startswith("/") else path
                    if title:
                        lines.append(_fmt_listing(title, loc, country))
                        url_map[title] = href
                if lines:
                    log.info("Workday CXS API returned %d %s roles", len(lines), cfg["label"])
                    return "\n".join(lines), url_map, {}
        except Exception as exc:
            log.debug("Workday CXS via Playwright failed: %s", exc)

    # Fallback: CXS API via requests (no Playwright — may be blocked by WAF if browser is concurrent)
    try:
        sess = requests.Session()
        sess.headers.update(cxs_headers)
        r1 = sess.post(cxs_url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, timeout=TIMEOUT)
        if r1.ok:
            desc_re = re.compile(rf", ({codes_alt})$|{re.escape(cfg['country_full_name'])}")
            country_ids = []
            for facet in r1.json().get("facets", []):
                for group in facet.get("values", []):
                    candidates = group.get("values", [group])
                    for v in candidates:
                        desc = v.get("descriptor", "")
                        if desc_re.search(desc):
                            country_ids.append(v["id"])
            if country_ids:
                r2 = sess.post(cxs_url,
                               json={"appliedFacets": {"locations": country_ids}, "limit": 100, "offset": 0, "searchText": ""},
                               timeout=TIMEOUT)
                if r2.ok:
                    lines, url_map = [], {}
                    for j in r2.json().get("jobPostings", []):
                        title = j.get("title", "").strip()
                        loc   = j.get("locationsText", "").strip()
                        path  = j.get("externalPath", "")
                        href  = f"{base}{path}" if path.startswith("/") else path
                        if title:
                            lines.append(_fmt_listing(title, loc, country))
                            url_map[title] = href
                    if lines:
                        log.info("Workday CXS API returned %d %s roles", len(lines), cfg["label"])
                        return "\n".join(lines), url_map, {}
    except Exception as exc:
        log.debug("Workday CXS API failed: %s", exc)

    # Fallback: RSS feed (no country filter, but works for some tenants)
    rss_url = f"{base}/en-US/{site}/jobs.rss"
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.ok and "<item>" in resp.text:
            titles = re.findall(r'<title><!\[CDATA\[(.+?)\]\]></title>', resp.text)
            locs   = re.findall(r'<g2:location>(.+?)</g2:location>', resp.text)
            links  = re.findall(r'<link>(.+?)</link>', resp.text)
            lines, url_map = [], {}
            for i, title in enumerate(titles[1:], 0):  # skip feed title
                title = title.strip()
                loc   = locs[i].strip() if i < len(locs) else ""
                href  = links[i + 1].strip() if i + 1 < len(links) else ""
                if not title:
                    continue
                lines.append(_fmt_listing(title, loc, country))
                url_map[title] = href
            if lines:
                return "\n".join(lines), url_map, {}
    except Exception:
        pass

    return None, {}, {}


def _api_pinpoint(slug: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict]:
    """Pinpoint HQ — UK-based ATS popular with SMEs."""
    url = f"https://{slug}.pinpointhq.com/api/v1/jobs?per_page=100"
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        if resp.ok:
            jobs = resp.json().get("data", [])
            lines, url_map, jd_map = [], {}, {}
            for job in jobs:
                attrs = job.get("attributes", {})
                title = (attrs.get("title") or "").strip()
                loc   = (attrs.get("location") or "").strip()
                href  = attrs.get("apply_url", "") or f"https://{slug}.pinpointhq.com/jobs/{job.get('id','')}"
                jd    = (attrs.get("description") or "").strip()
                if not title:
                    continue
                lines.append(_fmt_listing(title, loc, country))
                url_map[title] = href
                if jd:
                    jd_soup = BeautifulSoup(jd, "lxml")
                    jd_map[title] = _trim_jd(jd_soup.get_text(separator="\n", strip=True), 2000)
            if lines:
                return "\n".join(lines), url_map, jd_map
    except Exception:
        pass
    return None, {}, {}


_ATS_HANDLERS = [
    ("greenhouse", _api_greenhouse),
    ("lever",      _api_lever),
    ("workable",   _api_workable),
    ("smartrec",   _api_smartrecruiters),
    ("recruitee",  _api_recruitee),
    ("ashby",      _api_ashby),
    ("bamboohr",   _api_bamboohr),
    ("pinpoint",   _api_pinpoint),
    # Workday handled separately via _try_ats_api due to multi-part slug
]


def _try_connectid_api(country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict, str]:
    """
    ConnectID ATS (e.g. careers.rolls-royce.com) — detected via Playwright network interception.
    XHR to *.connectid.cloud is captured during page load; stored in _local.connectid_api_base.
    """
    api_base = getattr(_local, "connectid_api_base", None)
    if not api_base:
        return None, {}, {}, ""
    cfg = get_country_config(country)
    headers = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}
    try:
        r = requests.get(f"https://{api_base}/auth/gettoken", headers=HEADERS, timeout=TIMEOUT)
        if not r.ok:
            return None, {}, {}, ""
        token = r.json().get("token")
        if not token:
            return None, {}, {}, ""
        headers["Authorization"] = f"Bearer {token}"
        lines, url_map = [], {}
        page_num = 1
        primary_country = quote_plus(cfg["connectid_country"])
        while True:
            r = requests.post(
                f"https://{api_base}/api/jobs?page={page_num}&perPage=50&primaryCountry={primary_country}",
                headers=headers, json={}, timeout=TIMEOUT,
            )
            if not r.ok:
                break
            jobs = r.json().get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                title = (j.get("jobTitle") or "").strip()
                city  = (j.get("primaryCity") or j.get("primaryLocation") or "").strip()
                href  = j.get("applyUrl", "")
                if not title:
                    continue
                lines.append(_fmt_listing(title, city, country))
                url_map[title] = href
            if len(jobs) < 50:
                break
            page_num += 1
        if lines:
            log.info("ConnectID API: %d %s roles", len(lines), cfg["label"])
            careers_url = getattr(_local, "connectid_careers_url", "") or f"https://{api_base}"
            return "\n".join(lines), url_map, {}, careers_url
    except Exception as exc:
        log.debug("ConnectID API failed: %s", exc)
    return None, {}, {}, ""


def _try_gaia_api(raw: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict, str]:
    """Gaia/Socially SPA platform (e.g. joinus.gknaerospace.com). Detects iamgaia.com script."""
    if "iamgaia.com" not in raw and "stanltcs" not in raw:
        return None, {}, {}, ""
    page = getattr(_local, "playwright_page", None)
    if page is None:
        return None, {}, {}, ""
    cfg = get_country_config(country)
    try:
        matched_jobs = []
        for team in ["Engineering", "Manufacturing Engineer"]:
            body = {"searchQuery": "", "location": "", "team": team,
                    "startDate": None, "endDate": None, "pageNumber": 1, "pageSize": 100}
            result = page.evaluate("""async (body) => {
                const r = await fetch('/api/jobs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!r.ok) return null;
                return await r.json();
            }""", body)
            if not result:
                continue
            for job in result.get("items", []):
                if job.get("countryCode") == cfg["gaia_country_code"]:
                    matched_jobs.append(job)
        if not matched_jobs:
            return None, {}, {}, ""
        lines = [f"{j['title']} — {j.get('location', '')}" for j in matched_jobs]
        url_map = {j["title"]: j.get("url", "") for j in matched_jobs}
        careers_url = page.url
        log.info("Gaia API: %d %s engineering roles", len(matched_jobs), cfg["label"])
        return "\n".join(lines), url_map, {}, careers_url
    except Exception as exc:
        log.debug("Gaia API error: %s", exc)
        return None, {}, {}, ""


def _try_ats_api(raw_home: str, country: str = DEFAULT_COUNTRY) -> Tuple[Optional[str], dict, dict, str]:
    """
    Scan homepage HTML for ATS indicators; call the matching platform API.
    Returns (listings_text, url_map, jd_map, careers_url).
    All empty/None when no ATS detected or API call fails.
    """
    # Workday — needs company + wd-number + site all at once
    wd_m = _WORKDAY_RE.search(raw_home)
    if wd_m:
        company = wd_m.group(1).lower()
        wdn     = wd_m.group(2).lower()  # e.g. "wd3"
        site    = (wd_m.group(3) or "").strip("/") or "jobs"
        slug    = f"{company}|{wdn}|{site}"
        log.info("ATS detected: workday (slug=%s) — trying RSS feed", slug)
        try:
            listings, url_map, jd_map = _api_workday(slug, country)
            if listings:
                n = listings.count("\n") + 1
                log.info("Workday RSS returned %d roles", n)
                return listings, url_map, jd_map, wd_m.group(0)
            log.info("Workday RSS empty — will scrape")
        except Exception as exc:
            log.debug("Workday API failed: %s", exc)

    # All other ATS platforms
    for ats_name, handler in _ATS_HANDLERS:
        pattern = _ATS_SLUGS.get(ats_name)
        if not pattern:
            continue
        m = pattern.search(raw_home)
        if not m:
            continue
        slug = m.group(1).strip("/").split("/")[0].lower()
        if not slug or slug in _BAD_SLUGS:
            continue
        log.info("ATS detected: %s (slug=%s) — calling API", ats_name, slug)
        try:
            listings, url_map, jd_map = handler(slug, country)
            if listings:
                n = listings.count("\n") + 1
                log.info("ATS API (%s) returned %d roles", ats_name, n)
                careers_url = m.group(0) if m.group(0).startswith("http") else ""
                return listings, url_map, jd_map, careers_url
            log.info("ATS API (%s) returned 0 listings — will scrape", ats_name)
        except Exception as exc:
            log.debug("ATS API failed (%s/%s): %s", ats_name, slug, exc)

    # ConnectID (e.g. careers.rolls-royce.com) — detected via network interception
    ci_listings, ci_url_map, ci_jd_map, ci_url = _try_connectid_api(country)
    if ci_listings:
        return ci_listings, ci_url_map, ci_jd_map, ci_url

    # Gaia/Socially SPA (e.g. joinus.gknaerospace.com)
    gaia_listings, gaia_url_map, gaia_jd_map, gaia_url = _try_gaia_api(raw_home, country)
    if gaia_listings:
        return gaia_listings, gaia_url_map, gaia_jd_map, gaia_url

    return None, {}, {}, ""


def _same_site(url: str, base_url: str) -> bool:
    """True if url shares the root domain with base_url (subdomains allowed)."""
    base_domain = urlparse(base_url).netloc.lstrip("www.")
    url_domain = urlparse(url).netloc.lstrip("www.")
    return url_domain == base_domain or url_domain.endswith("." + base_domain)


def _find_links(raw_html: str, base_url: str, patterns: List[str]) -> List[str]:
    """Find all links matching any pattern, same site (including subdomains)."""
    soup = BeautifulSoup(raw_html, "lxml")
    seen, results = set(), []
    base_normalized = base_url.rstrip("/")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        combined = (href + " " + a.get_text(strip=True)).lower()
        if any(re.search(p, combined) for p in patterns):
            full = urljoin(base_url, href)
            if full.rstrip("/") == base_normalized:  # skip self-links
                continue
            if _same_site(full, base_url) and full not in seen:
                seen.add(full)
                results.append(full)
    return results


def _find_all_href(raw_html: str, base_url: str) -> List[str]:
    """Return all same-site hrefs (including subdomains)."""
    soup = BeautifulSoup(raw_html, "lxml")
    seen, results = set(), []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        if _same_site(full, base_url) and full not in seen:
            seen.add(full)
            results.append(full)
    return results


def _ask_claude(prompt: str, task: str = "link_pick", max_tokens: int = 1024) -> str:
    return llm_client.complete(task, prompt, max_tokens=max_tokens)


def _extract_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.upper().startswith(field + ":"):
            return line[len(field) + 1:].strip()
    return ""


def _find_careers_url(base_url: str, raw_home: str) -> Optional[str]:
    """
    Multi-strategy careers page finder:
    1. Links on homepage matching careers patterns
    2. Common path guessing
    3. Sitemap.xml parsing
    4. Claude picks from all found links if none obvious
    """
    # Strategy 1: Pattern-matched links on homepage (including subdomains)
    candidates = _find_links(raw_home, base_url, CAREERS_PATTERNS)
    if candidates:
        _STRONG_CAREER = re.compile(r"career|/jobs|vacanc|/job-")
        candidates.sort(key=lambda u: 0 if _STRONG_CAREER.search(u.lower()) else 1)
        log.info("Found careers link via pattern: %s", candidates[0])
        return candidates[0]

    # Strategy 1b: Check all same-site subpages linked from homepage
    subpages = _find_all_href(raw_home, base_url)[:12]
    for subpage in subpages:
        if subpage == base_url:
            continue
        raw_sub, _ = _fetch_raw_html(subpage)
        if not raw_sub:
            continue
        sub_candidates = _find_links(raw_sub, base_url, CAREERS_PATTERNS)
        if sub_candidates:
            log.info("Found careers link via subpage %s: %s", subpage, sub_candidates[0])
            return sub_candidates[0]
        time.sleep(0.2)

    # Strategy 2: Common path brute-force (including .html variants for static sites)
    for path in COMMON_CAREERS_PATHS:
        url = base_url.rstrip("/") + path
        raw, content = _fetch_raw_html(url)
        if content and len(content) > 200:
            log.info("Found careers page via path: %s", url)
            return url
        time.sleep(0.1)

    # Strategy 2.5: ATS URLs embedded in raw HTML (Greenhouse, Lever, Workable etc.)
    ats_url = _ATS_URL_PATTERN.search(raw_home)
    if ats_url:
        log.info("Found ATS careers URL: %s", ats_url.group())
        return ats_url.group()

    # Strategy 3: Sitemap
    for sitemap_path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap"]:
        sitemap_url = base_url.rstrip("/") + sitemap_path
        raw, _ = _fetch_raw_html(sitemap_url)
        if raw:
            matches = re.findall(r'<loc>(.*?)</loc>', raw)
            for loc in matches:
                if any(re.search(p, loc.lower()) for p in CAREERS_PATTERNS):
                    log.info("Found careers URL in sitemap: %s", loc)
                    return loc

    # Strategy 4: Ask Claude to identify the careers URL from all homepage links
    all_links = _find_all_href(raw_home, base_url)
    if all_links:
        links_text = "\n".join(all_links[:80])
        prompt = f"""From this list of links on {base_url}, identify the one most likely to be a careers/jobs page.
Links:
{links_text}

Reply with JUST the URL, or "none" if no careers page is evident.
"""
        try:
            answer = _ask_claude(prompt).strip()
            if answer.lower() != "none" and answer.startswith("http"):
                raw, content = _fetch_raw_html(answer)
                if content and len(content) > 200:
                    log.info("Claude identified careers page: %s", answer)
                    return answer
        except Exception:
            pass

    return None


def _scrape_company_context(base_url: str, raw_home: str, home_content: str) -> str:
    """Scrape homepage + about page for mission/vision/focus."""
    about_candidates = _find_links(raw_home, base_url, ABOUT_PATTERNS)
    about_content = ""
    if about_candidates:
        _, about_content = _fetch_raw_html(about_candidates[0])
        about_content = (about_content or "")[:3000]

    combined = f"{home_content[:2000]}\n\n{about_content}".strip()
    if not combined:
        return ""

    prompt = f"""Extract key company information from the content below.

CONTENT:
---
{combined}
---

Reply in this EXACT format:
MISSION: <company's mission or purpose — one sentence>
FOCUS: <2-3 key technology/product areas>
VISION: <long-term goal if stated, else "not stated">
NOTABLE: <specific projects, products, clients, or achievements>
"""
    try:
        return _ask_claude(prompt)
    except Exception as exc:
        log.warning("Company context extraction failed: %s", exc)
        return ""


def find_matching_role(company_website: str, company_name: str, country: str = DEFAULT_COUNTRY) -> dict:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-GB",
        )
        page = context.new_page()
        _local.playwright_page = page
        _local.playwright_instance = pw
        _local.connectid_api_base = None
        _local.connectid_careers_url = None

        def _on_response(response):
            m = _CONNECTID_RE.search(response.url)
            if m:
                _local.connectid_api_base = m.group(1)
                try:
                    _local.connectid_careers_url = page.url
                except Exception:
                    pass

        page.on("response", _on_response)
        try:
            return _find_matching_role(company_website, company_name, country)
        finally:
            _local.playwright_page = None
            _local.playwright_instance = None
            _local.connectid_api_base = None
            _local.connectid_careers_url = None
            browser.close()


def _find_matching_role(company_website: str, company_name: str, country: str = DEFAULT_COUNTRY) -> dict:
    """
    Returns:
      role_title, role_description, is_open_application, careers_url,
      company_mission, company_focus, company_vision, company_notable
    """
    result = {
        "role_title": "Open Application",
        "role_description": "",
        "is_open_application": True,
        "careers_url": "",
        "company_mission": "",
        "company_focus": "",
        "company_vision": "",
        "company_notable": "",
    }

    if not company_website:
        return result

    if not company_website.startswith("http"):
        company_website = "https://" + company_website
    base_url = company_website.rstrip("/")

    # 1. Fetch homepage
    log.info("Visiting: %s", base_url)
    raw_home, home_content = _fetch_raw_html(base_url)
    if not raw_home:
        log.warning("Could not fetch homepage for %s", company_name)
        return result

    # 2. Company context (mission/vision/focus)
    log.info("Extracting company context for %s", company_name)
    context_raw = _scrape_company_context(base_url, raw_home, home_content or "")
    result["company_mission"] = _extract_field(context_raw, "MISSION")
    result["company_focus"]   = _extract_field(context_raw, "FOCUS")
    result["company_vision"]  = _extract_field(context_raw, "VISION")
    result["company_notable"] = _extract_field(context_raw, "NOTABLE")

    cfg = get_country_config(country)

    # 3. Try ATS API first — structured JSON, handles JS-rendered pages
    log.info("Checking for ATS integration at %s", company_name)
    ats_listings, url_map, jd_map, ats_url = _try_ats_api(raw_home, country)

    if ats_listings:
        careers_content = ats_listings
        if ats_url:
            result["careers_url"] = ats_url
    else:
        # 3b. Scrape careers page the traditional way
        log.info("No ATS on homepage — finding careers page at %s", company_name)
        careers_url = _find_careers_url(base_url, raw_home)
        if not careers_url:
            log.info("No careers page found for %s — open application", company_name)
            return result

        result["careers_url"] = careers_url
        log.info("Careers page: %s", careers_url)
        time.sleep(0.5)

        raw_careers, careers_content = _fetch_raw_html(careers_url)
        if not careers_content or len(careers_content) < 100:
            log.info("Careers page empty or unreadable for %s", company_name)
            return result

        # Re-run ATS detection on the careers page (ATS links often only appear there)
        ats_listings2, url_map, jd_map, ats_url2 = _try_ats_api(raw_careers or "", country)
        if ats_listings2:
            log.info("ATS detected on careers page — using API data")
            careers_content = ats_listings2
            if ats_url2:
                result["careers_url"] = ats_url2
        else:
            jd_map = {}
            structured, url_map = _extract_job_listings(raw_careers or "", careers_url, country)
            if structured:
                log.info("Structured listings: %d roles", structured.count("\n") + 1)
                careers_content = structured

    # 5. Find best matching role (Sonnet for accuracy) — AM first, then any engineering
    prompt = f"""You are helping Prateek Sahni (MEng Mechanical & Manufacturing Engineering, additive manufacturing and process engineer) find a relevant {cfg['role_match_phrase']} role.

CANDIDATE PROFILE:
{get_cv_context(country)}

COMPANY: {company_name}
OPEN ROLES:
---
{careers_content[:MAX_CONTENT_CHARS]}
---

Select the SINGLE best matching role using this priority:
  1. Additive manufacturing / 3D printing roles (AM, LPBF, SLS, SLM, FDM, SLA, binder jetting)
  2. Process engineering / process development / manufacturing engineer
  3. Any engineering role relevant to a Mechanical & Manufacturing Engineering MEng graduate — including Mechanical Engineer, R&D Engineer, Materials Engineer, Production Engineer, Quality Engineer, Design Engineer, Systems Engineer, Project Engineer

RULES:
  - Role must be based ANYWHERE in {cfg['label']} (e.g. {cfg['example_cities']} — these are just examples, not a restriction) OR remote/hybrid open to {cfg['label']} applicants
  - Do NOT select roles unrelated to engineering (e.g. sales, HR, accounting)
  - Do NOT select operator or technician roles (e.g. Machine Operator, AM Technician, Lab Technician, Production Technician)
  - If multiple roles qualify, pick the highest priority one; if tied, pick the most senior
  - If NONE of the listed roles are engineering roles suitable for this candidate, reply: ROLE: None

Reply in this EXACT format (no extra text):
ROLE: <exact job title as listed, or "None">
DESCRIPTION: <one sentence: what the role involves and its location>
"""
    try:
        response = _ask_claude(prompt, task="role_match", max_tokens=512)
    except Exception as exc:
        log.error("Role matching failed: %s", exc)
        return result

    role_title = _extract_field(response, "ROLE")
    role_desc  = _extract_field(response, "DESCRIPTION")

    if role_title and role_title.lower() not in ("none", "n/a", ""):
        result["role_title"] = role_title
        result["is_open_application"] = False
        log.info("Matched role: '%s' at %s", role_title, company_name)

        # 6. Get JD — from API response first, then fetch page if needed
        bare = role_title.split(" (")[0].strip()
        jd_text = jd_map.get(role_title) or jd_map.get(bare)
        if not jd_text:
            role_url = url_map.get(role_title) or url_map.get(bare, "")
            if role_url:
                log.info("Fetching JD for '%s' from %s", role_title, role_url)
                jd_text = _fetch_jd_text(role_url)
        result["role_description"] = _trim_jd(jd_text, 1800) if jd_text else role_desc
    else:
        log.info("No matching %s role at %s — open application", cfg["label"], company_name)

    return result
