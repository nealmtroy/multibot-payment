import asyncio
import datetime as dt
import html
import io
import json
import logging
import random
import re
import secrets
import string
import time
import requests
from telethon import Button, errors, functions
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityBotCommand,
    MessageEntityCode,
    MessageEntityEmail,
    MessageEntityHashtag,
    MessageEntityItalic,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageEntityPhone,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
)

from sociabuzz_client import (
    SociaBuzzError,
    create_donation_order,
    create_qris,
    download_qr_response,
    new_session,
    check_pending,
)
from vip_bot.config import (
    FIRST_NAMES,
    LAST_NAMES,
    MIN_WITHDRAWAL_AMOUNT,
    WIB,
    BROADCAST_DISABLED_VALUES,
    BROADCAST_TIME_PATTERN,
)

LOGGER = logging.getLogger("telegram_vip_bot.helpers")

ENTITY_NAME_MAP = {
    "MessageEntityBlockquote": MessageEntityBlockquote,
    "MessageEntityBold": MessageEntityBold,
    "MessageEntityBotCommand": MessageEntityBotCommand,
    "MessageEntityCode": MessageEntityCode,
    "MessageEntityEmail": MessageEntityEmail,
    "MessageEntityHashtag": MessageEntityHashtag,
    "MessageEntityItalic": MessageEntityItalic,
    "MessageEntityMention": MessageEntityMention,
    "MessageEntityMentionName": MessageEntityMentionName,
    "MessageEntityPhone": MessageEntityPhone,
    "MessageEntityPre": MessageEntityPre,
    "MessageEntitySpoiler": MessageEntitySpoiler,
    "MessageEntityStrike": MessageEntityStrike,
    "MessageEntityTextUrl": MessageEntityTextUrl,
    "MessageEntityUnderline": MessageEntityUnderline,
    "MessageEntityUrl": MessageEntityUrl,
}


def utc_now_iso():
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def parse_iso_datetime(val):
    if not val:
        return None
    if isinstance(val, dt.datetime):
        return val if val.tzinfo else val.replace(tzinfo=dt.UTC)
    raw = str(val).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def adaptive_poll_delay(created_at, expires_at=None, attempts=0, error=""):
    if error:
        return min(30, 2 ** min(attempts, 5))
    return 3


def next_poll_at(created_at, expires_at=None, attempts=0, error=""):
    now = dt.datetime.now(dt.UTC)
    delay = adaptive_poll_delay(created_at, expires_at, attempts=attempts, error=error)
    return (now + dt.timedelta(seconds=delay)).replace(microsecond=0).isoformat()


def format_rupiah(amount):
    return f"Rp{int(amount):,}".replace(",", ".")


def format_button_amount(amount):
    return f"{int(amount):,}".replace(",", ".")


def format_custom_qris_expiry(countdown_iso):
    expires = parse_iso_datetime(countdown_iso)
    if not expires:
        return ""
    local = expires.astimezone(WIB)
    return local.strftime("%d %b %Y, %H:%M WIB")


def format_qris_expiry(raw_expires):
    if not raw_expires:
        return ""
    parsed = parse_iso_datetime(raw_expires)
    if not parsed:
        return raw_expires
    return parsed.astimezone(WIB).strftime("%d/%m/%Y %H:%M WIB")


def format_log_datetime(raw_iso):
    parsed = parse_iso_datetime(raw_iso)
    if not parsed:
        return "-"
    local = parsed.astimezone(WIB)
    return local.strftime("%d %b %Y, %H:%M WIB")


def display_name(user):
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or getattr(user, "username", "") or str(getattr(user, "id", ""))


def random_indonesian_identity():
    first = secrets.choice(FIRST_NAMES)
    last = secrets.choice(LAST_NAMES)
    num = random.randint(1000, 999999)
    email = f"{first.lower()}.{last.lower()}{num}@gmail.com"
    return f"{first} {last}", email


def public_invoice_id():
    date_part = dt.datetime.now(WIB).strftime("%y%m%d")
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"VIP-{date_part}-{suffix}"


def format_referral_code(user_id):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    value = (int(user_id) * 2654435761) & 0xFFFFFFFF
    chars = []
    for _ in range(6):
        chars.append(alphabet[value % len(alphabet)])
        value //= len(alphabet)
    return "".join(chars).rstrip("A") or "A2345"


def parse_referral_payload(payload):
    raw = (payload or "").strip()
    if raw.lower().startswith("ref_"):
        raw = raw[4:]
    raw = raw.upper()
    if 5 <= len(raw) <= 6 and all(ch in string.ascii_uppercase + string.digits for ch in raw):
        return raw
    return ""


def should_create_referral(invited_user_id, referrer_user_id, existing_referral=None):
    return bool(referrer_user_id) and int(invited_user_id) != int(referrer_user_id) and not existing_referral


def updated_referral_counters(user_row, commission):
    return {
        "balance": int(user_row.get("balance") or 0) + int(commission),
        "pending_referrals": max(0, int(user_row.get("pending_referrals") or 0) - 1),
        "successful_referrals": int(user_row.get("successful_referrals") or 0) + 1,
    }


def valid_withdrawal_amount(amount, balance):
    try:
        val = int(amount)
        return val >= MIN_WITHDRAWAL_AMOUNT and val <= int(balance)
    except (TypeError, ValueError):
        return False


def referral_commission(payment):
    amount = int(payment.get("package_amount") or payment.get("amount") or 0)
    return amount // 2


def parse_withdrawal_amount(raw):
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return int(digits) if digits else 0


def parse_withdrawal_details(raw):
    fields = {}
    labels = {
        "no hp": "phone",
        "nama e-wallet": "wallet_name",
        "atas nama": "account_name",
    }
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower()
        if normalized in labels:
            fields[labels[normalized]] = value.strip()
    missing = [label for label, key in labels.items() if not fields.get(key)]
    if missing:
        raise ValueError("Format data penarikan belum lengkap.")
    return fields


def withdrawal_details_text(amount_or_raw, phone=None, wallet_name=None, account_name=None):
    if phone is None and isinstance(amount_or_raw, str) and ("\n" in amount_or_raw or ":" in amount_or_raw):
        return parse_withdrawal_details(amount_or_raw)
    amount = amount_or_raw
    return (
        "<b>Konfirmasi Penarikan Saldo</b>\n\n"
        f"• Nominal: <b>{format_rupiah(amount)}</b>\n"
        f"• No HP E-Wallet: <code>{html.escape(str(phone or ''))}</code>\n"
        f"• Nama E-Wallet: <b>{html.escape(str(wallet_name or ''))}</b>\n"
        f"• Atas Nama: <b>{html.escape(str(account_name or ''))}</b>\n\n"
        "Pastikan data di atas sudah benar sebelum klik Konfirmasi."
    )


def validate_broadcast_time(raw):
    value = (raw or "").strip().lower()
    if value in BROADCAST_DISABLED_VALUES:
        return ""
    if not BROADCAST_TIME_PATTERN.fullmatch(value):
        raise ValueError("Format: /set_broadcasttime HH:MM atau /set_broadcasttime off")
    return value


def is_active_payment_duplicate(exc):
    return "idx_vip_payments_one_active_per_user" in str(exc) or "unique" in str(exc).lower()


def is_cloudflare_challenge(exc):
    text = str(exc)
    return "HTTP 403" in text and ("Just a moment" in text or "challenges.cloudflare.com" in text)


def is_user_blocked_error(exc):
    return isinstance(exc, errors.UserIsBlockedError) or "User is blocked" in str(exc)


def is_user_deactivated_error(exc):
    deactivated_error = getattr(errors, "InputUserDeactivatedError", None)
    return (deactivated_error is not None and isinstance(exc, deactivated_error)) or "user was deleted" in str(exc).lower()


def is_sociabuzz_timeout(exc):
    return isinstance(exc, (requests.Timeout, TimeoutError)) or "timed out" in str(exc).lower()


def entities_to_json(entities):
    if not entities:
        return "[]"
    return json.dumps([entity.to_dict() for entity in entities], ensure_ascii=False)


def json_to_entities(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("Invalid broadcast entities JSON: %r", raw)
        return []
    entities = []
    for item in data or []:
        cls = ENTITY_NAME_MAP.get(item.get("_"))
        if not cls:
            continue
        args = {key: value for key, value in item.items() if key != "_"}
        try:
            entities.append(cls(**args))
        except TypeError as exc:
            LOGGER.warning("Failed to rebuild entity %s: %s", item.get("_"), exc)
    return entities


def is_admin(config, user_id):
    return user_id in config.admin_user_ids


def username_or_name(row):
    username = (row.get("username") or "").strip()
    if username:
        return f"@{html.escape(username)}"
    return html.escape(row.get("full_name") or str(row.get("user_id") or ""))


def plain_user_link(row):
    user_id = int(row.get("user_id") or 0)
    return f'<a href="tg://user?id={user_id}">{username_or_name(row)}</a>'


def referral_user_log_text(row):
    user_id = int(row["user_id"])
    name = html.escape(row.get("full_name") or str(user_id))
    username = (row.get("username") or "").strip()
    username_line = f"\nUsername: @{html.escape(username)}" if username else ""
    return f'<a href="tg://user?id={user_id}">{name}</a>{username_line}\nUser ID: <code>{user_id}</code>'


def telegram_user_link(user):
    name = html.escape(display_name(user))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def internal_telegram_chat_url(chat_id):
    raw = str(chat_id or "").strip()
    if not raw.startswith("-100"):
        return ""
    internal_id = raw[4:]
    if not internal_id.isdigit():
        return ""
    return f"https://t.me/c/{internal_id}/4"


def parse_chat_setting(event, raw_value):
    value = raw_value.strip()
    if value.lower() in {"here", "this"}:
        return event.chat_id
    return int(value)


def normalize_package_code(code):
    normalized = (code or "").strip().lower()
    if not normalized:
        raise ValueError("Kode paket wajib diisi.")
    if len(normalized) > 32:
        raise ValueError("Kode paket maksimal 32 karakter.")
    allowed = set(string.ascii_lowercase + string.digits + "_-")
    if any(ch not in allowed for ch in normalized):
        raise ValueError("Kode paket hanya boleh huruf, angka, underscore, dan strip.")
    return normalized


def user_link(payment):
    name = html.escape(payment.get("full_name") or str(payment.get("user_id")))
    return f'<a href="tg://user?id={payment["user_id"]}">{name}</a>'


def create_qris_sync(config, user, amount=None, note_prefix="VIP"):
    session = new_session(config.sociabuzz_cookie)
    buyer_name, buyer_email = random_indonesian_identity()
    checkout_amount = amount if amount is not None else config.payment_amount
    note = f"{note_prefix} {user.id}"
    order_id, payment_url, _ = create_donation_order(
        session,
        config.sociabuzz_username,
        checkout_amount,
        buyer_name,
        buyer_email,
        note,
    )
    qris = create_qris(session, order_id, payment_url, checkout_amount, source_payment="xendit")
    qr_response = download_qr_response(session, qris)
    return (
        session,
        buyer_name,
        buyer_email,
        order_id,
        payment_url,
        qris,
        qr_response.content,
        checkout_amount,
    )


def create_qris_with_retries_sync(config, user, amount=None, note_prefix="VIP", attempts=3):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return create_qris_sync(config, user, amount, note_prefix)
        except Exception as exc:
            last_exc = exc
            if not is_sociabuzz_timeout(exc) or attempt >= attempts:
                raise
            delay = min(6, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            LOGGER.warning(
                "SociaBuzz timed out while creating %s QRIS for user %s, retrying in %.1fs (%s/%s): %s",
                note_prefix,
                user.id,
                delay,
                attempt,
                attempts,
                exc,
            )
            time.sleep(delay)
    raise last_exc


def check_payment_sync(config, inv_id):
    session = new_session(config.sociabuzz_cookie)
    return check_pending(session, inv_id)


# Asynchronous DB-dependent Helpers

async def runtime_vip_chat_id(config, db):
    if hasattr(db, "get_setting"):
        val = await db.get_setting("vip_chat_id")
        if val:
            try:
                return int(val)
            except ValueError:
                pass
    return config.vip_chat_id


async def runtime_log_chat_id(config, db):
    if hasattr(db, "get_setting"):
        val = await db.get_setting("log_chat_id")
        if val:
            try:
                return int(val)
            except ValueError:
                pass
    return config.log_chat_id


async def send_log(client, config, db, text, **kwargs):
    # Always route logging through the Master Management Bot if available
    sender = getattr(client, "master_client", None) or client
    try:
        log_chat_id = await runtime_log_chat_id(config, db)
    except Exception as exc:
        LOGGER.warning("Failed to load runtime log_chat_id: %s", exc)
        log_chat_id = config.log_chat_id
    if not log_chat_id:
        return
    try:
        await sender.send_message(log_chat_id, text, parse_mode="html", link_preview=False, **kwargs)
    except Exception as exc:
        LOGGER.warning("Failed to send log message to %s: %s", log_chat_id, exc)


async def safe_send_user(client, config, db, user_id, text, **kwargs):
    try:
        await client.send_message(user_id, text, **kwargs)
        return "sent"
    except Exception as exc:
        if is_user_blocked_error(exc):
            LOGGER.warning("User %s blocked the bot, cannot deliver message", user_id)
            status = "blocked"
            log_title = "User delivery blocked"
        elif is_user_deactivated_error(exc):
            LOGGER.warning("User %s is deleted/deactivated, cannot deliver message", user_id)
            status = "blocked"
            log_title = "User delivery deactivated"
        else:
            LOGGER.exception("Failed to send message to user %s", user_id)
            status = "error"
            log_title = "User delivery error"
        await send_log(
            client,
            config,
            db,
            (
                f"<b>{log_title}</b>\n"
                f"User: <code>{user_id}</code>\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            ),
        )
        return status


async def send_broadcast_to_user(client, db, broadcast_message, user_id):
    async def send_once():
        text = broadcast_message.get("message_text") or ""
        media_file_id = broadcast_message.get("media_telegram_file_id") or ""
        entities = json_to_entities(broadcast_message.get("entities_json") or "[]")
        entity_kwargs = {"formatting_entities": entities} if entities else {}
        if media_file_id:
            await client.send_file(user_id, media_file_id, caption=text or None, parse_mode=None, **entity_kwargs)
        elif text:
            await client.send_message(user_id, text, parse_mode=None, **entity_kwargs)
        else:
            return "empty"
        return "sent"

    error = None
    try:
        return await send_once()
    except FloodWaitError as exc:
        LOGGER.warning("FloodWait %ss while broadcasting to user %s", exc.seconds, user_id)
        await asyncio.sleep(max(1, int(exc.seconds)))
        try:
            return await send_once()
        except Exception as retry_exc:
            error = retry_exc
    except Exception as exc:
        error = exc

    if is_user_blocked_error(error):
        return "blocked"
    if is_user_deactivated_error(error):
        return "deactivated"
    return "error"


async def delete_qris_message(client, payment):
    chat_id = payment.get("qris_chat_id")
    message_id = payment.get("qris_message_id")
    invoice_id = payment.get("public_invoice_id") or payment.get("inv_id")
    if not chat_id or not message_id:
        return
    try:
        await client.delete_messages(int(chat_id), [int(message_id)], revoke=True)
    except Exception as exc:
        pass


async def create_invite_link(client, config, db, payment):
    vip_chat_id = int(payment.get("vip_chat_id") or 0) or (await runtime_vip_chat_id(config, db))
    if not vip_chat_id:
        raise RuntimeError("VIP chat belum di-set. Admin perlu set paket atau pakai /setvip <chat_id>.")
    invite_hours = int(payment.get("invite_expire_hours") or 0) or config.invite_expire_hours
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=invite_hours)
    title_name = payment.get("package_name") or "VIP"
    result = await client(
        functions.messages.ExportChatInviteRequest(
            peer=vip_chat_id,
            expire_date=expires_at,
            usage_limit=1,
            title=f"{title_name} {payment['inv_id']}",
        )
    )
    return result.link, expires_at.replace(microsecond=0).isoformat()
