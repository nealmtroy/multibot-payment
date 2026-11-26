import datetime as dt
import json
import re
import threading
import time
from urllib.parse import urlparse

from curl_cffi import requests


BASE_URL = "https://saweria.co"
BACKEND_URL = "https://backend.saweria.co"
DEFAULT_QRIS_EXPIRY_MINUTES = 15
CREATOR_CACHE_TTL_SECONDS = 300

_CREATOR_CACHE: dict[str, tuple[str, float]] = {}
_CREATOR_CACHE_LOCK = threading.Lock()


class SaweriaError(RuntimeError):
    pass


def noop_log(_message):
    pass


def new_session(cookie_header=""):
    session = requests.Session(impersonate="chrome")
    session.headers.update(
        {
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
    )
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    return session


def request_or_fail(session, method, url, **kwargs):
    response = session.request(method, url, timeout=30, **kwargs)
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise SaweriaError(f"{method} {url} failed: HTTP {response.status_code}: {snippet}")
    return response


def normalize_username(raw_username):
    raw = (raw_username or "").strip()
    if not raw:
        raise SaweriaError("Saweria username is empty.")
    if re.match(r"^https?://", raw, flags=re.I):
        parsed = urlparse(raw)
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            raise SaweriaError(f"Could not extract Saweria username from URL: {raw}")
        raw = path_parts[0]
    elif "saweria.co/" in raw.lower():
        after = raw.split("saweria.co/", 1)[1]
        raw = after.strip("/").split("/")[0]
    raw = raw.lstrip("@").strip("/")
    if not raw or not re.match(r"^[A-Za-z0-9_.-]+$", raw):
        raise SaweriaError(f"Invalid Saweria username: {raw_username}")
    return raw


def normalize_amount(amount):
    digits = re.sub(r"[^0-9]", "", str(amount))
    if not digits:
        raise SaweriaError("Amount must contain digits.")
    value = int(digits)
    if value < 1000:
        raise SaweriaError("Minimum Saweria QRIS amount is Rp1.000.")
    return value


def format_rupiah_plain(amount):
    return f"Rp{int(amount):,}".replace(",", ".")


def parse_saweria_datetime(raw_value):
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except Exception:
        pass
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def clear_creator_cache():
    with _CREATOR_CACHE_LOCK:
        _CREATOR_CACHE.clear()


def extract_creator_data(html):
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        flags=re.S | re.I,
    )
    if not match:
        raise SaweriaError("Could not find __NEXT_DATA__ in Saweria page HTML.")
    try:
        payload = json.loads(match.group(1))
    except ValueError as exc:
        raise SaweriaError("Invalid __NEXT_DATA__ JSON in Saweria page HTML.") from exc

    data = payload.get("props", {}).get("pageProps", {}).get("data")
    if not isinstance(data, dict) or not data.get("id"):
        raise SaweriaError("Saweria user profile not found or missing creator ID.")
    return data


def get_creator_profile(session, username, logger=noop_log):
    clean_username = normalize_username(username)
    profile_url = f"{BASE_URL}/{clean_username}"
    logger(f"[1/2] Opening Saweria page: {profile_url}")
    response = request_or_fail(session, "GET", profile_url)
    data = extract_creator_data(response.text)
    if data.get("isQRISEnabled") is False:
        raise SaweriaError(f"QRIS is disabled on Saweria account '{clean_username}'.")
    return data


def resolve_creator_id(session, username, use_cache=True, logger=noop_log):
    clean_username = normalize_username(username)
    if use_cache:
        now = time.monotonic()
        with _CREATOR_CACHE_LOCK:
            cached = _CREATOR_CACHE.get(clean_username.lower())
            if cached and (now - cached[1]) < CREATOR_CACHE_TTL_SECONDS:
                return cached[0], clean_username

    data = get_creator_profile(session, clean_username, logger=logger)
    creator_id = str(data["id"])
    with _CREATOR_CACHE_LOCK:
        _CREATOR_CACHE[clean_username.lower()] = (creator_id, time.monotonic())
    return creator_id, clean_username


def create_qris(session, username, amount, name, email, note, debug=False, logger=noop_log):
    creator_id, clean_username = resolve_creator_id(session, username, logger=logger)
    int_amount = normalize_amount(amount)

    logger("[2/2] Creating Saweria QRIS donation...")
    payload = {
        "agree": True,
        "notUnderage": True,
        "amount": int_amount,
        "currency": "IDR",
        "message": str(note or "")[:255],
        "payment_type": "qris",
        "vote": "",
        "customer_info": {
            "first_name": name,
            "email": email,
            "phone": "",
        },
    }
    headers = {
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/{clean_username}",
        "Accept": "application/json, text/plain, */*",
    }
    create_url = f"{BACKEND_URL}/donations/snap/{creator_id}"
    response = session.request("POST", create_url, json=payload, headers=headers, timeout=30)
    if response.status_code == 404:
        # Refresh creator_id cache once in case creator profile changed
        with _CREATOR_CACHE_LOCK:
            _CREATOR_CACHE.pop(clean_username.lower(), None)
        creator_id, clean_username = resolve_creator_id(session, clean_username, use_cache=False, logger=logger)
        create_url = f"{BACKEND_URL}/donations/snap/{creator_id}"
        response = request_or_fail(session, "POST", create_url, json=payload, headers=headers)
    elif response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise SaweriaError(f"POST {create_url} failed: HTTP {response.status_code}: {snippet}")

    try:
        raw_json = response.json()
    except ValueError as exc:
        raise SaweriaError(f"Saweria create QRIS did not return JSON: {response.text[:500]}") from exc

    if debug:
        logger("Saweria create QRIS response:")
        logger(json.dumps(raw_json, indent=2, ensure_ascii=False))

    data = raw_json.get("data") or {}
    donation_id = data.get("id")
    qr_string = data.get("qr_string")
    if not donation_id or not qr_string:
        raise SaweriaError(f"Saweria QRIS response missing id or qr_string: {raw_json}")

    amount_raw = int(data.get("amount_raw") or data.get("amount") or int_amount)
    created_dt = parse_saweria_datetime(data.get("created_at")) or dt.datetime.now(dt.UTC)
    expires_iso = (created_dt + dt.timedelta(minutes=DEFAULT_QRIS_EXPIRY_MINUTES)).replace(microsecond=0).isoformat()
    payment_url = f"{BASE_URL}/qris/snap/{donation_id}"

    normalized_qris = {
        "status": True,
        "inv_id": donation_id,
        "source_payment": "saweria",
        "data": {
            "id": donation_id,
            "qr_string": qr_string,
            "amount": format_rupiah_plain(amount_raw),
            "amount_raw": amount_raw,
            "countdown": expires_iso,
            "username": clean_username,
        },
    }
    return donation_id, payment_url, normalized_qris


def check_pending(session, inv_id, logger=noop_log):
    logger(f"Checking Saweria payment status for {inv_id}...")
    url = f"{BACKEND_URL}/donations/qris/snap/{inv_id}"
    headers = {
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/qris/snap/{inv_id}",
        "Accept": "application/json, text/plain, */*",
    }
    start = time.perf_counter()
    response = session.request("GET", url, headers=headers, timeout=30)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    if response.status_code == 404:
        return "failed_or_expired", url, elapsed_ms
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise SaweriaError(f"GET {url} failed: HTTP {response.status_code}: {snippet}")

    try:
        raw_json = response.json()
    except ValueError as exc:
        raise SaweriaError(f"Saweria check status did not return JSON: {response.text[:500]}") from exc

    data = raw_json.get("data") or {}
    tx_status = str(data.get("transaction_status") or "").strip().upper()

    if tx_status in {"SUCCESS", "SETTLED", "PAID", "COMPLETED"}:
        return "paid", url, elapsed_ms
    if tx_status in {"PENDING", "UNPAID", "WAITING"}:
        return "pending", url, elapsed_ms
    if tx_status in {"FAILED", "EXPIRED", "CANCELLED", "CANCELED", "DENY"}:
        return "failed_or_expired", url, elapsed_ms
    return "unknown", url, elapsed_ms
