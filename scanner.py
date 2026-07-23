"""
Job Intelligence Scanner for VFF Utility Engineering
-----------------------------------------------------
Секое утро (преку GitHub Actions) го прегледува career страниците на
компаниите од companies.csv, бара клучни зборови релевантни за
Make Ready / Pole Loading работа, ги отфрла огласите каде експлицитно
пишува дека мора да се биде во САД / со work authorization, и праќа
email со останатите (потенцијално remote/B2B) огласи.

Не гарантира 100% точност — ова е помошна алатка за филтрирање, не замена
за читање на самиот оглас пред аплицирање.
"""

import csv
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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

# Фрази што, ако се појават на страницата, значат дека огласот
# веројатно бара физичко присуство во САД / US work authorization.
EXCLUSION_PHRASES = [
    "us only",
    "u.s. only",
    "us-based only",
    "must reside in the us",
    "must reside in the united states",
    "must be located in the us",
    "must be located in the united states",
    "must be a us citizen",
    "must be a u.s. citizen",
    "us citizenship required",
    "work authorization required",
    "authorization to work in the united states",
    "authorized to work in the united states",
    "no sponsorship",
    "not provide sponsorship",
    "will not sponsor",
    "unable to sponsor",
    "no visa sponsorship",
    "green card holder",
    "must be legally authorized to work in the us",
    "candidates must reside",
    "local candidates only",
    "onsite only",
    "on-site only",
    "relocation required",
]

CONTEXT_CHARS = 160  # колку карактери контекст да се земат околу секое совпаѓање
PAGE_TIMEOUT_MS = 25000
NAV_WAIT_MS = 3000  # дополнително чекање по вчитување, за JS-рендерирани страници


@dataclass
class CompanyResult:
    name: str
    url: str
    status: str = "ok"  # ok | error | no_matches
    hits: list = field(default_factory=list)  # (keyword, context, excluded_by)
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

            excluded_by = None
            for phrase in EXCLUSION_PHRASES:
                if phrase in context:
                    excluded_by = phrase
                    break

            hits.append((kw, context, excluded_by))
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
    except Exception as e:  # намерно широко — не сакаме еден пад да го собори целиот скен
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
    relevant = []   # има keyword hits, БЕЗ exclusion
    excluded = []    # има keyword hits, НО со US-only/work-auth фраза
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
            relevant.append((r, clean_hits))
        if blocked_hits and not clean_hits:
            excluded.append((r, blocked_hits))

    lines = []
    lines.append(f"JOB INTELLIGENCE — дневен извештај")
    lines.append(f"Проверени компании: {len(results)}")
    lines.append(f"Потенцијално релевантни (без US-only ограничувања): {len(relevant)}")
    lines.append(f"Најдени клучни зборови, но изгледа US-only: {len(excluded)}")
    lines.append(f"Без совпаѓања: {len(no_matches)}")
    lines.append(f"Грешки при отворање: {len(errors)}")
    lines.append("")
    lines.append("=" * 70)
    lines.append("ПОТЕНЦИЈАЛНО РЕЛЕВАНТНИ")
    lines.append("=" * 70)

    if not relevant:
        lines.append("(нема нови совпаѓања денес)")
    for r, hits in relevant:
        lines.append("")
        lines.append(f"• {r.name}")
        lines.append(f"  {r.url}")
        seen_kw = set()
        for kw, context, _ in hits:
            if kw in seen_kw:
                continue
            seen_kw.add(kw)
            lines.append(f"  - [{kw}] ...{context}...")

    if excluded:
        lines.append("")
        lines.append("=" * 70)
        lines.append("ОТФРЛЕНИ (US-only / work authorization) — за информација")
        lines.append("=" * 70)
        for r, hits in excluded:
            lines.append(f"• {r.name} — {r.url}  (причина: '{hits[0][2]}')")

    if errors:
        lines.append("")
        lines.append("=" * 70)
        lines.append("ГРЕШКИ — провери ги рачно овие линкови")
        lines.append("=" * 70)
        for r in errors:
            lines.append(f"• {r.name} — {r.url}  ({r.error_msg})")

    return "\n".join(lines), len(relevant)


def send_email(subject: str, body: str):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        print("GMAIL_USER / GMAIL_APP_PASSWORD не се поставени — прескокнувам email, само печатам извештај.")
        print(body)
        return

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipient, msg.as_string())
    print(f"Email испратен до {recipient}")


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
            time.sleep(0.5)  # мала пауза, да не се преоптоварат серверите
        browser.close()

    report_body, relevant_count = build_report(results)
    subject = f"Job Intelligence — {relevant_count} релевантни огласи денес"
    send_email(subject, report_body)

    # исто и во GitHub Actions log, за увид без email
    print("\n\n" + report_body)


if __name__ == "__main__":
    sys.exit(main())
