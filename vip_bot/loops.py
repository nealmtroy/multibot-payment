import asyncio
import datetime as dt
import html
import logging
from vip_bot.config import WIB, BROADCAST_DISABLED_VALUES
from vip_bot.helpers import (
    check_payment_sync,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    parse_iso_datetime,
    next_poll_at,
    utc_now_iso,
    send_log,
    safe_send_user,
    delete_qris_message,
    create_invite_link,
    send_broadcast_to_user,
    user_link,
    plain_user_link,
    telegram_user_link,
    format_rupiah,
    referral_commission,
    internal_telegram_chat_url,
)
from vip_bot.messages import (
    invalid_payment_message,
    timeout_payment_message,
    package_buttons,
    paid_message,
)

LOGGER = logging.getLogger("telegram_vip_bot.loops")


def resolve_client(bot_manager_or_client, bot_code="default"):
    if hasattr(bot_manager_or_client, "get_client"):
        return bot_manager_or_client.get_client(bot_code)
    return bot_manager_or_client


async def credit_referral_if_needed(client, config, db, payment):
    bot_code = payment.get("bot_code") or "default"
    referral_id = payment.get("referral_id")
    if not referral_id:
        referral = await db.pending_referral_for_user(payment["user_id"], bot_code=bot_code)
        referral_id = referral.get("id") if referral else None
    if not referral_id:
        return
    commission = referral_commission(payment)
    if commission <= 0:
        return
    referral = await db.mark_referral_paid(referral_id, payment, commission)
    if not referral:
        return
    await safe_send_user(
        client,
        config,
        db,
        referral["referrer_user_id"],
        (
            f"✅ <b>[{html.escape(bot_code)}] Komisi referral masuk</b>\n\n"
            f"{html.escape(payment.get('full_name') or str(payment['user_id']))} sudah join member VIP.\n"
            f"Komisi kamu: <b>{format_rupiah(commission)}</b>"
        ),
        parse_mode="html",
    )
    referrer_row = (await db.get_user(referral["referrer_user_id"], bot_code=bot_code)) or {"user_id": referral["referrer_user_id"]}
    payment_row = {
        "user_id": payment["user_id"],
        "username": payment.get("username") or "",
        "full_name": payment.get("full_name") or "",
    }
    await send_log(
        client,
        config,
        db,
        (
            f"<b>[{html.escape(bot_code)}] REFERRAL COMMISSION CREDITED</b>\n\n"
            "<blockquote>"
            "<b>INVITER</b>\n"
            f"<b>Name</b>: {html.escape(referrer_row.get('full_name') or str(referrer_row.get('user_id')))}\n"
            f"<b>Username</b>: {('@' + html.escape(referrer_row.get('username'))) if referrer_row.get('username') else '-'}\n"
            f"<b>User ID</b>: <code>{referrer_row.get('user_id')}</code>"
            "</blockquote>\n\n"
            "<blockquote>"
            "<b>USER INVITED</b>\n"
            f"<b>Name</b>: {html.escape(payment.get('full_name') or str(payment['user_id']))}\n"
            f"<b>User ID</b>: <code>{payment['user_id']}</code>"
            "</blockquote>\n\n"
            "<blockquote>"
            "<b>TRANSACTION</b>\n"
            f"<b>Bot</b>: <code>{html.escape(bot_code)}</code>\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Package</b>: {html.escape(payment.get('package_code') or '')} {html.escape(payment.get('package_name') or '')}\n"
            f"<b>Package Amount</b>: {format_rupiah(int(payment.get('package_amount') or 0))}\n"
            f"<b>Commission</b>: {format_rupiah(commission)}\n\n"
            "Status: Success"
            "</blockquote>\n\n"
            f"User {plain_user_link(payment_row)} joined using Referral User {plain_user_link(referrer_row)}\n\n"
            f"Referral Code: {html.escape(referrer_row.get('referral_code') or '')}"
        ),
    )


async def process_paid_payment(client, config, db, payment):
    bot_code = payment.get("bot_code") or "default"
    if payment["status"] == "delivery_error":
        invite_link = payment.get("invite_link") or ""
        invite_expires_at = payment.get("invite_expires_at") or ""
        if not invite_link:
            await db.mark_delivery_error(payment["inv_id"], "Missing invite_link for delivery retry")
            await send_log(
                client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] Delivery retry error</b>\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    "Error: <code>Missing invite_link for delivery retry</code>"
                ),
            )
            return
    else:
        if not (await db.claim_paid_processing(payment["inv_id"])):
            return
        try:
            invite_link, invite_expires_at = await create_invite_link(client, config, db, payment)
        except Exception as exc:
            LOGGER.exception("Failed to create invite link for %s", payment["inv_id"])
            await db.mark_invite_error(payment["inv_id"], str(exc))
            await send_log(
                client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] Invite creation error</b>\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        if not (await db.mark_delivery_processing(payment["inv_id"], invite_link, invite_expires_at)):
            return

    await delete_qris_message(client, payment)
    delivery_status = await safe_send_user(
        client,
        config,
        db,
        payment["user_id"],
        paid_message(
            invite_link,
            payment.get("package_name") or "VIP",
            int(payment.get("invite_expire_hours") or 0) or config.invite_expire_hours,
            internal_telegram_chat_url(payment.get("vip_chat_id")),
        ),
        parse_mode="html",
        link_preview=False,
    )
    if delivery_status != "sent":
        if delivery_status == "blocked":
            await db.mark_delivery_blocked(payment["inv_id"], "User blocked the bot")
            await send_log(
                client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] Invite delivery blocked</b>\n"
                    f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    "Status: <code>User blocked the bot, delivery will not be retried</code>"
                ),
            )
        else:
            await db.mark_delivery_error(payment["inv_id"], "Failed to send invite link to user")
        return

    await db.mark_delivery_done(payment["inv_id"])
    await credit_referral_if_needed(client, config, db, payment)

    await send_log(
        client,
        config,
        db,
        (
            f"<b>[{html.escape(bot_code)}] PAYMENT PAID</b>\n\n"
            "<blockquote>"
            f"<b>Bot</b>: <code>{html.escape(bot_code)}</code>\n"
            f"<b>User</b>: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"<b>Package</b>: {html.escape(payment.get('package_code') or '')} {html.escape(payment.get('package_name') or '')}\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Internal Invoice</b>: <code>{html.escape(payment['inv_id'])}</code>\n"
            f"<b>Invite Link</b>: {html.escape(invite_link)}\n"
            f"<b>Invite Link Expires</b>: {format_log_datetime_wrapper(invite_expires_at)}"
            "</blockquote>"
        ),
    )


def format_log_datetime_wrapper(raw):
    from vip_bot.helpers import format_log_datetime
    return format_log_datetime(raw)


async def poll_once(bot_manager_or_client, config, db, payment):
    bot_code = payment.get("bot_code") or "default"
    client = resolve_client(bot_manager_or_client, bot_code)
    try:
        if payment["status"] in {"invite_error", "delivery_error"}:
            await process_paid_payment(client, config, db, payment)
            return

        status, status_url, elapsed_ms = await asyncio.to_thread(check_payment_sync, config, payment["inv_id"])
        LOGGER.info("Invoice %s status=%s latency=%sms", payment["inv_id"], status, elapsed_ms)
        if status == "paid":
            await process_paid_payment(client, config, db, payment)
        elif status in {"failed_or_expired", "unknown"}:
            await db.mark_payment_failed(payment["inv_id"], "failed_or_expired", "Expired or failed on gateway")
            packages = await db.list_packages(bot_code=bot_code)
            await delete_qris_message(client, payment)
            await safe_send_user(
                client,
                config,
                db,
                payment["user_id"],
                invalid_payment_message(),
                parse_mode="html",
                buttons=package_buttons(config, packages, bot_code=bot_code),
            )
            await send_log(
                client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] Payment {html.escape(status)}</b>\n"
                    f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                    f"Package: <code>{html.escape(payment.get('package_code') or '')}</code> {html.escape(payment.get('package_name') or '')}\n"
                    f"Check: {html.escape(status_url)}"
                ),
            )
        else:
            created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
            expires_at = parse_iso_datetime(payment.get("qris_expires"))
            attempts = int(payment.get("poll_attempts") or 0) + 1
            await db.mark_payment_pending(
                payment["inv_id"],
                attempts,
                next_poll_at(created_at, expires_at, attempts=attempts, error="") or utc_now_iso(),
            )
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            marker = "SociaBuzz Cloudflare HTTP 403"
            LOGGER.warning("SociaBuzz Cloudflare challenge while polling %s", payment["inv_id"])
            if payment["status"] == "pending":
                created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
                expires_at = parse_iso_datetime(payment.get("qris_expires"))
                attempts = int(payment.get("poll_attempts") or 0) + 1
                await db.mark_payment_pending(
                    payment["inv_id"],
                    attempts,
                    next_poll_at(created_at, expires_at, attempts=attempts, error=marker) or utc_now_iso(),
                    error=marker,
                )
            return
        LOGGER.exception("Polling failed for %s", payment["inv_id"])
        if payment["status"] == "pending":
            created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
            expires_at = parse_iso_datetime(payment.get("qris_expires"))
            attempts = int(payment.get("poll_attempts") or 0) + 1
            await db.mark_payment_pending(
                payment["inv_id"],
                attempts,
                next_poll_at(created_at, expires_at, attempts=attempts, error=str(exc)) or utc_now_iso(),
                error=str(exc),
            )


async def expire_pending_payment(client, config, db, payment, title="Payment expired"):
    bot_code = payment.get("bot_code") or "default"
    await db.mark_payment_timeout(payment["inv_id"])
    packages = await db.list_packages(bot_code=bot_code)
    await delete_qris_message(client, payment)
    await safe_send_user(
        client,
        config,
        db,
        payment["user_id"],
        timeout_payment_message(),
        parse_mode="html",
        buttons=package_buttons(config, packages, bot_code=bot_code),
    )
    await send_log(
        client,
        config,
        db,
        (
            f"<b>[{html.escape(bot_code)}] {html.escape(title)}</b>\n"
            f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
            f"Package: <code>{html.escape(payment.get('package_code') or '')}</code> {html.escape(payment.get('package_name') or '')}"
        ),
    )


async def polling_loop(bot_manager_or_client, config, db):
    LOGGER.info("Starting Centralized Multi-Bot Payment polling loop (Native Postgres)...")
    while True:
        try:
            await db.recover_stale_processing()
            now = dt.datetime.now(dt.UTC)
            payments = await db.retryable_payments(now.isoformat(), config.poll_batch_size)
            for payment in payments:
                expires_at = parse_iso_datetime(payment.get("qris_expires"))
                bot_code = payment.get("bot_code") or "default"
                client = resolve_client(bot_manager_or_client, bot_code)
                if expires_at and now >= expires_at and payment["status"] == "pending":
                    await expire_pending_payment(client, config, db, payment)
                    continue
                if int(payment.get("poll_attempts") or 0) >= config.poll_max_attempts and payment["status"] == "pending":
                    await expire_pending_payment(client, config, db, payment, title="Payment polling limit reached")
                    continue
                await poll_once(bot_manager_or_client, config, db, payment)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            LOGGER.exception("Error in payment polling loop: %s", exc)
        await asyncio.sleep(config.poll_interval_seconds)


async def send_broadcast_batch(client, db, broadcast_message, target_ids, concurrency=5, bot_code="default"):
    semaphore = asyncio.Semaphore(concurrency)
    totals = {"sent": 0, "blocked": 0, "deactivated": 0, "error": 0}

    async def _send(uid):
        async with semaphore:
            status = await send_broadcast_to_user(client, db, broadcast_message, uid)
            if status in totals:
                totals[status] += 1
            else:
                totals["error"] += 1
            if status == "sent":
                try:
                    await db.mark_user_broadcasted(uid, bot_code=bot_code)
                except Exception:
                    pass

    await asyncio.gather(*[_send(uid) for uid in target_ids])
    return totals


async def broadcast_loop(bot_manager_or_client, config, db):
    LOGGER.info("Starting Multi-Bot Broadcast loop (Native Postgres)...")
    while True:
        try:
            now_wib = dt.datetime.now(WIB)
            today_str = now_wib.strftime("%Y-%m-%d")
            current_time = now_wib.strftime("%H:%M")

            # Collect all bots to evaluate
            known_bot_codes = {"default"}
            if hasattr(bot_manager_or_client, "active_bots"):
                known_bot_codes.update(bot_manager_or_client.active_bots.keys())
            try:
                db_bots = await db.list_active_bots()
                for b in db_bots:
                    known_bot_codes.add(b["bot_code"])
            except Exception:
                pass

            for b_code in sorted(known_bot_codes):
                try:
                    b_time = await db.get_broadcast_time(bot_code=b_code)
                    if not b_time or b_time in BROADCAST_DISABLED_VALUES:
                        continue
                    last_date = await db.get_last_broadcast_date(bot_code=b_code)
                    if current_time == b_time and last_date != today_str:
                        msg = await db.get_active_broadcast_message(bot_code=b_code)
                        if not msg:
                            LOGGER.info("Broadcast time reached for [%s] (%s WIB) but no active message", b_code, b_time)
                            continue

                        client = resolve_client(bot_manager_or_client, b_code)
                        targets = [t["user_id"] for t in (await db.get_broadcast_targets(bot_code=b_code, limit=1000))]
                        if targets:
                            LOGGER.info("Dispatching broadcast for bot [%s] to %d users...", b_code, len(targets))
                            totals = await send_broadcast_batch(
                                client, db, msg, targets, config.qris_create_concurrency, bot_code=b_code
                            )
                            LOGGER.info("Broadcast for bot [%s] completed: %s", b_code, totals)
                            await send_log(
                                client,
                                config,
                                db,
                                (
                                    f"📢 <b>Broadcast Harian Selesai [{html.escape(b_code)}]</b>\n"
                                    f"• Waktu: <code>{html.escape(b_time)} WIB</code>\n"
                                    f"• Terkirim: <b>{totals.get('sent', 0)}</b>\n"
                                    f"• Diblokir: <b>{totals.get('blocked', 0)}</b>\n"
                                    f"• Akun Dihapus: <b>{totals.get('deactivated', 0)}</b>\n"
                                    f"• Gagal: <b>{totals.get('error', 0)}</b>"
                                ),
                            )
                        await db.set_last_broadcast_date(today_str, bot_code=b_code)
                except Exception as sub_exc:
                    LOGGER.exception("Error processing broadcast for bot [%s]: %s", b_code, sub_exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            LOGGER.exception("Error in broadcast loop: %s", exc)
        await asyncio.sleep(20)
