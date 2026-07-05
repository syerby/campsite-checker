#!/usr/bin/env python3
"""
Green River Reservoir campsite availability checker.

2026: Scans all remaining Sat+Sun windows (2-4 nights) for cancellations.
2027: Checks Thu-Mon (4-night) windows as they unlock daily on the 11-month rolling schedule.
Sends an email alert whenever a new opening is found.
"""

import json
import os
import time
from datetime import date, timedelta
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

NOTIFY_EMAIL    = "syerby@gmail.com"
FROM_EMAIL      = "onboarding@resend.dev"
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")

# Season: third weekend in May → second Monday in October
SEASON_START_2026 = date(2026, 5, 16)
SEASON_END_2026   = date(2026, 10, 12)
SEASON_START_2027 = date(2027, 5, 15)
SEASON_END_2027   = date(2027, 10, 11)

STATE_FILE = Path(__file__).parent / "last_alerted.json"

DELAY_BETWEEN_REQUESTS = 2  # seconds — be polite to the server

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


def windows_with_sat_and_sun(start: date, end: date, min_nights: int = 2, max_nights: int = 4):
    """Yield (arrival, nights) for every stay in [start, end] covering both Sat and Sun."""
    d = start
    while d <= end:
        for nights in range(min_nights, max_nights + 1):
            checkout = d + timedelta(days=nights)
            if checkout > end:
                continue
            stay_days = {(d + timedelta(days=i)).weekday() for i in range(nights)}
            if {5, 6}.issubset(stay_days):  # 5=Sat 6=Sun
                yield (d, nights)
        d += timedelta(days=1)


def thu_mon_windows_in_booking_range(today: date, season_start: date, season_end: date):
    """Yield (thursday, 4) for every Thu-Mon window now inside the 11-month booking window."""
    cutoff = add_months(today, 11)
    d = season_start
    while d.weekday() != 3:  # advance to first Thursday
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


def is_available(html: str) -> bool:
    """Return True if the HTML contains at least one available campsite."""
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: <td class="avail"> or class="a" with text "A"
    for td in soup.find_all("td", class_=lambda c: c and ("avail" in c.lower() or c.lower() == "a")):
        if td.get_text(strip=True).upper() == "A":
            return True

    # Strategy 2: any <td> whose text is exactly "A"
    for td in soup.find_all("td"):
        if td.get_text(strip=True).upper() == "A":
            return True

    # Strategy 3: tag with title containing "available"
    for tag in soup.find_all(True, attrs={"title": True}):
        if "available" in tag["title"].lower():
            return True

    # Strategy 4: explicit "no sites" message → definitely nothing open
    text = soup.get_text(" ", strip=True).lower()
    if "no sites" in text or "no campsites" in text or "0 site" in text:
        return False

    # Strategy 5: a Reserve/Book link implies at least one open slot
    for a in soup.find_all("a", href=True):
        if "reserve" in a["href"].lower() or "booking" in a["href"].lower():
            return True

    return False


def check_window(session: requests.Session, arrival: date, nights: int) -> bool:
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
        return is_available(resp.text)
    except requests.RequestException as e:
        print(f"  [warn] {arrival} x{nights}n — {e}")
        return False

# ── State (dedup) ─────────────────────────────────────────────────────────────

def load_state() -> set:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("available", []))
    return set()


def save_state(available: set):
    STATE_FILE.write_text(json.dumps({"available": sorted(available)}, indent=2))


def window_key(arrival: date, nights: int) -> str:
    return f"{arrival.isoformat()}:{nights}"

# ── Email via SendGrid ────────────────────────────────────────────────────────

def send_email(new_windows: list):
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY — skipping email.")
        return

    lines = []
    for arrival, nights in sorted(new_windows):
        checkout = arrival + timedelta(days=nights)
        lines.append(
            f"  {arrival.strftime('%a %b %-d')} to {checkout.strftime('%a %b %-d')} ({nights} nights)"
        )

    body = (
        "New availability at Green River Reservoir!\n\n"
        + "\n".join(lines)
        + f"\n\nBook now:\n{BOOK_URL}\n"
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
            "subject": f"Green River Reservoir: {len(new_windows)} site(s) open!",
            "text": body,
        },
        timeout=15,
    )
    if resp.status_code == 200:
        print(f"Email sent: {len(new_windows)} new opening(s).")
    else:
        print(f"Email failed ({resp.status_code}): {resp.text}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print(f"Checking Green River Reservoir — {today.isoformat()}")

    session  = make_session()
    previous = load_state()
    current  = set()

    # 2026: cancellations — all remaining Sat+Sun windows
    check_start_2026 = max(today, SEASON_START_2026)
    windows_2026 = list(windows_with_sat_and_sun(check_start_2026, SEASON_END_2026))
    print(f"2026: checking {len(windows_2026)} Sat+Sun window(s)...")
    for arrival, nights in windows_2026:
        print(f"  {arrival} x{nights}n ... ", end="", flush=True)
        avail = check_window(session, arrival, nights)
        print("OPEN" if avail else "full")
        if avail:
            current.add(window_key(arrival, nights))
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # 2027: new openings — Thu-Mon windows now inside the 11-month window
    if today >= date(2026, 6, 15):
        windows_2027 = list(thu_mon_windows_in_booking_range(today, SEASON_START_2027, SEASON_END_2027))
        print(f"2027: checking {len(windows_2027)} Thu-Mon window(s)...")
        for arrival, nights in windows_2027:
            print(f"  {arrival} x{nights}n ... ", end="", flush=True)
            avail = check_window(session, arrival, nights)
            print("OPEN" if avail else "full")
            if avail:
                current.add(window_key(arrival, nights))
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # Alert only on genuinely new openings
    new_openings = current - previous
    if new_openings:
        new_windows = []
        for key in new_openings:
            d_str, n_str = key.split(":")
            new_windows.append((date.fromisoformat(d_str), int(n_str)))
        send_email(new_windows)
    else:
        print("No new openings found.")

    save_state(current)
    print("Done.")


if __name__ == "__main__":
    main()
