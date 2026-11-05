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
    "osp design", "outside plant", "joint use", "pole attachment", "osp designer",
    "qgis", "arcgis", "gis analyst", "gis technician", "gis specialist",
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
    "georgian", "armenian", "azerbaijani", "kazakh", "uzbek", "thai", "vietnamese",
    "indonesian", "malay", "tagalog", "filipino", "bengali", "urdu", "persian", "farsi",
    "albanian", "lithuanian", "latvian", "estonian", "slovenian", "tamil", "telugu",
    "swahili", "amharic", "kurdish", "pashto", "mongolian", "nepali", "punjabi",
    "marathi", "gujarati", "afrikaans", "belarusian", "maltese", "irish", "welsh",
]
OK_LANGS = {"english", "serbian", "macedonian", "croatian", "bosnian", "montenegrin"}
LANG_REQ_RE = re.compile(
    r"(native|fluent|fluency|proficien\w*|advanced|c1|c2)\s+(level\s+)?(in\s+|of\s+)?(" +
    "|".join(w for w in LANG_WORDS if w.isalpha()) + r")\b", re.I)
# not easy / not what he wants for the easy-work bucket
SIDE_EXCLUDE = [
    "senior", "director", "head of", "vice president", " vp ", "lead ", "principal",
    "manager", "developer", "software", "devops", "programmer", "engineer",
    "sales", "business development", "account executive", "recruiter", "marketing",
    "sql", "database", "data analyst", "data engineer", "data scientist", "analytics",
    "business intelligence", "bi analyst", "etl", "python", "java", "javascript",
]
SCAM_TERMS = [
    "registration fee", "pay a fee", "training fee", "telegram", "whatsapp",
    "bank details", "purchase equipment", "wire transfer", "crypto wallet",
    "no experience needed earn", "earn up to $", "daily payout",
]
# he wants chat and email only, no live voice or video work
CALL_TERMS = [
    "phone support", "phone calls", "phone-based", "phone based", "by phone",
    "on the phone", "over the phone", "telephone", "inbound call", "outbound call",
    "cold call", "call center", "call centre", "contact center", "voice support",
    "handle calls", "answer calls", "take calls", "live calls", "video call",
    "video support", "screen shar", "screenshar", "zoom call", "voice channel",
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

# US-only signals in title or text, even when the board says "Anywhere"
US_TITLE_RE = re.compile(
    r"(^|[^a-z])(us remote|us-remote|us based|us-based|u\.s\.|usa|united states|us only|us-only|"
    r"\(us\)|\(usa\)|us citizens?)([^a-z]|$)|^\s*us\s", re.I)
US_TEXT_SIGNS = [
    "high school diploma or ged", "or ged", " ged ", "veterans", "401(k)", "401k", "w-2", "w2 ",
    "us-based", "u.s.-based", "based in the us", "based in the united states",
    "must reside in the", "us time zones", "us timezone", "eastern time", "pacific time",
    "central time", "est hours", "cst hours", "pst hours",
]
# work that means speaking to people, even if the ad does not say "call"
VOICE_SIGNS = [
    "verbal communication", "ambient noise", "quiet workspace", "door that closes",
    "headset", "speak with", "speaking with", "talk to customers", "talking to customers",
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


TARGET_TERMS = ["make ready", "pole loading", "pole attachment", "o-calc", "katapult",
                "joint use", "outside plant", "osp design", "qgis", "gis technician",
                "gis analyst"]


def fetch_targeted():
    """Keyword searches for MRE and utility/telecom design work on boards that support search."""
    import time
    out, calls, fails = [], 0, 0
    for term in TARGET_TERMS:
        for url, kind in (
            ("https://jobicy.com/api/v2/remote-jobs?count=50&tag=" + term.replace(" ", "+"), "jobicy"),
            ("https://himalayas.app/jobs/api/search?q=" + term.replace(" ", "+"), "himalayas"),
        ):
            calls += 1
            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json().get("jobs", [])
                for j in data:
                    if kind == "jobicy":
                        out.append(job(j.get("jobTitle"), j.get("companyName"), j.get("jobGeo"),
                                       j.get("url"), j.get("pubDate"),
                                       j.get("jobDescription") or j.get("jobExcerpt"),
                                       j.get("jobIndustry"), "Jobicy search"))
                    else:
                        restr = j.get("locationRestrictions") or []
                        out.append(job(j.get("title"), j.get("companyName"),
                                       ", ".join(restr) if restr else "Worldwide",
                                       j.get("applicationLink") or j.get("guid"), j.get("pubDate"),
                                       j.get("description") or j.get("excerpt"),
                                       j.get("categories"), "Himalayas search"))
            except Exception:
                fails += 1
            time.sleep(2)
    print(f"targeted searches: {calls} calls, {fails} failed, {len(out)} results")
    return out


# ---- Macedonia / Skopje local boards ------------------------------------------
LOCAL_SEEN_FILE = "local_seen.json"
LOCAL_PAGES = [
    "https://kariera.mk/rabota-vo-skopje",
    "https://kariera.mk/rabota-vo-skopje?page=2",
    "https://kariera.mk/rabota-vo-skopje?page=3",
    "https://www.vrabotuvanje.com.mk/en/job-search?locations=%D0%A1%D0%BA%D0%BE%D0%BF%D1%98%D0%B5",
]
LOCAL_EXCLUDE = [
    "programer", "programmer", "developer", "software", "devops", "sql", "baza-na", "data-analy",
    "analiticar", "analitichar", "doktor", "medicinsk", "sestra", "lekar", "farmac", "stomatolog",
    "prodazen", "prodavac", "prodazno", "recruiter", "marketing", "rakovoditel", "direktor", "menadzer", "manager", "telefonska", "prodazh", "prodazb",
    "kelner", "sanker", "bravar", "vulkanizer", "magacioner", "vozac", "gotvach", "pica-majstor",
    "chistac", "ciscenje", "nemski", "german", "senior",
]
LOCAL_FIT = [
    "administrat", "asistent", "referent", "koordinator", "recepcion", "poddrska",
    "korisnick", "inzhener", "inzener", "engineer", "kontrolor", "sekretar", "support",
    "assistant", "office", "customer", "podatoci", "data-entry", "gis",
]


def fetch_local():
    from html import unescape as html_unescape
    from urllib.parse import urljoin
    out, fails = [], 0
    for page in LOCAL_PAGES:
        try:
            r = requests.get(page, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            fails += 1
            print("local fetch failed:", page, type(e).__name__, str(e)[:100])
            continue
        host = "kariera.mk" if "kariera" in page else "vrabotuvanje.com.mk"
        titles = {}
        for m in re.finditer(r'<a\b[^>]*href="([^"]*?/job/[^"#]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
            url = urljoin(page, html_unescape(m.group(1))).split("?")[0]
            text = strip_html(m.group(2))
            good = len(text) > 3 and not any(w in text.lower() for w in ("погледни", "аплицирај", "view", "apply", "повеќе"))
            if url not in titles or (good and not titles[url]):
                titles[url] = text if good else ""
        for url, text in titles.items():
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            out.append(job(text or slug.replace("-", " "), "", "Skopje", url, None, "", [slug], "Local: " + host))
    print(f"local pages: {len(LOCAL_PAGES)}, failed {fails}, links {len(out)}")
    return out


def classify_local(j):
    slug = " ".join(j["tags"]).lower()
    t = (j["title"] + " " + slug).lower()
    if any(x in t for x in LOCAL_EXCLUDE):
        STATS["local: not for him"] = STATS.get("local: not for him", 0) + 1
        return None
    if any(x in t for x in LOCAL_FIT):
        return ("local", "fit")
    return ("local", "other")


def load_local_seen():
    import json
    try:
        return set(json.load(open(LOCAL_SEEN_FILE, encoding="utf-8")))
    except Exception:
        return set()


def save_local_seen(seen):
    import json
    json.dump(sorted(seen)[-3000:], open(LOCAL_SEEN_FILE, "w", encoding="utf-8"))


FETCHERS = [fetch_remotive, fetch_remotive_support, fetch_remoteok, fetch_jobicy,
            fetch_wwr, fetch_targeted]


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

    if any(s in desc for s in SCAM_TERMS):
        return drop("scam signals")
    if any(r.search(desc) for r in US_RE):
        return drop("US-only text")
    loc = loc_class(j["location"])
    if loc == "no":
        return drop("location not open to him")
    if US_TITLE_RE.search(j["title"]) or any(x in desc for x in US_TEXT_SIGNS):
        return drop("US-only text")
    if any(b in head for b in BAD_TERMS):
        if loc not in ("ok", "eu"):
            return None
        if any(w in title for w in LANG_WORDS) or LANG_REQ_RE.search(desc):
            return drop("needs a language he lacks")
        m = re.search(r"(?:network|trainers?|contributor|annotator)\s*[-–:]\s*([a-z]+)", title.strip())
        if m and m.group(1) not in OK_LANGS and m.group(1) not in ("remote", "video", "audio",
                                                                  "text", "image", "data",
                                                                  "freelance", "contract"):
            return drop("needs a language he lacks")
        years = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", desc)]
        if years and max(years) >= 2:
            return drop("asks for 2+ years of experience")
        return ("ai", "ok")
    if hits(CORE_RE, head):
        if any(w in title for w in (" sales ", "business development", "marketing", "recruiter",
                                    "account executive", "account manager", "developer", "software",
                                    "programmer", "devops", "full stack", "full-stack", "backend",
                                    "back-end", "frontend", "front-end", "data scientist",
                                    " ux", "ux/", " ui ", "ui/", "product design", "graphic",
                                    "brand", "visual design", "web design", "creative",
                                    "illustrat", "animator", "head of", "director",
                                    "vice president", "chief ", "sql", "database",
                                    "data analyst", "data engineer", "analytics",
                                    "business intelligence")):
            return drop("senior / dev / sales title")
        return ("core", "ok" if loc == "ok" else "maybe")
    if hits(SIDE_RE, head) and loc in ("ok", "eu"):
        if any(w in title for w in LANG_WORDS) or LANG_REQ_RE.search(desc):
            return drop("needs a language he lacks")
        if any(w in title for w in SIDE_EXCLUDE):
            return drop("senior / dev / sales title")
        if any(c in title or c in desc for c in CALL_TERMS + VOICE_SIGNS):
            return drop("needs calls / video / screen sharing")
        years = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", desc)]
        if years and max(years) >= 2:
            return drop("asks for 2+ years of experience")
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

    local_seen = load_local_seen()
    new_local = set()
    seen, buckets = set(), {("core", "ok"): [], ("core", "maybe"): [], ("side", "ok"): [],
                            ("ai", "ok"): [], ("local", "fit"): [], ("local", "other"): []}
    for j in all_jobs:
        if not j["url"] or j["url"] in seen:
            continue
        if j["source"].startswith("Local"):
            if j["url"] in local_seen:
                continue
            c = classify_local(j)
            seen.add(j["url"])
            if c:
                buckets[c].append(j)
                new_local.add(j["url"])
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

    def line(j):
        d = j["published"].strftime("%Y-%m-%d") if j["published"] else "?"
        loc = j["location"] or "n/a"
        return f"- **{j['title']}** | {j['company'] or 'n/a'} | {loc} | {j['source']} | {d}\n  {j['url']}"

    parts = []
    titles = {
        ("core", "ok"): "Engineering/design, open worldwide",
        ("core", "maybe"): "Engineering/design, location unclear (check manually)",
        ("side", "ok"): "Easy remote work (chat, email, admin, data entry), open to him",
        ("ai", "ok"): "AI training / annotation gigs, open to him (check pay and platform first)",
        ("local", "fit"): "Skopje / North Macedonia, office, admin, support, engineering (check on-site vs remote)",
        ("local", "other"): "Skopje / North Macedonia, other (shop, hospitality, production...)",
    }
    for key in [("core", "ok"), ("core", "maybe"), ("side", "ok"), ("ai", "ok"),
                ("local", "fit"), ("local", "other")]:
        items = buckets[key][:12 if key == ("local", "other") else 25]
        if items:
            parts.append(f"## {titles[key]} ({len(buckets[key])})\n\n" + "\n".join(line(j) for j in items))
    parts.append("Sources: [Remotive](https://remotive.com), [Remote OK](https://remoteok.com), "
                 "[Jobicy](https://jobicy.com), "
                 "[We Work Remotely](https://weworkremotely.com)\n\n"
                 "Scan status: " + ", ".join(status) +
                 "\n\nFiltered out: " + (", ".join(f"{k}: {v}" for k, v in STATS.items()) or "nothing"))

    save_local_seen(local_seen | new_local)
    with open("remote_title.txt", "w", encoding="utf-8") as f:
        f.write(f"Remote jobs {now.strftime('%Y-%m-%d')}: {total} new")
    with open("remote_body.txt", "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
