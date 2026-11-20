"""Live FMCSA SAFER Company Snapshot fetching, parsing and classification.

Uses the public SAFER query endpoint:
  https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY
    &query_type=queryCarrierSnapshot&query_param=MC_MX|USDOT&query_string=<id>

Be polite to SAFER: sequential fetches, a short delay between requests,
timeouts, and exponential-ish backoff on 429/503/5xx before giving up.
"""
import re
import time
import random
import requests
from bs4 import BeautifulSoup

SAFER_URL = "https://safer.fmcsa.dot.gov/query.asp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
DELAY = 0.6          # seconds between SAFER requests (plus jitter)
MAX_RETRIES = 3
BACKOFF = (5, 15, 30)  # seconds per retry on 429/503/5xx or network error

# SAFER cargo names that map to the "fresh" and "reefer" categories.
FRESH_CARGO = {"Fresh Produce"}
REEFER_CARGO = {"Refrigerated Food", "Meat", "Dairy"}
# Non-freight cargo that never counts as a match for dispatching.
NON_FREIGHT_CARGO = {"Passengers"}


def cargo_categories(cargo_list):
    """Normalise a list of SAFER cargo names to ['general'|'fresh'|'reefer']."""
    cats = set()
    for c in cargo_list:
        c = c.strip()
        if not c or c in NON_FREIGHT_CARGO:
            continue
        if c in FRESH_CARGO:
            cats.add("fresh")
        elif c in REEFER_CARGO:
            cats.add("reefer")
        else:
            cats.add("general")
    return sorted(cats)


def _clean(text):
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _field_value(soup, label):
    """Find a <TH> whose text starts with `label` and return the next <TD> text."""
    for th in soup.find_all("th"):
        t = _clean(th.get_text())
        if t.rstrip(":").lower() == label.lower():
            td = th.find_next_sibling("td")
            if td is not None:
                return _clean(td.get_text(" ", strip=True))
    return ""


def parse_snapshot(html, mc_number):
    """Parse a SAFER Company Snapshot page into a record dict.

    Returns a dict with keys: found, inactive, legal_name, dba_name,
    entity_type, usdot_status, usdot_number, authority_status,
    mc_number, address_line1, city, state, phone, power_units,
    cargo_carried (list of names), cargo_categories (list).
    """
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else ""
    rec = {
        "found": True,
        "inactive": False,
        "legal_name": "",
        "dba_name": "",
        "entity_type": "",
        "usdot_status": "",
        "usdot_number": "",
        "authority_status": "",
        "mc_number": str(mc_number),
        "address_line1": "",
        "city": "",
        "state": "",
        "phone": "",
        "power_units": "",
        "cargo_carried": [],
        "cargo_categories": [],
        "raw_title": title,
    }
    if "RECORD NOT FOUND" in title.upper():
        rec["found"] = False
        return rec
    if "RECORD INACTIVE" in title.upper() or "IS INACTIVE IN THE SAFER DATABASE" in html.upper():
        rec["found"] = True
        rec["inactive"] = True
        return rec

    rec["legal_name"] = _field_value(soup, "Legal Name")
    rec["dba_name"] = _field_value(soup, "DBA Name")
    rec["entity_type"] = _field_value(soup, "Entity Type")
    rec["usdot_status"] = _field_value(soup, "USDOT Status")
    rec["usdot_number"] = _field_value(soup, "USDOT Number")
    rec["authority_status"] = _field_value(soup, "Operating Authority Status")
    # Authority value looks like "AUTHORIZED FOR: Motor Carrier of Property ..."
    rec["authority_status"] = re.sub(
        r"\s*For Licensing and Insurance details click here\.?\s*$",
        "", rec["authority_status"])
    mc_field = _field_value(soup, "MC/MX/FF Number(s)")
    m = re.search(r"MC-(\d+)", mc_field)
    if m:
        rec["mc_number"] = m.group(1)
    rec["phone"] = _field_value(soup, "Phone")
    rec["power_units"] = _field_value(soup, "Power Units")

    # Physical address: the TD holds "44 JADWIN RD<br> KINGSTON, OH 45644"
    # — split on the line break so street / city / state stay separate.
    addr = _field_value(soup, "Physical Address")
    addr_lines = []
    for th in soup.find_all("th"):
        if _clean(th.get_text()).rstrip(":").lower() == "physical address":
            td = th.find_next_sibling("td")
            if td is not None:
                addr_lines = [l.strip() for l in
                              td.get_text("\n", strip=True).split("\n") if l.strip()]
            break
    if len(addr_lines) >= 2:
        rec["address_line1"] = addr_lines[0]
        m = re.match(r"^(.*?),\s*([A-Z]{2})\s*\d{5}", addr_lines[1])
        if m:
            rec["city"] = m.group(1).strip()
            rec["state"] = m.group(2).strip()
    else:
        rec["address_line1"] = addr
        m = re.search(r"([A-Za-z .'\-]+?),\s*([A-Z]{2})\s*\d{5}", addr)
        if m:
            rec["city"] = m.group(1).strip().split()[-1]
            rec["state"] = m.group(2).strip()

    # Cargo Carried: only X-marked items inside the Cargo Carried section.
    cargo = []
    anchor = soup.find("a", href=re.compile(r"#Cargo", re.I))
    if anchor is not None:
        section = anchor.find_parent("tr")
        if section is not None:
            # The checklist table follows this row; scan forward until the
            # next section header row (class querylabelbkg with a link).
            node = section
            for _ in range(400):
                node = node.find_next("tr")
                if node is None:
                    break
                # stop at the next top-level section header
                hdr = node.find("td", class_="querylabelbkg")
                if hdr is not None and node.find("a", class_="querylabel"):
                    # only stop if this is a new section, not the cargo one
                    if "Cargo" not in _clean(hdr.get_text()):
                        break
                for td in node.find_all("td", class_="queryfield"):
                    if _clean(td.get_text()) == "X":
                        sib = td.find_next_sibling("td")
                        if sib is not None:
                            name = _clean(sib.get_text(" ", strip=True))
                            if name:
                                cargo.append(name)
            # de-duplicate, keep order
            seen = set()
            cargo = [c for c in cargo if not (c in seen or seen.add(c))]
    rec["cargo_carried"] = cargo
    rec["cargo_categories"] = cargo_categories(cargo)
    return rec


def fetch_snapshot(identifier, param="MC_MX", delay=DELAY):
    """Fetch one SAFER snapshot. Returns (record_dict, error_or_None).

    record_dict has the parse_snapshot shape plus:
      fetch_error (bool), error (str), qualified (bool),
      status (one of Qualified / Not Found / Skip — ... / Error),
      note (str).
    Classification happens in classify(); this function only fetches.
    """
    params = {
        "searchtype": "ANY",
        "query_type": "queryCarrierSnapshot",
        "query_param": param,
        "query_string": str(identifier),
    }
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(SAFER_URL, params=params, headers=HEADERS,
                                timeout=TIMEOUT)
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                last_err = "HTTP %s from SAFER" % resp.status_code
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF[attempt])
                    continue
                return _error_record(identifier, last_err), last_err
            resp.raise_for_status()
            rec = parse_snapshot(resp.text, identifier)
            rec["fetch_error"] = False
            rec["error"] = ""
            if delay:
                time.sleep(delay + random.uniform(0, 0.4))
            return rec, None
        except requests.RequestException as exc:
            last_err = "Fetch error: %s" % exc
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF[attempt])
                continue
            return _error_record(identifier, last_err), last_err
    return _error_record(identifier, last_err or "Fetch error"), last_err


def _error_record(identifier, error):
    return {
        "found": False, "inactive": False, "legal_name": "", "dba_name": "",
        "entity_type": "", "usdot_status": "", "usdot_number": "",
        "authority_status": "", "mc_number": str(identifier),
        "address_line1": "", "city": "", "state": "", "phone": "",
        "power_units": "", "cargo_carried": [], "cargo_categories": [],
        "raw_title": "", "fetch_error": True, "error": error or "Fetch error",
    }


def classify(rec, selected_cargo=("general", "fresh", "reefer")):
    """Classify a parsed SAFER record.

    Returns (status, note, qualified_bool). Statuses:
      Qualified | Not Found | Skip — Not authorized |
      Skip — Record inactive | Skip — Cargo mismatch | Error
    Rule (transparent, unlike the original's undisclosed extra check):
      authorised for-hire carrier whose cargo matches a selected type.
    """
    if rec.get("fetch_error"):
        return "Error", rec.get("error", "Fetch error"), False
    if not rec.get("found"):
        return "Not Found", "No SAFER record for this MC", False
    if rec.get("inactive"):
        return "Skip — Record inactive", "SAFER record is inactive", False
    auth = (rec.get("authority_status") or "").upper()
    if not auth.startswith("AUTHORIZED FOR"):
        return ("Skip — Not authorized",
                rec.get("authority_status") or "Not authorized", False)
    entity = (rec.get("entity_type") or "").upper()
    if "CARRIER" not in entity:
        return ("Skip — Cargo mismatch",
                "Entity is %s, not a carrier" % (rec.get("entity_type") or "—"), False)
    cats = rec.get("cargo_categories") or []
    if not cats:
        return "Skip — Cargo mismatch", "No freight cargo listed", False
    if not set(cats) & set(selected_cargo):
        return ("Skip — Cargo mismatch",
                "Cargo %s does not match selected types"
                % ", ".join(cats), False)
    return "Qualified", "", True


def check_mc(mc_number, selected_cargo=("general", "fresh", "reefer"), delay=DELAY):
    """Fetch + classify one MC. Returns the record dict with status/note/qualified."""
    rec, _err = fetch_snapshot(mc_number, param="MC_MX", delay=delay)
    status, note, qualified = classify(rec, selected_cargo)
    rec["status"] = status
    rec["note"] = note
    rec["qualified"] = qualified
    return rec


def check_usdot(usdot, selected_cargo=("general", "fresh", "reefer"), delay=DELAY):
    """Fetch + classify one USDOT (Motus-style input)."""
    rec, _err = fetch_snapshot(usdot, param="USDOT", delay=delay)
    status, note, qualified = classify(rec, selected_cargo)
    rec["status"] = status
    rec["note"] = note
    rec["qualified"] = qualified
    return rec
