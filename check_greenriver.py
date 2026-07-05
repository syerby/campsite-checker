#!/usr/bin/env python3
"""
Green River Reservoir campsite availability checker.

2026: Scans all remaining Sat+Sun windows (2-4 nights) for cancellations.
2027: Checks Thu-Mon (4-night) windows as they unlock daily on the 11-month rolling schedule.
Sends a daily summary email; flags newly opened windows with *** NEW ***.
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
CONTRACT_CODE = "VT"
PARK_ID       = "1280076"
PARK_NAME     = "green-river-reservoir-state-park"
BASE_URL      = "https://vtstateparks-visit.com"
BOOK_URL      = (
    f"{BASE_URL}/camping/{PARK_NAME}/r/campgroundDetails.do"
    f"?contractCode={CONTRACT_CODE}&parkId={PARK_ID}"
)

NOTIFY_EMAIL   = "syerby@gmail.com"
FROM_EMAIL     = "onboarding@resend.dev"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

SEASON_START_2026 = date(2026, 5, 16)
SEASON_END_2026   = date(2026, 8, 31)
SEASON_START_2027 = date(2027, 5, 15)
SEASON_END_2027   = date(2027, 10, 11)

STATE_FILE = Path(__file__).parent / "last_alerted.json"

DELAY_BETWEEN_REQUESTS = 2

# ── Date helpers ──────────────────────────────────────────────────────────────

def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    def days_in_month(y, m):
        return [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    day = min(d.day, days_in_month(year, month))
    return date(year, month, day)


def generate_2026_windows(start: date, end: date):
    """Yield (arrival, nights) for:
    - Any 2-4 night stay that covers both Sat and Sun
    - Any 2-night Thu+Fri stay
    """
    d = start
    while d <= end:
        for nights in range(2, 5):
            checkout = d + timedelta(days=nights)
            if checkout > end:
                continue
            stay_days = {(d + timedelta(days=i)).weekday() for i in range(nights)}
            sat_sun  = {5, 6}.issubset(stay_days)
            thu_fri  = stay_days == {3, 4}  # exactly Thu+Fri, 2 nights
            if sat_sun or thu_fri:
                yield (d, nights)
        d += timedelta(days=1)


def thu_mon_windows_in_booking_range(today: date, season_start: date, season_end: date):
    cutoff = add_months(today, 11)
    d = season_start
    while d.weekday() != 3:
        d += timedelta(days=1)
    while d <= season_end and d <= cutoff:
        if d + timedelta(days=4) <= season_end:
            yield (d, 4)
        d += timedelta(days=7)

# ── Availability check ────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": BASE_URL,
    })
    try:
        session.get(BOOK_URL, timeout=15)
    except requests.RequestException:
        pass
    return session


def get_available_sites(html: str, arrival: date, nights: int) -> list:
    """
    Parse the ReserveAmerica calendar HTML.

    The calendar shows a 2-week grid: each row is a site, each cell is one
    night. Available nights render as a link: <a href="...arvdate=M/D/YYYY...">A</a>.
    We extract the actual date from the link href, so we only count a site as
    available when ALL nights of the requested stay have a linked 'A'.
    """
    soup = BeautifulSoup(html, "html.parser")
    requested_dates = {arrival + timedelta(days=i) for i in range(nights)}
    sites = []

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        site_name = cells[0].get_text(strip=True).strip("[]")
        if not site_name or site_name.upper() in ("A", "R", "X", "SITE", "LOOP", ""):
            continue

        # Collect dates that have a linked "A" in this row
        available_dates = set()
        for cell in cells[1:]:
            link = cell.find("a")
            if not link or link.get_text(strip=True).upper() != "A":
                continue
            href = link.get("href", "")
            m = re.search(r"arvdate=(\d+/\d+/\d{4})", href)
            if m:
                try:
                    cell_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
                    available_dates.add(cell_date)
                except ValueError:
                    pass

        if requested_dates and requested_dates.issubset(available_dates):
            sites.append(site_name)

    return sites


def check_window(session: requests.Session, arrival: date, nights: int) -> list:
    params = {
        "contractCode": CONTRACT_CODE,
        "parkId": PARK_ID,
        "arvdate": arrival.strftime("%m/%d/%Y"),
        "lengthOfStay": nights,
        "startIdx": 0,
    }
    try:
        resp = session.get(f"{BASE_URL}/campsiteCalendar.do", params=params, timeout=15)
        resp.raise_for_status()
        return get_available_sites(resp.text, arrival, nights)
    except requests.RequestException as e:
        print(f"  [warn] {arrival} x{nights}n — {e}")
        return []

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        available = data.get("available", {})
        if isinstance(available, list):
            return {k: [] for k in available}
        return available
    return {}


def save_state(available: dict):
    STATE_FILE.write_text(json.dumps({"available": available}, indent=2))


def window_key(arrival: date, nights: int) -> str:
    return f"{arrival.isoformat()}:{nights}"

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(current: dict, previous: dict):
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY set — skipping email.")
        return

    new_keys = set(current) - set(previous)

    if current:
        lines = []
        for key in sorted(current):
            d_str, n_str  = key.split(":")
            arrival       = date.fromisoformat(d_str)
            nights        = int(n_str)
            checkout      = arrival + timedelta(days=nights)
            site_names    = current[key]
            sites_str     = ", ".join(site_names) if site_names else "see website"
            flag          = "  *** NEW ***" if key in new_keys else ""
            lines.append(
                f"  {arrival.strftime('%a %b %-d')} to {checkout.strftime('%a %b %-d')}"
                f" ({nights} nights) — {sites_str}{flag}"
            )
        subject = f"Green River Reservoir: {len(current)} window(s) open!"
        body    = (
            "Available windows at Green River Reservoir:\n\n"
            + "\n".join(lines)
            + f"\n\nBook now:\n{BOOK_URL}\n"
        )
    else:
        subject = "Green River Reservoir: nothing available today"
        body    = (
            "No campsites available at Green River Reservoir today.\n\n"
            "This check runs daily at 9 AM EDT — you will hear from us tomorrow.\n"
        )

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [NOTIFY_EMAIL],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if resp.status_code == 200:
        print("Email sent.")
    else:
        print(f"Email failed ({resp.status_code}): {resp.text}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print(f"=== Green River Reservoir check — {today.isoformat()} ===")

    session  = make_session()
    previous = load_state()
    current  = {}

    check_start_2026 = max(today, SEASON_START_2026)
    windows_2026 = list(generate_2026_windows(check_start_2026, SEASON_END_2026))
    print(f"2026: {len(windows_2026)} Sat+Sun window(s) to check")
    for arrival, nights in windows_2026:
        print(f"  {arrival} x{nights}n ... ", end="", flush=True)
        sites = check_window(session, arrival, nights)
        print(", ".join(sites) if sites else "full")
        if sites:
            current[window_key(arrival, nights)] = sites
        time.sleep(DELAY_BETWEEN_REQUESTS)

    if today >= date(2026, 6, 15):
        windows_2027 = list(thu_mon_windows_in_booking_range(today, SEASON_START_2027, SEASON_END_2027))
        print(f"2027: {len(windows_2027)} Thu-Mon window(s) to check")
        for arrival, nights in windows_2027:
            print(f"  {arrival} x{nights}n ... ", end="", flush=True)
            sites = check_window(session, arrival, nights)
            print(", ".join(sites) if sites else "full")
            if sites:
                current[window_key(arrival, nights)] = sites
            time.sleep(DELAY_BETWEEN_REQUESTS)

    send_email(current, previous)
    save_state(current)
    print("Done.")


if __name__ == "__main__":
    main()
