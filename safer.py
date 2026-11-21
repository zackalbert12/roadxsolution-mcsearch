"""Live FMCSA SAFER Company Snapshot fetching, parsing and classification.

Uses the public SAFER query endpoint:
  https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY
    &query_type=queryCarrierSnapshot&query_param=MC_MX|USDOT&query_string=<id>

Be polite to SAFER: sequential fetches per worker, a short delay between
requests, timeouts, and exponential-ish backoff on 429/503/5xx before
giving up. Bulk scans go through rotating proxies (like the original
site) so one IP never hammers SAFER; single searches go DIRECT FIRST
(a working direct call takes ~4 s, blind proxy rotation was costing
~30 s) and only fall back to proxies when direct fails. The proxy
pool itself is health-checked once per cache TTL — dead/slow
proxies never reach the rotation. There is no free proxy list:
ROAD_PROXIES pins proxies explicitly, otherwise fetches go direct.
"""
import concurrent.futures
import os
import re
import threading
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
DELAY = 0.6          # seconds between SAFER requests per worker (plus jitter)
MAX_RETRIES = 3
BACKOFF = (5, 15, 30)  # seconds per retry on 429/503/5xx or network error

# --- hard fetch deadlines ------------------------------------------------
# Render logs (2026-11-21) proved the `requests` timeout does NOT bound
# the TCP connect / TLS handshake phase when going through a proxy: the
# single gunicorn worker hung there until Gunicorn killed it after ~30 s
# (WORKER TIMEOUT -> bare 500, 7 times in 2 hours). So every outbound
# call ALSO runs in a daemon thread with a hard wall-clock deadline
# that cannot be bypassed: on expiry the hung thread is abandoned and
# the attempt fails fast instead of stalling the request.
# Per-attempt deadline + whole-call budget for SAFER snapshot/name
# fetches: a single lookup can then take at most ~20 s, and an auto-mode
# search (USDOT + MC) at most ~40 s — both well under the 120 s
# gunicorn timeout. Census (owner name) is a single call, capped at 10 s.
SAFER_CONNECT_TIMEOUT = 5    # requests (connect, read) tuple, first defence
SAFER_READ_TIMEOUT = 20
SAFER_ATTEMPT_DEADLINE = 10  # hard seconds per attempt
SAFER_TOTAL_BUDGET = 20      # hard seconds for one fetch_snapshot/search call
CENSUS_CONNECT_TIMEOUT = 5
CENSUS_READ_TIMEOUT = 10
CENSUS_HARD_DEADLINE = 10    # hard seconds for one officer lookup


def _request_with_deadline(fn, deadline):
    """Run fn() (a requests.get call) in a daemon thread and wait at most
    `deadline` seconds. Returns (result, error, timed_out).

    The thread is abandoned on timeout (daemon, so it can never block
    process exit); the socket op inside it ends on its own when the OS
    gives up. `error` is the exception fn() raised, or None.
    """
    box = {}

    def _run():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - returned via box
            box["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=deadline)
    if t.is_alive():
        return None, None, True
    if "error" in box:
        return None, box["error"], False
    if "result" not in box:
        # Thread ended without a result (e.g. killed by SystemExit) —
        # treat it as a failure, never as a success.
        return None, RuntimeError("request thread ended without a result"), False
    return box["result"], None, False

# --- proxies -------------------------------------------------------------
# No free proxy list: the unauthenticated ProxyScrape feed was a trust
# exposure and the pool was empty in practice anyway, so scans already
# go direct — the fetch only cost ~0.6 s per load to re-validate.
# ROAD_PROXIES (comma-separated http://host:port) still pins proxies
# explicitly; otherwise every fetch is direct.
PROXY_CACHE_TTL = 600  # seconds
_PROXY_POOL = {"list": [], "fetched_at": 0.0, "idx": 0,
               "lock": threading.Lock()}

# --- proxy health-check ---------------------------------------------------
# Env-provided proxies (ROAD_PROXIES) are probed once (in parallel,
# hard-budgeted) and only proxies that answer fast are kept, fastest
# first — a dead paid proxy must not stall a search. Validation runs
# at most once per PROXY_CACHE_TTL, and a direct-first search never
# pays for it on the happy path (the pool is only loaded after direct
# fails).
PROXY_PROBE_URL = "https://example.com"  # tiny, reliable HTTPS endpoint
PROXY_PROBE_TIMEOUT = (2, 3)   # (connect, read) per probe
PROXY_PROBE_DEADLINE = 3       # hard seconds per probe
PROXY_PROBE_WORKERS = 10
PROXY_VALIDATE_MAX = 30        # max proxies probed per refresh
PROXY_GOOD_TARGET = 10         # stop once this many good proxies found
PROXY_VALIDATION_BUDGET = 10   # hard seconds for the whole validation


def _probe_proxy(proxy):
    """One probe: GET a tiny HTTPS page through `proxy`. Returns
    (proxy, latency_seconds) when the proxy answers, else None.
    Any HTTP response counts — even a 4xx proves the proxy works."""
    t0 = time.monotonic()

    def _get():
        return requests.get(PROXY_PROBE_URL, headers=HEADERS,
                            timeout=PROXY_PROBE_TIMEOUT,
                            proxies={"http": proxy, "https": proxy})

    resp, _err, timed_out = _request_with_deadline(_get,
                                                   deadline=PROXY_PROBE_DEADLINE)
    if timed_out or resp is None:
        return None
    return proxy, time.monotonic() - t0


def _validate_proxies(proxies):
    """Probe a raw proxy list in parallel; keep only proxies that
    answer within the probe deadline, fastest first. Bounded by
    PROXY_VALIDATION_BUDGET in total: stops early once enough good
    proxies are found, and never waits for stragglers. Returns []
    when none answer (callers then fetch direct)."""
    candidates = list(dict.fromkeys(proxies))  # de-dupe, keep order
    random.shuffle(candidates)
    candidates = candidates[:PROXY_VALIDATE_MAX]
    if not candidates:
        return []
    good = []
    started = time.time()
    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=PROXY_PROBE_WORKERS)
    try:
        futs = {ex.submit(_probe_proxy, p): p for p in candidates}
        try:
            for fut in concurrent.futures.as_completed(
                    futs, timeout=PROXY_VALIDATION_BUDGET):
                res = fut.result()
                if res is not None:
                    good.append(res)
                if len(good) >= PROXY_GOOD_TARGET:
                    break
                if time.time() - started >= PROXY_VALIDATION_BUDGET:
                    break
        except TimeoutError:
            pass
    finally:
        # Do not wait for stragglers: each probe is already hard-bounded
        # at PROXY_PROBE_DEADLINE, so running ones finish within ~3 s.
        ex.shutdown(wait=False, cancel_futures=True)
    good.sort(key=lambda item: item[1])
    return [p for p, _lat in good]


def _load_proxies():
    """Proxy list: ROAD_PROXIES env (comma-separated http://host:port)
    only — there is no free-list fallback. The fresh list is
    health-checked once per PROXY_CACHE_TTL (see _validate_proxies)
    and cached — callers only ever see proxies that answered fast.
    Returns [] when proxies are disabled (ROAD_NO_PROXY=1) or none
    could be loaded/validated — callers then fetch direct."""
    if os.environ.get("ROAD_NO_PROXY") == "1":
        return []
    now = time.time()
    with _PROXY_POOL["lock"]:
        if _PROXY_POOL["list"] and now - _PROXY_POOL["fetched_at"] < PROXY_CACHE_TTL:
            return list(_PROXY_POOL["list"])
    raw = []
    env = os.environ.get("ROAD_PROXIES", "").strip()
    if env:
        raw = [p.strip() for p in env.split(",") if p.strip()]
    # Health-check the fresh list (bounded, ~10 s max): dead/slow
    # proxies never reach the rotation. An empty result just means
    # "fetch direct" — callers already handle that.
    good = _validate_proxies(raw) if raw else []
    with _PROXY_POOL["lock"]:
        _PROXY_POOL["list"] = good
        _PROXY_POOL["fetched_at"] = now
    return list(good)


def _next_proxy(proxies):
    """Round-robin pick from the pool (thread-safe); None = direct."""
    if not proxies:
        return None
    with _PROXY_POOL["lock"]:
        proxy = proxies[_PROXY_POOL["idx"] % len(proxies)]
        _PROXY_POOL["idx"] += 1
    return proxy

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


def fetch_snapshot(identifier, param="MC_MX", delay=DELAY,
                   direct_first=False):
    """Fetch one SAFER snapshot. Returns (record_dict, error_or_None).

    record_dict has the parse_snapshot shape plus:
      fetch_error (bool), error (str), qualified (bool),
      status (one of Qualified / Not Found / Skip — ... / Error),
      note (str).
    Classification happens in classify(); this function only fetches.

    direct_first=True (single searches): try the direct connection
    first — a working direct call takes ~4 s, while blind proxy
    rotation was costing ~30 s — and only fall back to the
    health-checked proxy pool when direct fails. The pool is loaded
    lazily, after direct fails, so the happy path never pays the
    validation cost. direct_first=False (bulk scans): the old order —
    proxies first, direct last — so one IP never hammers SAFER.
    """
    params = {
        "searchtype": "ANY",
        "query_type": "queryCarrierSnapshot",
        "query_param": param,
        "query_string": str(identifier),
    }
    # Attempt plan: direct-first for single searches, proxy rotation
    # for bulk scans (see docstring). In direct-first mode the proxy
    # list stays unloaded until direct actually fails.
    proxies = None if direct_first else _load_proxies()
    attempts = [None] if direct_first else []
    seen = set()
    if not direct_first:
        for _ in range(MAX_RETRIES):
            p = _next_proxy(proxies)
            if p and p not in seen:
                attempts.append(p)
                seen.add(p)
        attempts.append(None)  # direct
    proxies_loaded = not direct_first

    def _maybe_load_proxies():
        """Direct-first only: on the first failed direct attempt, load
        the health-checked proxy pool and append fresh proxies to the
        attempt list. No-op in proxy-first mode (pool already loaded)
        and on later calls (pool already appended)."""
        nonlocal proxies_loaded
        if not direct_first or proxies_loaded:
            return
        proxies_loaded = True
        pool = _load_proxies()
        for _ in range(MAX_RETRIES):
            p = _next_proxy(pool)
            if p and p not in seen:
                attempts.append(p)
                seen.add(p)

    last_err = None
    started = time.time()
    attempt = 0
    while attempt < len(attempts):
        proxy = attempts[attempt]
        attempt += 1
        # Whole-call budget: the sum of hung attempts can never exceed
        # SAFER_TOTAL_BUDGET, so one lookup cannot stall its request.
        remaining = SAFER_TOTAL_BUDGET - (time.time() - started)
        if remaining <= 0:
            break
        req_proxies = {"http": proxy, "https": proxy} if proxy else None
        resp, err, timed_out = _request_with_deadline(
            lambda _rp=req_proxies: requests.get(
                SAFER_URL, params=params, headers=HEADERS,
                timeout=(SAFER_CONNECT_TIMEOUT, SAFER_READ_TIMEOUT),
                proxies=_rp),
            deadline=min(SAFER_ATTEMPT_DEADLINE, remaining))
        if timed_out:
            # The connect/TLS handshake stalled past the hard deadline.
            last_err = "Fetch timed out (connection stalled)"
            if proxy is not None and not direct_first:
                # A hung proxy is never going to answer: skip any
                # remaining proxies and go straight to the direct attempt.
                attempts = attempts[:attempt] + [None]
            # In direct-first mode a stalled attempt just falls through
            # to the next one (proxy fallback, or the error below).
        elif err is not None:
            last_err = "Fetch error: %s" % err
            # Dead proxy / network error: rotate immediately, no sleep —
            # the next proxy is a different IP, so waiting helps nothing.
        elif resp.status_code in (429, 503) or resp.status_code >= 500:
            # SAFER itself is rate-limiting: wait, then retry. Load the
            # proxy pool first when direct just got throttled, so the
            # retry can go through a different IP.
            last_err = "HTTP %s from SAFER" % resp.status_code
            _maybe_load_proxies()
            if attempt < len(attempts):
                wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                remaining = SAFER_TOTAL_BUDGET - (time.time() - started)
                if remaining <= 0:
                    break
                time.sleep(min(wait, remaining))
                continue
            return _error_record(identifier, last_err), last_err
        else:
            try:
                resp.raise_for_status()
            except requests.RequestException as exc:
                last_err = "Fetch error: %s" % exc
            else:
                rec = parse_snapshot(resp.text, identifier)
                rec["fetch_error"] = False
                rec["error"] = ""
                if delay:
                    time.sleep(delay + random.uniform(0, 0.4))
                return rec, None
        # Direct failed in direct-first mode: load the (health-checked)
        # proxy pool now and append fresh proxies to the attempt list.
        _maybe_load_proxies()
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
      Skip — Record inactive | Skip — Cargo mismatch |
      Not a carrier | Error
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
        return ("Not a carrier",
                "Entity is %s, not a carrier" % (rec.get("entity_type") or "—"), False)
    cats = rec.get("cargo_categories") or []
    if not cats:
        return "Skip — Cargo mismatch", "No freight cargo listed", False
    if not set(cats) & set(selected_cargo):
        return ("Skip — Cargo mismatch",
                "Cargo %s does not match selected types"
                % ", ".join(cats), False)
    return "Qualified", "", True


# --- FMCSA MCMIS Company Census (owner / officer name) --------------------
# The SAFER snapshot does not publish an owner name, but the public MCMIS
# Company Census File on data.transportation.gov does: fields
# company_officer_1 / company_officer_2, looked up by USDOT number.
# Used ONLY for the public carrier-search detail page — never during scans
# (one extra request per check would slow scans and hit Socrata limits).
# Socrata throttles anonymous requests (HTTP 429), so any failure just
# means "no owner shown". Set ROAD_SOCRATA_APP_TOKEN (free, from
# data.transportation.gov) to raise the limit.
CENSUS_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
_OFFICER_CACHE = {}


def fetch_company_officer(dot_number):
    """Owner/officer name for a USDOT number from the MCMIS census file.
    Returns the name string, or None when unknown/unavailable."""
    dot = "".join(ch for ch in str(dot_number or "") if ch.isdigit())
    if not dot:
        return None
    if dot in _OFFICER_CACHE:
        return _OFFICER_CACHE[dot]
    name = None
    try:
        headers = {"Accept": "application/json"}
        token = os.environ.get("ROAD_SOCRATA_APP_TOKEN", "").strip()
        if token:
            headers["X-App-Token"] = token
        # Hard deadline: a stalled Census connect/TLS handshake killed
        # the gunicorn worker the same way a stalled SAFER fetch did
        # (Render logs 2026-11-21, 10:26 UTC). Failure just means
        # "no owner shown" — never a hang.
        r, _err, _timed_out = _request_with_deadline(
            lambda: requests.get(CENSUS_URL, params={
                "dot_number": dot,
                "$select": "company_officer_1,company_officer_2",
                "$limit": "1",
            }, headers=headers,
              timeout=(CENSUS_CONNECT_TIMEOUT, CENSUS_READ_TIMEOUT)),
            deadline=CENSUS_HARD_DEADLINE)
        if r is not None and r.status_code == 200:
            rows = r.json()
            if rows:
                row = rows[0]
                name = (row.get("company_officer_1") or "").strip() or \
                       (row.get("company_officer_2") or "").strip() or None
    except Exception:
        name = None
    # L5: bound the cache (max 1,000 DOTs, oldest evicted) so it cannot
    # grow without limit over the life of the process.
    if len(_OFFICER_CACHE) >= 1000:
        _OFFICER_CACHE.pop(next(iter(_OFFICER_CACHE)))
    _OFFICER_CACHE[dot] = name
    return name


def check_mc(mc_number, selected_cargo=("general", "fresh", "reefer"), delay=DELAY,
             direct_first=False):
    """Fetch + classify one MC. Returns the record dict with status/note/qualified.
    direct_first=True: direct connection first (single searches)."""
    rec, _err = fetch_snapshot(mc_number, param="MC_MX", delay=delay,
                               direct_first=direct_first)
    status, note, qualified = classify(rec, selected_cargo)
    rec["status"] = status
    rec["note"] = note
    rec["qualified"] = qualified
    return rec


def check_usdot(usdot, selected_cargo=("general", "fresh", "reefer"), delay=DELAY,
                direct_first=False):
    """Fetch + classify one USDOT (Motus-style input).
    direct_first=True: direct connection first (single searches)."""
    rec, _err = fetch_snapshot(usdot, param="USDOT", delay=delay,
                               direct_first=direct_first)
    status, note, qualified = classify(rec, selected_cargo)
    rec["status"] = status
    rec["note"] = note
    rec["qualified"] = qualified
    return rec


def parse_name_matches(html, limit=50):
    """Parse a SAFER 'Select Company' page into match dicts.

    Each match row links to the carrier snapshot by USDOT:
    query.asp?...&query_param=USDOT&query_string=<usdot>.
    Returns [{"name", "location", "usdot"}] in SAFER's order,
    de-duplicated by USDOT (the same carrier can appear twice,
    once under its legal name and once under a DBA).
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "query_param=USDOT" not in href:
            continue
        m = re.search(r"query_string=(\d+)", href)
        if not m:
            continue
        usdot = m.group(1)
        if usdot in seen:
            continue
        seen.add(usdot)
        name = _clean(a.get_text(" ", strip=True))
        location = ""
        tr = a.find_parent("tr")
        if tr is not None:
            td = tr.find("td")
            if td is not None:
                location = _clean(td.get_text(" ", strip=True))
        matches.append({"name": name, "location": location, "usdot": usdot})
        if len(matches) >= limit:
            break
    return matches


def search_by_name(name, limit=50, direct_first=False):
    """Search SAFER by company name (keyword search).

    SAFER redirects NAME searches to keywordx.asp, which lists
    'Possible Keyword Matches' — name + location per carrier, each
    linking to its snapshot by USDOT. (LEGAL_NAME is NOT a valid
    query_param — SAFER treats it as an MC/MX number search and
    returns 'RECORD NOT FOUND'.)

    Returns (matches, error_or_None). An empty match list with no
    error means SAFER found nobody under that name.

    direct_first=True (single searches): direct first, proxy pool
    only on direct failure — same as fetch_snapshot. False (bulk):
    proxies first, direct last.
    """
    params = {
        "searchtype": "ANY",
        "query_type": "queryCarrierSnapshot",
        "query_param": "NAME",
        "query_string": name,
    }
    proxies = None if direct_first else _load_proxies()
    attempts = [None] if direct_first else []
    seen = set()
    if not direct_first:
        for _ in range(MAX_RETRIES):
            p = _next_proxy(proxies)
            if p and p not in seen:
                attempts.append(p)
                seen.add(p)
        attempts.append(None)  # final direct attempt
    proxies_loaded = not direct_first

    def _maybe_load_proxies():
        """Direct-first only: on the first failed direct attempt, load
        the health-checked proxy pool and append fresh proxies."""
        nonlocal proxies_loaded
        if not direct_first or proxies_loaded:
            return
        proxies_loaded = True
        pool = _load_proxies()
        for _ in range(MAX_RETRIES):
            p = _next_proxy(pool)
            if p and p not in seen:
                attempts.append(p)
                seen.add(p)

    last_err = None
    started = time.time()
    attempt = 0
    while attempt < len(attempts):
        proxy = attempts[attempt]
        attempt += 1
        # Same hard budget as fetch_snapshot: one name search can never
        # stall its request past SAFER_TOTAL_BUDGET.
        remaining = SAFER_TOTAL_BUDGET - (time.time() - started)
        if remaining <= 0:
            break
        req_proxies = {"http": proxy, "https": proxy} if proxy else None
        resp, err, timed_out = _request_with_deadline(
            lambda _rp=req_proxies: requests.get(
                SAFER_URL, params=params, headers=HEADERS,
                timeout=(SAFER_CONNECT_TIMEOUT, SAFER_READ_TIMEOUT),
                proxies=_rp),
            deadline=min(SAFER_ATTEMPT_DEADLINE, remaining))
        if timed_out:
            last_err = "Fetch timed out (connection stalled)"
            if proxy is not None and not direct_first:
                attempts = attempts[:attempt] + [None]
        elif err is not None:
            last_err = "Fetch error: %s" % err
        elif resp.status_code in (429, 503) or resp.status_code >= 500:
            last_err = "HTTP %s from SAFER" % resp.status_code
            _maybe_load_proxies()
            if attempt < len(attempts):
                wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                remaining = SAFER_TOTAL_BUDGET - (time.time() - started)
                if remaining <= 0:
                    break
                time.sleep(min(wait, remaining))
                continue
            return [], last_err
        else:
            try:
                resp.raise_for_status()
            except requests.RequestException as exc:
                last_err = "Fetch error: %s" % exc
            else:
                return parse_name_matches(resp.text, limit), None
        _maybe_load_proxies()
    return [], last_err or "Fetch error"
