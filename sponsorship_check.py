#!/usr/bin/env python3
"""
sponsorship_check.py — Fills the "Sponsorship" column (Yes / No) on the 3 sender
tabs plus "blank email", by looking each row's company up in its own country's
permit register:

  UK rows      → "Permits UK"  (register of licensed sponsors)
  Ireland rows → "Permits IRL" (employment permits issued)

Names are compared exactly after normalisation (lowercase, punctuation dropped,
trailing legal suffixes like Ltd / Limited / UC / PLC and country qualifiers
like UK / Ireland stripped), so "TWI" matches "TWI Ltd" but never a
substring like "Twin Engines Ltd". Groups registered under a subsidiary's
name (e.g. EOS → "EOS Electro Optical Systems Limited") are listed in OVERRIDES.

Usage:
  python sponsorship_check.py            # write Yes/No to the sheet
  python sponsorship_check.py --dry-run  # print matches, write nothing
"""

import re
import sys
from collections import Counter

sys.path.insert(0, ".")

import gspread
from gspread.utils import rowcol_to_a1

from config.settings import SENDERS, SPREADSHEET_ID, DEFAULT_COUNTRY
from agents.sheet_agent import _load_creds

# ── Constants ─────────────────────────────────────────────────────────────────

TARGET_TABS  = [s["sheet_name"] for s in SENDERS] + ["blank email"]
REGISTERS    = {"UK": "Permits UK", "Ireland": "Permits IRL"}
COMPANY_COL  = "Company name"
REGISTER_COL = "Company Name"
COUNTRY_COL  = "Country"
SPONSOR_COL  = "Sponsorship"

# Stripped repeatedly from the end of a name, so "Google Ireland Limited" → "google"
_TRAILING_TOKENS = {
    "ltd", "limited", "plc", "llp", "lp", "uc", "dac", "clg", "ulc", "inc",
    "incorporated", "llc", "corp", "corporation", "gmbh", "ag", "sa", "sas",
    "bv", "nv", "spa", "srl", "ab", "as", "oy", "unlimited",
    "uk", "gb", "ireland", "irl",
}

# Groups whose sponsor licence / permits sit under a subsidiary's legal name,
# so the exact match can't find them. Keyed by normalised outreach name; the
# value is the register entry that justifies the Yes (checked at load time).
OVERRIDES = {
    "UK": {
        "eos":            "EOS Electro Optical Systems Limited",
        "gkn aerospace":  "GKN Aerospace Services",
        "3d systems":     "3D Systems Europe Ltd",
        "stratasys":      "Stratasys Solutions Limited",
        "carbolite":      "Carbolite Gero Limited",
        "fives":          "Fives Landis Limited",
        "novanta":        "NOVANTA TECHNOLOGIES UK LIMITED",
        "shapemaster":    "Shapemaster Global Limited",
        "micronics":      "Micronics Filtration Ltd",
        "eagleburgmann":  "EagleBurgmann Industries UK LP",
        "becker":         "Becker Industrial Coatings Ltd",
        "shimadzu":       "Shimadzu Research Laboratory (Europe) Ltd",
        "cpi":            "CPI TMD Technologies Ltd",
    },
    "Ireland": {
        "pfizer":             "Pfizer Ireland Pharmaceuticals Unlimited Company",
        "allergan":           "Allergan Pharmaceuticals Ireland Unlimited Company",
        "medtronic":          "Medtronic Vascular Galway Unlimited Company",
        "abbvie":             "AbbVie Ireland NL B.V.",
        "msd":                "MSD International GmbH",
        "grifols":            "Grifols Worldwide Operations Limited",
        "teleflex":           "Teleflex Medical Europe Limited",
        "trane technologies": "Trane Technologies International Limited",
        "trinity biotech":    "Trinity Biotech Manufacturing Limited",
        "vantive":            "Vantive Manufacturing Limited",
        "orona":              "Orona Midwestern Lifts Ltd",
        "zoetis":             "Zoetis Belgium S.A.",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalise(name: str, drop_brackets: bool = False) -> str:
    """drop_brackets is for outreach names, where brackets hold asides like
    "(DMC)". Register names keep bracket text — "SMC (RHIWBINA) LTD" is a
    different company from "SMC" — though "(UK)" still strips as a suffix."""
    s = name.lower().replace("&", " and ").replace("+", " and ")
    if drop_brackets:
        s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)        # "u.k." → "u k" handled below
    s = re.sub(r"\bu k\b", "uk", s)
    s = re.sub(r"\bunlimited company\s*$", "unlimited", s)   # Irish legal form
    tokens = s.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _TRAILING_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def load_register(ss, tab: str) -> dict:
    """normalised name → first original register name that produced it."""
    ws = ss.worksheet(tab)
    values = ws.col_values(ws.row_values(1).index(REGISTER_COL) + 1)[1:]
    index = {}
    for raw in values:
        if not raw.strip() or raw.strip().lower() == "total":
            continue
        index.setdefault(normalise(raw), raw.strip())
    return index


def apply_overrides(registers: dict):
    """Fold OVERRIDES into the register lookups, skipping any whose backing
    register entry has disappeared (e.g. licence revoked on a later refresh)."""
    for country, entries in OVERRIDES.items():
        for outreach_name, register_name in entries.items():
            if normalise(register_name) in registers[country]:
                registers[country].setdefault(outreach_name, register_name)
            else:
                print(f"  Override skipped — {register_name!r} no longer in {REGISTERS[country]}")


def _country_key(raw: str) -> str:
    c = raw.strip().lower()
    if c in ("ireland", "ie", "irl", "republic of ireland"):
        return "Ireland"
    return DEFAULT_COUNTRY


# ── Main processor ────────────────────────────────────────────────────────────

def process_tab(ss, tab: str, registers: dict, dry_run: bool):
    print(f"\n  '{tab}'…")
    ws = ss.worksheet(tab)
    all_values = ws.get_all_values()
    headers = all_values[0]
    company_idx = headers.index(COMPANY_COL)
    country_idx = headers.index(COUNTRY_COL)
    sponsor_idx = headers.index(SPONSOR_COL)

    companies, results, counts = [], [], Counter()
    for r in all_values[1:]:
        r = r + [""] * (len(headers) - len(r))
        company = r[company_idx].strip()
        companies.append(company)
        if not company:
            results.append([""])
            continue
        country = _country_key(r[country_idx])
        hit = registers[country].get(normalise(company, drop_brackets=True))
        results.append(["Yes" if hit else "No"])
        counts[(country, "Yes" if hit else "No")] += 1
        if hit and dry_run:
            print(f"    Yes  [{country}] {company!r:45} ↔ {hit!r}")

    for (country, verdict), n in sorted(counts.items()):
        print(f"    {country:8} {verdict:3}: {n}")

    if dry_run or not results:
        return

    # sheet_sort may reorder rows while we worked — only write if the company
    # column is still in the order we computed against.
    live = ws.col_values(company_idx + 1)[1:]
    live += [""] * (len(companies) - len(live))
    if [c.strip() for c in live] != companies:
        print("    Rows moved since read (sheet was re-sorted) — skipped, re-run.")
        return

    start = rowcol_to_a1(2, sponsor_idx + 1)
    end   = rowcol_to_a1(len(results) + 1, sponsor_idx + 1)
    ws.update(results, range_name=f"{start}:{end}", value_input_option="RAW")
    print(f"    Wrote {len(results)} rows.")


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"Sponsorship check{' (dry run)' if dry_run else ''}…")

    gc = gspread.authorize(_load_creds(SENDERS[0]["token_file"]))
    gc.set_timeout(60)
    ss = gc.open_by_key(SPREADSHEET_ID)

    registers = {country: load_register(ss, tab) for country, tab in REGISTERS.items()}
    for country, reg in registers.items():
        print(f"  Loaded {len(reg):,} {country} register names")
    apply_overrides(registers)

    for tab in TARGET_TABS:
        process_tab(ss, tab, registers, dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
