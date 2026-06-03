#!/usr/bin/env python3
"""
cleanview_scraper.py
--------------------
Pulls live U.S. data-center capacity data from the Cleanview national tracker
(https://cleanview.co/data-centers) and writes a clean, structured dataset
(JSON + CSV) with derived metrics computed.

Designed to run on a schedule (cron / GitHub Actions / Task Scheduler) so a
front-end can read the output and behave like a living dashboard.

USAGE
    pip install requests beautifulsoup4
    python cleanview_scraper.py                 # full pull -> ./data/
    python cleanview_scraper.py --states tx va  # subset of states
    python cleanview_scraper.py --selftest      # validate parser, no network

OUTPUT
    data/datacenters.json   full structured snapshot + derived metrics
    data/states.csv         one row per state (flat, for Excel/BI)

NOTE: Cleanview pages are server-rendered HTML. Parsing keys off the stable
summary sentence ("There are N operating data centers in X with a combined
capacity of Y MW, and M planned projects that would add Z MW") and the
facility anchor blocks. If Cleanview changes its markup, update the regexes
in parse_summary() / parse_facilities() — they are isolated on purpose.
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://cleanview.co/data-centers"
HEADERS = {"User-Agent": "GoreCreek-research/1.0 (capacity monitor; contact: internal)"}
OUTDIR = Path("data")

# state slug -> display name
STATES = {
    "alabama": "Alabama", "alaska": "Alaska", "arizona": "Arizona", "arkansas": "Arkansas",
    "california": "California", "colorado": "Colorado", "connecticut": "Connecticut",
    "delaware": "Delaware", "florida": "Florida", "georgia": "Georgia", "hawaii": "Hawaii",
    "idaho": "Idaho", "illinois": "Illinois", "indiana": "Indiana", "iowa": "Iowa",
    "kansas": "Kansas", "kentucky": "Kentucky", "louisiana": "Louisiana", "maine": "Maine",
    "maryland": "Maryland", "massachusetts": "Massachusetts", "michigan": "Michigan",
    "minnesota": "Minnesota", "mississippi": "Mississippi", "missouri": "Missouri",
    "montana": "Montana", "nebraska": "Nebraska", "nevada": "Nevada",
    "new-hampshire": "New Hampshire", "new-jersey": "New Jersey", "new-mexico": "New Mexico",
    "new-york": "New York", "north-carolina": "North Carolina", "north-dakota": "North Dakota",
    "ohio": "Ohio", "oklahoma": "Oklahoma", "oregon": "Oregon", "pennsylvania": "Pennsylvania",
    "rhode-island": "Rhode Island", "south-carolina": "South Carolina",
    "south-dakota": "South Dakota", "tennessee": "Tennessee", "texas": "Texas", "utah": "Utah",
    "vermont": "Vermont", "virginia": "Virginia", "washington": "Washington",
    "west-virginia": "West Virginia", "wisconsin": "Wisconsin", "wyoming": "Wyoming",
}

# ---------- parsing ----------

SUMMARY_RE = re.compile(
    r"There are\s+([\d,]+)\s+operating data centers.*?"
    r"combined capacity of\s+([\d,]+)\s*MW.*?"
    r"and\s+([\d,]+)\s+planned projects.*?"
    r"add\s+([\d,]+)\s*MW",
    re.IGNORECASE | re.DOTALL,
)

# facility anchor text, e.g.:
# "Henrico Data Center 500 MW Year Operational: 2020 Location: Henrico, Virginia Developer: Meta"
# "Delta Gigasite - Expansion 9,700 MW Expected Year: TBD Location: Millard, Utah Developer: Creekstone Energy"
FACILITY_RE = re.compile(
    r"(?P<name>.+?)\s+(?P<mw>[\d,]+)\s*MW\s*"
    r"(?:Year Operational|Expected Year):\s*(?P<year>[^L]+?)\s*"
    r"Location:\s*(?P<loc>.+?)\s*"
    r"Developer:\s*(?P<dev>.+?)\s*$",
    re.IGNORECASE,
)


def _to_int(s: str) -> int:
    return int(str(s).replace(",", "").strip())


def strip_html(html: str) -> str:
    """Plain text fallback if BeautifulSoup is unavailable."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def parse_summary(text: str):
    """Return dict of the four headline integers, or None if not found."""
    text = re.sub(r"[*]+", " ", text)          # drop markdown bold if present
    text = re.sub(r"\s+", " ", text)            # collapse whitespace/newlines
    m = SUMMARY_RE.search(text)
    if not m:
        return None
    op_n, op_mw, pl_n, pl_mw = (_to_int(g) for g in m.groups())
    return {
        "operating_count": op_n,
        "operating_mw": op_mw,
        "planned_count": pl_n,
        "planned_mw": pl_mw,
    }


def parse_facilities(anchor_texts):
    """Parse a list of facility anchor strings into structured rows."""
    out = []
    for t in anchor_texts:
        t = re.sub(r"\s+", " ", t).strip()
        m = FACILITY_RE.match(t)
        if not m:
            continue
        d = m.groupdict()
        out.append({
            "name": d["name"].strip(),
            "mw": _to_int(d["mw"]),
            "year": d["year"].strip(),
            "location": d["loc"].strip(),
            "developer": d["dev"].strip(),
        })
    return out


# ---------- derived metrics ----------

def derive(summary: dict) -> dict:
    """Compute the ratios the dashboard cares about. Every metric is a labelled
    calculation with its inputs preserved (number-provenance rule)."""
    op_mw = summary["operating_mw"]
    pl_mw = summary["planned_mw"]
    op_n = summary["operating_count"]
    pl_n = summary["planned_count"]
    d = {}
    d["pipeline_multiple"] = round(pl_mw / op_mw, 2) if op_mw else None        # planned/operating MW
    d["operating_avg_mw"] = round(op_mw / op_n, 1) if op_n else None           # MW per operating site
    d["planned_avg_mw"] = round(pl_mw / pl_n, 1) if pl_n else None             # MW per planned site
    if d["operating_avg_mw"] and d["planned_avg_mw"]:
        d["size_divergence"] = round(d["planned_avg_mw"] / d["operating_avg_mw"], 1)
    d["total_count"] = op_n + pl_n
    d["total_mw"] = op_mw + pl_mw
    return d


# ---------- fetch ----------

def fetch(url: str) -> str:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def get_anchor_texts(html: str):
    """Extract facility anchor text blocks under the two 'Largest ...' sections."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        texts = []
        for a in soup.find_all("a"):
            txt = a.get_text(" ", strip=True)
            if " MW " in f" {txt} " and ("Developer:" in txt):
                texts.append(txt)
        return texts
    except ImportError:
        # crude fallback: split stripped text on " MW " boundaries is unreliable; skip facilities
        return []


def scrape_page(slug: str | None):
    url = f"{BASE}/{slug}" if slug else f"{BASE}/us"
    html = fetch(url)
    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    except ImportError:
        text = strip_html(html)
    summary = parse_summary(text)
    facilities = parse_facilities(get_anchor_texts(html))
    return {"url": url, "summary": summary, "facilities": facilities}


# ---------- orchestration ----------

def run(state_slugs):
    OUTDIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    snapshot = {"pulled_at": ts, "source": BASE, "national": None, "states": {}, "errors": {}}

    # national
    try:
        nat = scrape_page(None)
        if nat["summary"]:
            nat["derived"] = derive(nat["summary"])
        snapshot["national"] = nat
        print(f"[ok] national: {nat['summary']}")
    except Exception as e:  # noqa
        snapshot["errors"]["national"] = str(e)
        print(f"[ERR] national: {e}", file=sys.stderr)

    # states
    for slug in state_slugs:
        try:
            page = scrape_page(slug)
            if page["summary"]:
                page["derived"] = derive(page["summary"])
            snapshot["states"][slug] = page
            s = page["summary"]
            print(f"[ok] {slug}: {s['operating_mw']} op / {s['planned_mw']} pl MW")
            time.sleep(1.0)  # be polite
        except Exception as e:  # noqa
            snapshot["errors"][slug] = str(e)
            print(f"[ERR] {slug}: {e}", file=sys.stderr)

    # write JSON
    (OUTDIR / "datacenters.json").write_text(json.dumps(snapshot, indent=2))

    # write flat states CSV
    with (OUTDIR / "states.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["state", "operating_count", "operating_mw", "planned_count",
                    "planned_mw", "pipeline_multiple", "operating_avg_mw", "planned_avg_mw"])
        for slug, page in snapshot["states"].items():
            s = page.get("summary") or {}
            d = page.get("derived") or {}
            if not s:
                continue
            w.writerow([STATES.get(slug, slug), s["operating_count"], s["operating_mw"],
                        s["planned_count"], s["planned_mw"], d.get("pipeline_multiple"),
                        d.get("operating_avg_mw"), d.get("planned_avg_mw")])

    print(f"\nWrote {OUTDIR/'datacenters.json'} and {OUTDIR/'states.csv'}")
    return snapshot


# ---------- self-test (no network) ----------

SAMPLE = """
There are **195 operating data centers** in Virginia with a combined capacity of
3,311 MW, and **213 planned projects** that would add 36,484 MW of additional capacity.
Henrico Data Center 500 MW Year Operational: 2020 Location: Henrico, Virginia Developer: Meta
Delta Gigasite - Expansion 9,700 MW Expected Year: TBD Location: Millard, Utah Developer: Creekstone Energy
"""


def selftest():
    ok = True
    s = parse_summary(SAMPLE)
    print("summary ->", s)
    assert s == {"operating_count": 195, "operating_mw": 3311,
                 "planned_count": 213, "planned_mw": 36484}, "summary parse failed"
    d = derive(s)
    print("derived ->", d)
    assert d["pipeline_multiple"] == round(36484 / 3311, 2)
    assert d["operating_avg_mw"] == round(3311 / 195, 1)
    fac = parse_facilities([
        "Henrico Data Center 500 MW Year Operational: 2020 Location: Henrico, Virginia Developer: Meta",
        "Delta Gigasite - Expansion 9,700 MW Expected Year: TBD Location: Millard, Utah Developer: Creekstone Energy",
    ])
    print("facilities ->", json.dumps(fac, indent=2))
    assert fac[0]["name"] == "Henrico Data Center" and fac[0]["mw"] == 500
    assert fac[1]["mw"] == 9700 and fac[1]["developer"] == "Creekstone Energy"
    print("\nSELF-TEST PASSED" if ok else "SELF-TEST FAILED")


# ---------- cli ----------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape Cleanview data-center tracker.")
    ap.add_argument("--states", nargs="*", help="state slugs (e.g. texas virginia). default: all")
    ap.add_argument("--selftest", action="store_true", help="validate parser offline")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        sys.exit(0)

    slugs = list(STATES.keys())
    if args.states:
        want = {x.lower().replace(" ", "-") for x in args.states}
        slugs = [s for s in STATES if s in want]
    run(slugs)
