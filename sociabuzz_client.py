import atexit
import concurrent.futures
import io
import json
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from camoufox.sync_api import Camoufox


BASE_URL = "https://sociabuzz.com"
_SHUTDOWN_SENTINEL = object()


class SociaBuzzError(RuntimeError):
    pass


def noop_log(_message):
    pass


def _parse_cookie_header(cookie_header):
    cookies = {}
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def _is_cloudflare_html(title, body):
    title_l = (title or "").lower()
    body_l = (body or "").lower()
    return "just a moment" in title_l or "challenges.cloudflare.com" in body_l or "cf-browser-verification" in body_l


class CamoufoxResponse:
    def __init__(self, status_code, text="", content=b"", headers=None, url=""):
        self.status_code = int(status_code)
        self.text = text or ""
        if content:
            self.content = content
        else:
            self.content = self.text.encode("utf-8")
        self.headers = headers or {}
        self.url = url

    def json(self):
        return json.loads(self.text)


class _CamoufoxWorker:
    """Dedicated single-thread actor running a warm headless Camoufox browser."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = None
        self._thread = None
        self._cm = None
        self._browser = None
        self._contexts = {}
        self._next_ctx_id = 1

    def _ensure_started(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._queue = queue.Queue()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="camoufox-sociabuzz-worker",
                daemon=True,
            )
            self._thread.start()

    def _start_browser(self):
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return
            except Exception:
                pass
            self._stop_browser()
        self._cm = Camoufox(headless=True)
        self._browser = self._cm.__enter__()

    def _stop_browser(self):
        for entry in list(self._contexts.values()):
            try:
                entry["context"].close()
            except Exception:
                pass
        self._contexts.clear()
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        self._cm = None
        self._browser = None

    def _run_loop(self):
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN_SENTINEL:
                    break
                fn, fut = item
                if fut.set_running_or_notify_cancel():
                    try:
                        res = fn()
                        fut.set_result(res)
                    except BaseException as exc:
                        fut.set_exception(exc)
        finally:
            self._stop_browser()

    def submit(self, fn, timeout=60):
        self._ensure_started()
        fut = concurrent.futures.Future()
        self._queue.put((fn, fut))
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(f"Camoufox operation timed out after {timeout}s") from exc

    def create_context(self, cookie_header="", extra_headers=None):
        def _op():
            self._start_browser()
            ctx = self._browser.new_context(locale="en-US")
            if extra_headers:
                ctx.set_extra_http_headers(extra_headers)
            parsed = _parse_cookie_header(cookie_header)
            if parsed:
                ctx.add_cookies(
                    [
                        {
                            "name": k,
                            "value": v,
                            "domain": ".sociabuzz.com",
                            "path": "/",
                        }
                        for k, v in parsed.items()
                    ]
                )
            page = ctx.new_page()
            ctx_id = self._next_ctx_id
            self._next_ctx_id += 1
            self._contexts[ctx_id] = {"context": ctx, "page": page}
            return ctx_id

        return self.submit(_op, timeout=60)

    def close_context(self, ctx_id, wait=True):
        if not ctx_id:
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return

        def _op():
            entry = self._contexts.pop(ctx_id, None)
            if entry:
                try:
                    entry["context"].close()
                except Exception:
                    pass

        if wait:
            try:
                self.submit(_op, timeout=15)
            except Exception:
                pass
        else:
            fut = concurrent.futures.Future()
            self._queue.put((_op, fut))

    def get_cookies(self, ctx_id):
        def _op():
            entry = self._contexts.get(ctx_id)
            if not entry:
                return {}
            return {c["name"]: c["value"] for c in entry["context"].cookies()}

        return self.submit(_op, timeout=15)

    def perform_request(self, ctx_id, method, url, headers=None, data=None, json_body=None, timeout=30):
        def _op():
            entry = self._contexts.get(ctx_id)
            if not entry:
                raise SociaBuzzError("Camoufox context is closed.")
            ctx = entry["context"]
            page = entry["page"]
            method_u = (method or "GET").upper()
            req_headers = dict(headers or {})
            timeout_ms = max(1000, int((timeout or 30) * 1000))

            parsed_url = urlparse(url)
            is_same_origin = (parsed_url.netloc or "").lower().endswith("sociabuzz.com")
            is_ajax = any(k.lower() == "x-requested-with" for k in req_headers) or json_body is not None or data is not None
            is_pending_check = "/payment/pending" in parsed_url.path

            try:
                if is_same_origin and is_pending_check and method_u == "GET":
                    resp = ctx.request.get(url, headers=req_headers or None, timeout=timeout_ms)
                    text = resp.text()
                    if resp.status == 403 and _is_cloudflare_html("", text):
                        return self._goto_page(page, url, timeout_ms)
                    return CamoufoxResponse(
                        status_code=resp.status,
                        text=text,
                        content=resp.body(),
                        headers=dict(resp.headers),
                        url=resp.url,
                    )

                if is_same_origin and method_u == "GET" and not is_ajax and "/payment/pay/setting" not in parsed_url.path:
                    return self._goto_page(page, url, timeout_ms)

                if is_same_origin:
                    if not (page.url or "").startswith(BASE_URL):
                        self._goto_page(page, BASE_URL, timeout_ms)

                    fetch_headers = {
                        k: v
                        for k, v in req_headers.items()
                        if k.lower() not in {"origin", "referer", "host", "content-length", "cookie"}
                    }
                    body_str = None
                    if json_body is not None:
                        body_str = json.dumps(json_body)
                        if not any(k.lower() == "content-type" for k in fetch_headers):
                            fetch_headers["Content-Type"] = "application/json"
                    elif data is not None:
                        body_str = urlencode(data) if isinstance(data, dict) else str(data)
                        if not any(k.lower() == "content-type" for k in fetch_headers):
                            fetch_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

                    result = page.evaluate(
                        """async ({ url, method, headers, body }) => {
                            const opts = { method, headers, credentials: 'include' };
                            if (body !== null && body !== undefined && method !== 'GET' && method !== 'HEAD') {
                                opts.body = body;
                            }
                            const resp = await fetch(url, opts);
                            const text = await resp.text();
                            const respHeaders = {};
                            resp.headers.forEach((v, k) => { respHeaders[k] = v; });
                            return { status: resp.status, text, headers: respHeaders, url: resp.url };
                        }""",
                        {
                            "url": url,
                            "method": method_u,
                            "headers": fetch_headers,
                            "body": body_str,
                        },
                    )
                    return CamoufoxResponse(
                        status_code=result["status"],
                        text=result["text"],
                        headers=result.get("headers") or {},
                        url=result.get("url") or url,
                    )

                fetch_kwargs = {"method": method_u, "timeout": timeout_ms}
                if req_headers:
                    fetch_kwargs["headers"] = req_headers
                if json_body is not None:
                    fetch_kwargs["data"] = json_body
                elif data is not None:
                    fetch_kwargs["form"] = data
                resp = ctx.request.fetch(url, **fetch_kwargs)
                raw_bytes = resp.body()
                return CamoufoxResponse(
                    status_code=resp.status,
                    text=raw_bytes.decode("utf-8", errors="replace"),
                    content=raw_bytes,
                    headers=dict(resp.headers),
                    url=resp.url,
                )
            except Exception as exc:
                msg = str(exc)
                if "Timeout" in type(exc).__name__ or "timed out" in msg.lower() or "timeout" in msg.lower():
                    raise TimeoutError(f"Camoufox request timed out: {msg}") from exc
                raise

        return self.submit(_op, timeout=max(30, int(timeout or 30) + 15))

    def _goto_page(self, page, url, timeout_ms):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = resp.status if resp else 200
        deadline = time.monotonic() + min(15.0, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            title = page.title()
            html = page.content()
            if not _is_cloudflare_html(title, html):
                return CamoufoxResponse(status_code=status, text=html, url=page.url)
            page.wait_for_timeout(500)
            status = 200
        html = page.content()
        return CamoufoxResponse(status_code=status, text=html, url=page.url)

    def shutdown(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return
            q = self._queue
            t = self._thread
            self._thread = None
            self._queue = None
        if q is not None:
            q.put(_SHUTDOWN_SENTINEL)
        if t is not None and t.is_alive():
            t.join(timeout=5)


_CAMOUFOX_WORKER = _CamoufoxWorker()
atexit.register(_CAMOUFOX_WORKER.shutdown)


def close_camoufox():
    _CAMOUFOX_WORKER.shutdown()


class CamoufoxSession:
    def __init__(self, cookie_header="", worker=None):
        self._worker = worker or _CAMOUFOX_WORKER
        self._cookie_header = cookie_header or ""
        self._initial_cookies = _parse_cookie_header(self._cookie_header)
        self._ctx_id = None
        self.headers = {
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
        if self._cookie_header:
            self.headers["Cookie"] = self._cookie_header

    def _ensure_context(self):
        if self._ctx_id is None:
            extra_headers = {k: v for k, v in self.headers.items() if k.lower() != "cookie"}
            self._ctx_id = self._worker.create_context(
                cookie_header=self._cookie_header,
                extra_headers=extra_headers,
            )
        return self._ctx_id

    @property
    def cookies(self):
        if self._ctx_id is None:
            return dict(self._initial_cookies)
        merged = dict(self._initial_cookies)
        merged.update(self._worker.get_cookies(self._ctx_id))
        return merged

    def request(self, method, url, timeout=30, headers=None, data=None, json=None, **_kwargs):
        ctx_id = self._ensure_context()
        return self._worker.perform_request(
            ctx_id,
            method,
            url,
            headers=headers,
            data=data,
            json_body=json,
            timeout=timeout,
        )

    def close(self):
        if self._ctx_id is not None:
            ctx_id = self._ctx_id
            self._ctx_id = None
            self._worker.close_context(ctx_id, wait=True)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        self.close()

    def __del__(self):
        if getattr(self, "_ctx_id", None) is not None and getattr(self, "_worker", None) is not None:
            try:
                ctx_id = self._ctx_id
                self._ctx_id = None
                self._worker.close_context(ctx_id, wait=False)
            except Exception:
                pass


def new_session(cookie_header=""):
    return CamoufoxSession(cookie_header=cookie_header)


def request_or_fail(session, method, url, **kwargs):
    response = session.request(method, url, timeout=30, **kwargs)
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise SociaBuzzError(f"{method} {url} failed: HTTP {response.status_code}: {snippet}")
    return response


def extract_csrf(html):
    match = re.search(
        r'name=["\']sb_token_csrf["\']\s+value=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if not match:
        match = re.search(
            r'value=["\']([^"\']+)["\']\s+name=["\']sb_token_csrf["\']',
            html,
            flags=re.I,
        )
    if not match:
        raise SociaBuzzError("Could not find sb_token_csrf in donate form HTML.")
    return match.group(1)


def normalize_amount(amount):
    digits = re.sub(r"[^0-9]", "", str(amount))
    if not digits:
        raise SociaBuzzError("Amount must contain digits.")
    return f"{int(digits):,}"


def create_donation_order(session, username, amount, name, email, note, debug=False, logger=noop_log):
    tribe_url = f"{BASE_URL}/{username}/tribe"
    logger(f"[1/5] Opening TRIBE page: {tribe_url}")
    request_or_fail(session, "GET", tribe_url)

    form_url = f"{BASE_URL}/{username}/donate/queue?type=donate&currency=IDR&message="
    logger("[2/5] Loading donate form and CSRF token...")
    form_response = request_or_fail(
        session,
        "GET",
        form_url,
        headers={"Referer": tribe_url, "X-Requested-With": "XMLHttpRequest"},
    )
    csrf = extract_csrf(form_response.text)
    logger("[3/5] Creating donation order...")

    payload = {
        "sb_token_csrf": csrf,
        "currency": "IDR",
        "amount": normalize_amount(amount),
        "qty": "1",
        "support_duration": "30",
        "note": note,
        "fullname": name,
        "email": email,
        "is_agree": "1",
        "years18": "1",
        "is_vote": "0",
        "is_voice": "0",
        "is_mediashare": "0",
        "is_gif": "0",
        "is_sound": "0",
        "is_voicy": "0",
        "vote_id": "",
        "voice": "",
        "ms_maxtime": "",
        "start_from": "0",
        "ms_starthour": "0",
        "ms_startminute": "0",
        "ms_startsecond": "0",
        "spin_check": "0",
        "prev_url": tribe_url,
        "hide_email": "0",
        "is_tiktok": "0",
        "tiktok_duration": "0",
        "is_instagram": "0",
        "instagram_duration": "0",
        "wishlist_id": "",
        "quickpay": "0",
    }

    submit_url = f"{BASE_URL}/{username}/donate/get-form-queue"
    submit_response = request_or_fail(
        session,
        "POST",
        submit_url,
        data=payload,
        headers={
            "Referer": tribe_url,
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )

    try:
        data = submit_response.json()
    except ValueError as exc:
        raise SociaBuzzError(f"Create order did not return JSON: {submit_response.text[:500]}") from exc

    if debug:
        logger("Create order response:")
        logger(json.dumps(data, indent=2, ensure_ascii=False))

    redirect = (
        data.get("content", {}).get("redirect")
        or data.get("redirect")
        or data.get("content", {}).get("url")
    )
    if not redirect:
        raise SociaBuzzError(f"Could not find payment redirect in response: {data}")

    payment_url = urljoin(BASE_URL, redirect)
    order_id = Path(urlparse(payment_url).path).name
    if not order_id:
        raise SociaBuzzError(f"Could not parse order id from payment URL: {payment_url}")

    return order_id, payment_url, data


def create_qris(session, order_id, payment_url, amount, source_payment="midtrans", debug=False, logger=noop_log):
    logger(f"[4/5] Opening payment page: {payment_url}")
    payment_response = request_or_fail(session, "GET", payment_url)
    payment_html = payment_response.text

    cookies = session.cookies
    csrf = cookies.get("csrf_cookie_name") or cookies.get("sociabuzz_sb_cookie_csrf")
    if not csrf:
        csrf_match = re.search(r"csrf[_-]hash['\"]?\s*[:=]\s*['\"]([^'\"]+)", payment_html, flags=re.I)
        csrf = csrf_match.group(1) if csrf_match else ""
    if not csrf:
        raise SociaBuzzError("Could not find payment CSRF token. Try passing cookie from browser.")

    logger("[5/5] Loading payment settings and creating QRIS...")
    plain_amount = re.sub(r"[^0-9]", "", str(amount)) or "1000"
    setting_url = (
        f"{BASE_URL}/payment/pay/setting"
        f"?amount={plain_amount}&currency=IDR&base_amount={plain_amount}&base_currency=IDR"
        f"&currency_def=IDR&convertion=IDR&country=Indonesia&feature=TRIBE"
        f"&is_borne_fee=1&risk=&message=&direct=&service_fee=0"
        f"&token={order_id}&country_account="
    )
    request_or_fail(session, "GET", setting_url, headers={"Referer": payment_url})

    payload = {
        "sb_token_csrf": csrf,
        "order_id": order_id,
        "final_currency": "IDR",
        "currency_def": "IDR",
        "payment_method": "qris",
        "type_payment": "qris",
        "source_payment": source_payment,
        "country": "ID",
        "country_pay": "Indonesia",
    }
    response = request_or_fail(
        session,
        "POST",
        f"{BASE_URL}/payment/send/create",
        json=payload,
        headers={
            "Referer": payment_url,
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
        },
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise SociaBuzzError(f"Create QRIS did not return JSON: {response.text[:500]}") from exc

    if debug:
        logger("Create QRIS response:")
        logger(json.dumps(data, indent=2, ensure_ascii=False))

    if not data.get("status"):
        raise SociaBuzzError(f"Create QRIS failed: {data}")

    return data


def download_qr(session, qris_data, output_path):
    response = download_qr_response(session, qris_data)
    path = Path(output_path)
    path.write_bytes(response.content)
    return path


def download_qr_response(session, qris_data):
    qr_string = qris_data.get("data", {}).get("qr_string")
    if not qr_string:
        raise SociaBuzzError("QR response has no data.qr_string.")
    if re.match(r"^https?://", qr_string, flags=re.I):
        return request_or_fail(session, "GET", qr_string)
    return LocalResponse(render_qr_payload(qr_string))


class LocalResponse:
    def __init__(self, content):
        self.content = content


def render_qr_payload(qr_payload):
    try:
        import qrcode
    except ImportError as exc:
        raise SociaBuzzError(
            "Xendit returns a direct QRIS payload, not an image URL. "
            "Install QR renderer first: pip install qrcode[pil]"
        ) from exc
    image = qrcode.make(qr_payload)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def check_pending(session, inv_id, logger=noop_log):
    logger(f"Checking payment status for {inv_id}...")
    url = f"{BASE_URL}/payment/pending?type=qris&inv_id={inv_id}"
    start = time.perf_counter()
    response = request_or_fail(session, "GET", url)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    text = re.sub(r"\s+", " ", response.text).lower()

    if any(marker in text for marker in ("transaction success", "pembayaran berhasil", "terima kasih")):
        return "paid", url, elapsed_ms
    if any(marker in text for marker in ("pembayaran belum selesai", "transaction pending", "pending")):
        return "pending", url, elapsed_ms
    if any(marker in text for marker in ("expired", "kedaluwarsa", "failed", "gagal")):
        return "failed_or_expired", url, elapsed_ms
    return "unknown", url, elapsed_ms


def poll_status(session, inv_id, interval, max_polls, logger=noop_log):
    for index in range(1, max_polls + 1):
        status, status_url, elapsed_ms = check_pending(session, inv_id, logger=logger)
        logger(f"poll {index}/{max_polls}: {status} | {elapsed_ms} ms | {status_url}")
        if status != "pending":
            return status
        if index < max_polls:
            logger(f"Waiting {interval}s before next check...")
            time.sleep(interval)
    return "pending"
