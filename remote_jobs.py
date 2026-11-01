"""Daily scan of worldwide-remote job boards for MRE / telecom / utility design work.

Sources (public APIs/feeds, no login): Remotive, Remote OK, Jobicy, Himalayas, We Work Remotely.
Writes remote_title.txt and remote_body.txt only if something relevant was found.
"""
import re
import sys
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-intel-remote/1.0)"}
import os
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "40"))  # daily run, small overlap
TIMEOUT = 30

# ---- what we look for ------------------------------------------------------
CORE_TERMS = [
    "make ready", "make-ready", "pole loading", "o-calc", "ocalc", "katapult",
    "osp", "outside plant", "joint use", "pole attachment", "fiber design",
    "fttx", "fttp", "telecom design", "telecom engineer", "utility design",
    "network design", "fiber engineer", "cad drafter", "autocad", "drafter",
    "drafting", "engineering technician", "gis", "permit", "permitting",
]
SIDE_TERMS = [
    "data entry", "virtual assistant", "administrative assistant", "admin assistant",
    "it support", "help desk", "helpdesk", "customer support", "customer service",
    "customer care", "customer success", "support agent", "support specialist",
    "support representative", "support associate", "chat support", "email support",
    "live chat", "livechat", "content moderator", "moderator", "transcription",
    "transcriber", "proofreader", "proofreading", "back office", "order processing",
    "data quality", "document control", "office assistant",
    "executive assistant", "personal assistant", "coordinator",
]
# language requirements he cannot meet (he speaks English, Serbian, Macedonian)
LANG_WORDS = [
    "german", "spanish", "french", "italian", "dutch", "polish", "portuguese",
    "turkish", "czech", "arabic", "japanese", "korean", "russian", "ukrainian",
    "hindi", "norwegian", "swedish", "danish", "finnish", "hungarian", "romanian",
    "bulgarian", "greek", "hebrew", "chinese", "mandarin", "icelandic", "slovak",
    "bilingual", "multilingual", "trilingual", "m/w/d", "(w/m/d)",
]
LANG_REQ_RE = re.compile(
    r"(native|fluent|fluency|proficien\w*|advanced|c1|c2)\s+(level\s+)?(in\s+|of\s+)?(" +
    "|".join(w for w in LANG_WORDS if w.isalpha()) + r")\b", re.I)
# not easy / not what he wants for the easy-work bucket
SIDE_EXCLUDE = [
    "senior", "director", "head of", "vice president", " vp ", "lead ", "principal",
    "manager", "developer", "software", "devops", "programmer", "engineer",
    "sales", "business development", "account executive", "recruiter", "marketing",
]
SCAM_TERMS = [
    "registration fee", "pay a fee", "training fee", "telegram", "whatsapp",
    "bank details", "purchase equipment", "wire transfer", "crypto wallet",
    "no experience needed earn", "earn up to $", "daily payout",
]
STATS = {}
# titles/tags containing these are dropped (AI-training gigs etc.)
BAD_TERMS = [
    "annotation", "annotator", "ai trainer", "ai training", "data labeling",
    "data labelling", "rater", "evaluator", "crowdsourc", "prompt writer",
]
# location text that counts as open to us
OK_LOC = ["worldwide", "anywhere", "global", "international", "all countries", "world wide"]
# location text that is unclear, shown separately for manual check
MAYBE_LOC = ["europe", "emea", "balkan", "serbia", "macedonia", "eu ", "cet", "european"]
# description text that kills a listing (US-only requirements)
US_ONLY_PATTERNS = [
    r"must (be|reside|live)[^.]{0,40}(united states|u\.s\.|\bus\b|usa)",
    r"(authorized|eligible|legally authorized) to work in the (united states|u\.s\.|us)\b",
    r"us (citizen|citizenship|work authorization)",
    r"u\.s\. (citizen|citizenship|work authorization)",
    r"must be (located|based) in the (united states|u\.s\.|us)\b",
    r"(usa|us|united states)[- ]only",
]


def term_re(t):
    return re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])")


CORE_RE = [term_re(t) for t in CORE_TERMS]
SIDE_RE = [term_re(t) for t in SIDE_TERMS]
US_RE = [re.compile(p, re.I) for p in US_ONLY_PATTERNS]


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_dt(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v, dt.timezone.utc)
        v = str(v).strip()
        if v.isdigit():
            return dt.datetime.fromtimestamp(int(v), dt.timezone.utc)
        try:
            d = dt.datetime.fromisoformat(v.replace(" ", "T", 1) if " " in v and "T" not in v else v)
        except ValueError:
            d = parsedate_to_datetime(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def job(title, company, location, url, published, desc, tags, source):
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        "published": parse_dt(published),
        "desc": desc or "",
        "tags": [str(t) for t in (tags or [])],
        "source": source,
    }


# ---- fetchers ---------------------------------------------------------------
def fetch_remotive():
    r = requests.get("https://remotive.com/api/remote-jobs", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [job(j.get("title"), j.get("company_name"), j.get("candidate_required_location"),
                j.get("url"), j.get("publication_date"), j.get("description"),
                j.get("tags"), "Remotive") for j in r.json().get("jobs", [])]


def fetch_remotive_support():
    r = requests.get("https://remotive.com/api/remote-jobs?category=customer-support",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [job(j.get("title"), j.get("company_name"), j.get("candidate_required_location"),
                j.get("url"), j.get("publication_date"), j.get("description"),
                j.get("tags"), "Remotive") for j in r.json().get("jobs", [])]


def fetch_remoteok():
    r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is a legal notice
        out.append(job(j.get("position"), j.get("company"), j.get("location"),
                       j.get("url"), j.get("date") or j.get("epoch"), j.get("description"),
                       j.get("tags"), "Remote OK"))
    return out


def fetch_jobicy():
    r = requests.get("https://jobicy.com/api/v2/remote-jobs?count=100", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [job(j.get("jobTitle"), j.get("companyName"), j.get("jobGeo"), j.get("url"),
                j.get("pubDate"), j.get("jobDescription") or j.get("jobExcerpt"),
                j.get("jobIndustry"), "Jobicy") for j in r.json().get("jobs", [])]


def fetch_himalayas():
    out = []
    for offset in range(0, 100, 20):
        r = requests.get(f"https://himalayas.app/jobs/api?limit=20&offset={offset}",
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            restr = j.get("locationRestrictions") or []
            loc = ", ".join(restr) if restr else "Worldwide"
            out.append(job(j.get("title"), j.get("companyName"), loc,
                           j.get("applicationLink") or j.get("guid"), j.get("pubDate"),
                           j.get("description") or j.get("excerpt"),
                           j.get("categories"), "Himalayas"))
    return out


def fetch_wwr():
    r = requests.get("https://weworkremotely.com/remote-jobs.rss", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        raw_title = it.findtext("title") or ""
        company, _, title = raw_title.partition(": ")
        if not title:
            company, title = "", raw_title
        out.append(job(title, company, it.findtext("region") or "",
                       it.findtext("link"), it.findtext("pubDate"),
                       it.findtext("description"), [], "We Work Remotely"))
    return out


FETCHERS = [fetch_remotive, fetch_remotive_support, fetch_remoteok, fetch_jobicy,
            fetch_himalayas, fetch_wwr]


# ---- filtering --------------------------------------------------------------
def loc_class(location):
    l = location.lower().strip()
    if any(x in l for x in OK_LOC):
        return "ok"
    if l == "":
        return "maybe"
    if any(x in l for x in MAYBE_LOC):
        return "eu"
    return "no"


def hits(regexes, text):
    return {r.pattern for r in regexes if r.search(text)}


def classify(j):
    head = f"{j['title']} {' '.join(j['tags'])}".lower()
    title = f" {j['title'].lower()} "
    desc = strip_html(j["desc"]).lower()

    def drop(reason):
        STATS[reason] = STATS.get(reason, 0) + 1
        return None

    if any(b in head for b in BAD_TERMS):
        return drop("AI-training / annotation")
    if any(s in desc for s in SCAM_TERMS):
        return drop("scam signals")
    if any(r.search(desc) for r in US_RE):
        return drop("US-only text")
    loc = loc_class(j["location"])
    if loc == "no":
        return drop("location not open to him")
    if hits(CORE_RE, head) or len(hits(CORE_RE, desc)) >= 2:
        return ("core", "ok" if loc == "ok" else "maybe")
    if hits(SIDE_RE, head) and loc in ("ok", "eu"):
        if any(w in title for w in LANG_WORDS) or LANG_REQ_RE.search(desc):
            return drop("needs a language he lacks")
        if any(w in title for w in SIDE_EXCLUDE):
            return drop("senior / dev / sales title")
        return ("side", "ok")
    return None


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    all_jobs, status = [], []
    for f in FETCHERS:
        try:
            got = f()
            all_jobs += got
            status.append(f"{f.__name__[6:]}: {len(got)}")
        except Exception as e:  # one broken source must not kill the run
            status.append(f"{f.__name__[6:]}: ERROR {type(e).__name__}")
    print("sources ->", ", ".join(status))

    seen, buckets = set(), {("core", "ok"): [], ("core", "maybe"): [], ("side", "ok"): []}
    for j in all_jobs:
        if not j["url"] or j["url"] in seen:
            continue
        if j["published"] and j["published"] < cutoff:
            continue
        c = classify(j)
        if c in buckets:
            seen.add(j["url"])
            buckets[c].append(j)

    total = sum(len(v) for v in buckets.values())
    print("matches:", total)
    print("filtered out:", STATS)
    if total == 0:
        return 0

    def line(j):
        d = j["published"].strftime("%Y-%m-%d") if j["published"] else "?"
        loc = j["location"] or "n/a"
        return f"- **{j['title']}** | {j['company'] or 'n/a'} | {loc} | {j['source']} | {d}\n  {j['url']}"

    parts = []
    titles = {
        ("core", "ok"): "Engineering/design, open worldwide",
        ("core", "maybe"): "Engineering/design, location unclear (check manually)",
        ("side", "ok"): "Easy remote work (admin, data entry, IT support), open worldwide",
    }
    for key in [("core", "ok"), ("core", "maybe"), ("side", "ok")]:
        items = buckets[key][:25]
        if items:
            parts.append(f"## {titles[key]} ({len(buckets[key])})\n\n" + "\n".join(line(j) for j in items))
    parts.append("Sources: [Remotive](https://remotive.com), [Remote OK](https://remoteok.com), "
                 "[Jobicy](https://jobicy.com), [Himalayas](https://himalayas.app), "
                 "[We Work Remotely](https://weworkremotely.com)\n\n"
                 "Scan status: " + ", ".join(status) +
                 "\n\nFiltered out: " + (", ".join(f"{k}: {v}" for k, v in STATS.items()) or "nothing"))

    with open("remote_title.txt", "w", encoding="utf-8") as f:
        f.write(f"Remote jobs {now.strftime('%Y-%m-%d')}: {total} new")
    with open("remote_body.txt", "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
