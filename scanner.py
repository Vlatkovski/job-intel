"""
Job Intelligence / Lead Scanner for VFF Utility Engineering
-------------------------------------------------------------
Секое утро (преку GitHub Actions) го прегледува career страниците на
компаниите од companies.csv и бара клучни зборови релевантни за
Make Ready / Pole Loading работа (Pole Loading, Make Ready, O-Calc,
Joint Use, NESC, итн.).

ВАЖНО — намена: ова НЕ е алатка за аплицирање на огласи. Целта е да
идентификува компании кои МОМЕНТАЛНО активно вработуваат за MRE/OSP
позиции — тоа е сигнал дека имаат волумен работа и се добра мета за
директен B2B subcontracting контакт (не пријава преку career страница).
Дали конкретната огласена позиција бара US престој е небитно за таа цел.

Не гарантира 100% точност — ова е помошна алатка за откривање leads.
"""

import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Конфигурација — тука се менуваат зборовите, без да се допира логиката
# ---------------------------------------------------------------------------

KEYWORDS = [
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

# Фрази кои сигнализираат дека компанијата ЕКСПЛИЦИТНО не сака надворешни
# subcontractor/vendor фирми (различно од "employee мора да живее во US" —
# тоа веќе не е релевантно за нашата цел). Ретко се среќава, но ако се
# појави, вреди да се одвои во посебна секција за информација.
NO_SUBCONTRACTOR_PHRASES = [
    "no subcontractors",
    "not accepting new vendors",
    "no third-party vendors",
    "no staffing agencies",
    "no recruiters",
    "no agencies please",
]

CONTEXT_CHARS = 160
PAGE_TIMEOUT_MS = 25000
NAV_WAIT_MS = 3000


@dataclass
class CompanyResult:
    name: str
    url: str
    status: str = "ok"  # ok | error | no_matches
    hits: list = field(default_factory=list)  # (keyword, context, blocked_by)
    error_msg: str = ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def find_hits(raw_text: str):
    text = normalize(raw_text)
    hits = []
    for kw in KEYWORDS:
        for m in re.finditer(re.escape(kw), text):
            start = max(0, m.start() - CONTEXT_CHARS // 2)
            end = min(len(text), m.end() + CONTEXT_CHARS // 2)
            context = text[start:end].strip()

            blocked_by = None
            for phrase in NO_SUBCONTRACTOR_PHRASES:
                if phrase in context:
                    blocked_by = phrase
                    break

            hits.append((kw, context, blocked_by))
    return hits


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
        hits = find_hits(text)
        if hits:
            result.hits = hits
            result.status = "ok"
        else:
            result.status = "no_matches"
    except Exception as e:
        result.status = "error"
        result.error_msg = str(e)[:200]
    finally:
        if page is not None:
            page.close()
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
    leads = []       # компании со отворени MRE позиции — потенцијални B2B цели
    blocked = []      # експлицитно не сакаат subcontractors/vendors
    no_matches = []
    errors = []

    for r in results:
        if r.status == "error":
            errors.append(r)
            continue
        if r.status == "no_matches":
            no_matches.append(r)
            continue

        clean_hits = [h for h in r.hits if h[2] is None]
        blocked_hits = [h for h in r.hits if h[2] is not None]

        if clean_hits:
            leads.append((r, clean_hits))
        elif blocked_hits:
            blocked.append((r, blocked_hits))

    lines = []
    lines.append("JOB INTELLIGENCE — дневни B2B leads")
    lines.append(f"Проверени компании: {len(results)}")
    lines.append(f"Активно вработуваат MRE/OSP позиции (потенцијални leads): {len(leads)}")
    lines.append(f"Експлицитно не сакаат subcontractors/vendors: {len(blocked)}")
    lines.append(f"Без совпаѓања денес: {len(no_matches)}")
    lines.append(f"Грешки при отворање: {len(errors)}")
    lines.append("")
    lines.append("=" * 70)
    lines.append("LEADS — активно вработуваат, контактирај директно за subcontracting")
    lines.append("=" * 70)

    if not leads:
        lines.append("(нема нови leads денес)")
    for r, hits in leads:
        lines.append("")
        lines.append(f"• {r.name}")
        lines.append(f"  {r.url}")
        seen_kw = set()
        for kw, context, _ in hits:
            if kw in seen_kw:
                continue
            seen_kw.add(kw)
            lines.append(f"  - [{kw}] ...{context}...")

    if blocked:
        lines.append("")
        lines.append("=" * 70)
        lines.append("НЕ СЕ ОТВОРЕНИ ЗА VENDORS — за информација, прескокни")
        lines.append("=" * 70)
        for r, hits in blocked:
            lines.append(f"• {r.name} — {r.url}  (причина: '{hits[0][2]}')")

    if errors:
        lines.append("")
        lines.append("=" * 70)
        lines.append("ГРЕШКИ — провери ги рачно овие линкови")
        lines.append("=" * 70)
        for r in errors:
            lines.append(f"• {r.name} — {r.url}  ({r.error_msg})")

    return "\n".join(lines), len(leads)


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

    report_body, lead_count = build_report(results)
    subject = f"Job Intelligence — {lead_count} нови B2B leads денес"
    write_report_file(subject, report_body)

    print("\n\n" + report_body)


if __name__ == "__main__":
    sys.exit(main())
