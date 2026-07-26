"""
Job Intelligence / Lead Scanner for VFF Utility Engineering
-------------------------------------------------------------
Секое утро (преку GitHub Actions):
  1. Го проверува career URL-то на секоја компанија од companies.csv за
     MRE/pole loading клучни зборови (Pole Loading, Make Ready, O-Calc,
     Joint Use, NESC, итн.) — тоа е сигнал за ВОЛУМЕН работа.
  2. За компаниите каде најде волумен, автоматски проба неколку чести
     патеки на истиот домен (/vendors, /partners, /subcontractors,
     /become-a-vendor, /procurement) за да провери дали компанијата
     ЈАВНО спомнува subcontractor/vendor програма — тоа е посилен сигнал
     дека реално прифаќаат надворешни партнери, не само вработени.
  3. Компаниите со и двата сигнали се означуваат како 🔥 HOT LEAD.

Ова НЕ е алатка за аплицирање на огласи — целта е откривање компании за
директен B2B subcontracting контакт.

Не гарантира 100% точност — помошна алатка, не замена за реален outreach.
"""

import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Конфигурација
# ---------------------------------------------------------------------------

# Сигнал 1: волумен работа (се бара на career страницата)
JOB_KEYWORDS = [
    "pole loading",
    "make ready",
    "make-ready",
    "o-calc",
    "ocalc",
    "spidacalc",
    "spida calc",
    "joint use",
    "joint-use",
    "nesc",
    "katapult",
    "osp design",
    "outside plant",
]

# Сигнал 2: отвореност за надворешни партнери (се бара на /vendors, /partners итн.)
VENDOR_KEYWORDS = [
    "become a vendor",
    "become a subcontractor",
    "subcontractor network",
    "vendor network",
    "vendor registration",
    "supplier diversity",
    "partner with us",
    "capacity partner",
    "join our network",
    "prospective vendor",
    "prequalification",
    "prospective subcontractor",
]

# Патеки што автоматски се пробуваат на истиот домен, само за компаниите
# каде веќе најдовме волумен работа (за да не се губи време без потреба).
VENDOR_PATHS = [
    "/vendors",
    "/become-a-vendor",
    "/subcontractors",
    "/partners",
    "/supplier-diversity",
    "/procurement",
]

CONTEXT_CHARS = 160
PAGE_TIMEOUT_MS = 20000
VENDOR_PAGE_TIMEOUT_MS = 8000
NAV_WAIT_MS = 2500


@dataclass
class CompanyResult:
    name: str
    url: str
    status: str = "ok"  # ok | error | no_matches
    job_hits: list = field(default_factory=list)  # (keyword, context)
    vendor_hit: tuple = None  # (vendor_url, keyword, context) or None
    error_msg: str = ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def find_hits(raw_text: str, keywords):
    text = normalize(raw_text)
    hits = []
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text):
            start = max(0, m.start() - CONTEXT_CHARS // 2)
            end = min(len(text), m.end() + CONTEXT_CHARS // 2)
            context = text[start:end].strip()
            hits.append((kw, context))
    return hits


def domain_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def check_vendor_program(browser, base_url: str):
    """Проба неколку чести патеки на домен за vendor/subcontractor програма.
    Враќа (url, keyword, context) на првиот погодок, или None."""
    root = domain_root(base_url)
    for path in VENDOR_PATHS:
        candidate = root + path
        page = None
        try:
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ))
            resp = page.goto(candidate, timeout=VENDOR_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            if resp is None or resp.status >= 400:
                continue
            page.wait_for_timeout(NAV_WAIT_MS)
            text = page.inner_text("body")
            hits = find_hits(text, VENDOR_KEYWORDS)
            if hits:
                kw, context = hits[0]
                return (candidate, kw, context)
        except Exception:
            continue
        finally:
            if page is not None:
                page.close()
    return None


def scan_company(browser, name: str, url: str) -> CompanyResult:
    result = CompanyResult(name=name, url=url)
    page = None
    try:
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(NAV_WAIT_MS)
        text = page.inner_text("body")
        hits = find_hits(text, JOB_KEYWORDS)
        if hits:
            result.job_hits = hits
            result.status = "ok"
        else:
            result.status = "no_matches"
    except Exception as e:
        result.status = "error"
        result.error_msg = str(e)[:200]
    finally:
        if page is not None:
            page.close()

    # Втор сигнал: пробај vendor страници САМО ако веќе имаме волумен работа
    if result.status == "ok" and result.job_hits:
        try:
            result.vendor_hit = check_vendor_program(browser, url)
        except Exception:
            result.vendor_hit = None

    return result


def load_companies(csv_path: str):
    companies = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("company_name") or "").strip()
            url = (row.get("career_url") or "").strip()
            if name and url:
                companies.append((name, url))
    return companies


def build_report(results):
    hot_leads = []    # и волумен И jавна vendor програма
    leads = []         # само волумен работа
    no_matches = []
    errors = []

    for r in results:
        if r.status == "error":
            errors.append(r)
        elif r.status == "no_matches":
            no_matches.append(r)
        elif r.vendor_hit:
            hot_leads.append(r)
        else:
            leads.append(r)

    lines = []
    lines.append("JOB INTELLIGENCE — дневни B2B leads")
    lines.append(f"Проверени компании: {len(results)}")
    lines.append(f"🔥 HOT (волумен + јавна vendor програма): {len(hot_leads)}")
    lines.append(f"Leads (само волумен работа): {len(leads)}")
    lines.append(f"Без совпаѓања денес: {len(no_matches)}")
    lines.append(f"Грешки при отворање: {len(errors)}")
    lines.append("")

    lines.append("=" * 70)
    lines.append("🔥 HOT LEADS — имаат волумен И јавно спомнуваат vendor/subcontractor програма")
    lines.append("=" * 70)
    if not hot_leads:
        lines.append("(нема денес — ова е ретко, но кога се појави е најсилен сигнал)")
    for r in hot_leads:
        lines.append("")
        lines.append(f"• {r.name}")
        lines.append(f"  Career: {r.url}")
        vurl, vkw, vctx = r.vendor_hit
        lines.append(f"  Vendor страница: {vurl}")
        lines.append(f"  - [{vkw}] ...{vctx}...")

    lines.append("")
    lines.append("=" * 70)
    lines.append("LEADS — имаат волумен работа (контактирај директно, немаат јавна vendor страница)")
    lines.append("=" * 70)
    if not leads:
        lines.append("(нема нови leads денес)")
    for r in leads:
        lines.append("")
        lines.append(f"• {r.name}")
        lines.append(f"  {r.url}")
        seen_kw = set()
        for kw, context in r.job_hits:
            if kw in seen_kw:
                continue
            seen_kw.add(kw)
            lines.append(f"  - [{kw}] ...{context}...")

    if errors:
        lines.append("")
        lines.append("=" * 70)
        lines.append("ГРЕШКИ — провери ги рачно овие линкови")
        lines.append("=" * 70)
        for r in errors:
            lines.append(f"• {r.name} — {r.url}  ({r.error_msg})")

    return "\n".join(lines), len(hot_leads), len(leads)


def write_report_file(subject: str, body: str):
    with open("report_title.txt", "w", encoding="utf-8") as f:
        f.write(subject)
    with open("report_body.txt", "w", encoding="utf-8") as f:
        f.write(body)


def main():
    csv_path = os.environ.get("COMPANIES_CSV", "companies.csv")
    companies = load_companies(csv_path)
    print(f"Вчитани {len(companies)} компании од {csv_path}")

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for i, (name, url) in enumerate(companies, 1):
            print(f"[{i}/{len(companies)}] Проверувам: {name} ({url})")
            result = scan_company(browser, name, url)
            results.append(result)
            time.sleep(0.5)
        browser.close()

    report_body, hot_count, lead_count = build_report(results)
    subject = f"Job Intelligence — {hot_count} HOT + {lead_count} leads денес"
    write_report_file(subject, report_body)

    print("\n\n" + report_body)


if __name__ == "__main__":
    sys.exit(main())
