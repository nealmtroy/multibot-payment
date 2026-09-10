import asyncio
import io
import html
import logging
from telethon import events, Button, errors
from vip_bot.helpers import (
    create_qris_with_retries_sync,
    public_invoice_id,
    send_log,
    telegram_user_link,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    format_button_amount,
    format_rupiah,
    parse_referral_payload,
    format_referral_code,
    display_name,
    safe_send_user,
    valid_withdrawal_amount,
    parse_withdrawal_amount,
    withdrawal_details_text,
    normalize_package_code,
)
from vip_bot.messages import (
    qris_caption,
    default_package,
    package_buttons,
    main_menu_keyboard_text,
    main_menu_buttons,
    main_menu_button_labels,
)
from sociabuzz_client import SociaBuzzError

LOGGER = logging.getLogger("telegram_vip_bot.handlers.user")


def private_only(handler):
    async def wrapped(event):
        if not event.is_private:
            return
        await handler(event)
    return wrapped


async def send_qris(event, config, db, qris_semaphore, user_locks, package=None, invoice_message=None, bot_code="default"):
    user = await event.get_sender()
    lock = user_locks.setdefault((user.id, bot_code), asyncio.Lock())
    if lock.locked():
        await event.respond("QRIS kamu sedang dibuat. Tunggu beberapa detik, jangan klik berulang.")
        return
    async with lock:
        await send_qris_locked(event, config, db, qris_semaphore, user, package, invoice_message, bot_code=bot_code)


async def send_qris_locked(event, config, db, qris_semaphore, user, package=None, invoice_message=None, bot_code="default"):
    await db.upsert_user(user, bot_code=bot_code)
    package = package or default_package(config, bot_code=bot_code)
    pending = await db.latest_pending_for_user(user.id, bot_code=bot_code)
    if pending:
        await event.respond(
            "Masih ada pembayaran yang sedang dicek. Tunggu statusnya selesai dulu sebelum membuat QRIS baru."
        )
        return

    if invoice_message is None:
        invoice_message = await event.respond("⏳ Membuat QRIS...")
    else:
        try:
            await event.client.edit_message(event.chat_id, invoice_message.id, "⏳ Membuat QRIS...")
        except errors.MessageNotModifiedError:
            pass

    try:
        async with qris_semaphore:
            (
                _session,
                buyer_name,
                buyer_email,
                order_id,
                payment_url,
                qris,
                qr_bytes,
                checkout_amount,
            ) = await asyncio.to_thread(create_qris_with_retries_sync, config, user, int(package["amount"]), package["code"].upper())
        socia_invoice_id = qris.get("inv_id")
        if not socia_invoice_id:
            raise SociaBuzzError(f"QRIS response missing inv_id: {qris}")

        buyer_invoice_id = public_invoice_id()
        qr_file = io.BytesIO(qr_bytes)
        qr_file.name = f"{buyer_invoice_id}.png"
        payload = qris.get("data", {})
        invoice_message = await event.client.edit_message(
            event.chat_id,
            invoice_message.id,
            qris_caption(
                package,
                buyer_invoice_id,
                checkout_amount,
                payload.get("amount") or "",
                payload.get("countdown") or "",
            ),
            file=qr_file,
            parse_mode="html",
        )
        referral = await db.pending_referral_for_user(user.id, bot_code=bot_code)
        await db.create_payment(
            user,
            buyer_invoice_id,
            order_id,
            payment_url,
            socia_invoice_id,
            checkout_amount,
            buyer_name,
            buyer_email,
            qris,
            event.chat_id,
            invoice_message.id,
            package=package,
            referral=referral,
            bot_code=bot_code,
        )
        await send_log(
            event.client,
            config,
            db,
            (
                f"<b>[{html.escape(bot_code)}] QRIS CREATED</b>\n\n"
                "<blockquote>"
                f"<b>Bot</b>: <code>{html.escape(bot_code)}</code>\n"
                f"<b>User</b>: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                f"<b>Package</b>: {html.escape(package.get('code') or '')} {html.escape(package.get('name') or '')} (<code>{package.get('vip_chat_id') or ''}</code>)\n"
                f"<b>Package Amount</b>: {format_button_amount(checkout_amount)}\n"
                f"<b>QRIS Amount</b>: {html.escape(payload.get('amount') or '')}\n"
                f"<b>Invoice</b>: <code>{html.escape(buyer_invoice_id)}</code>\n"
                f"<b>Internal Invoice</b>: <code>{html.escape(socia_invoice_id)}</code>\n"
                f"<b>Source Payment</b>: {html.escape(qris.get('source_payment') or '')}\n"
                f"<b>Order ID</b>: <code>{html.escape(order_id)}</code>"
                "</blockquote>"
            ),
        )
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            LOGGER.warning("SociaBuzz Cloudflare challenge while creating QRIS for user %s", user.id)
        elif is_sociabuzz_timeout(exc):
            LOGGER.warning("SociaBuzz timed out while creating QRIS for user %s after retries: %s", user.id, exc)
        else:
            LOGGER.exception("Failed to create QRIS")
        try:
            await invoice_message.delete()
        except Exception:
            LOGGER.warning("Failed to delete invoice message after create error", exc_info=True)
        if is_cloudflare_challenge(exc):
            await event.respond("QRIS belum bisa dibuat karena sistem sedang membatasi request. Coba lagi beberapa menit lagi.")
            await send_log(
                event.client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] QRIS gateway blocked</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    "Reason: <code>SociaBuzz Cloudflare HTTP 403</code>"
                ),
            )
            return
        if is_sociabuzz_timeout(exc):
            await event.respond("QRIS lagi lambat dibuat. Coba lagi sebentar lagi ya.")
            await send_log(
                event.client,
                config,
                db,
                (
                    f"<b>[{html.escape(bot_code)}] QRIS gateway timeout</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    f"Package: <code>{html.escape(package.get('code') or '')}</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        await event.respond("Gagal membuat QRIS. Coba lagi beberapa saat lagi.")
        await send_log(
            event.client,
            config,
            db,
            f"<b>[{html.escape(bot_code)}] QRIS error</b>\n<code>{html.escape(str(exc))}</code>",
        )


async def send_package_menu(event, config, db, message=None, bot_code="default"):
    packages = await db.list_packages(bot_code=bot_code)
    buttons = package_buttons(config, packages, bot_code=bot_code)
    text = "Silakan pilih paket VIP yang ingin kamu beli:"
    if message is None:
        await event.respond(text, buttons=buttons)
    else:
        try:
            await event.client.edit_message(event.chat_id, message.id, text, buttons=buttons)
        except errors.MessageNotModifiedError:
            pass


async def handle_referral_start(event, config, db, payload, bot_code="default"):
    code = parse_referral_payload(payload)
    if not code:
        return
    try:
        user = await event.get_sender()
        referrer = await db.get_user_by_referral_code(code, bot_code=bot_code)
        if not referrer:
            return
        referral, created = await db.create_referral_if_absent(referrer, user, bot_code=bot_code)
        if not created or not referral:
            return
        await safe_send_user(
            event.client,
            config,
            db,
            referrer["user_id"],
            f"✅ {html.escape(display_name(user))} berhasil diundang menggunakan referral link kamu.",
            parse_mode="html",
        )
        invited_row = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
        }
        from vip_bot.helpers import plain_user_link
        await send_log(
            event.client,
            config,
            db,
            (
                f"<b>[{html.escape(bot_code)}] Referral Joined</b>\n"
                f"User {plain_user_link(invited_row)} joined using Referral User {plain_user_link(referrer)}\n\n"
                f"Referral Code: {html.escape(code)}"
            ),
        )
    except Exception as exc:
        LOGGER.exception("Failed to process referral start")
        await send_log(
            event.client,
            config,
            db,
            f"<b>[{html.escape(bot_code)}] Referral start error</b>\n<code>{html.escape(str(exc))}</code>",
        )


async def send_profile(event, config, db, bot_code="default"):
    try:
        user = await event.get_sender()
        await db.upsert_user(user, bot_code=bot_code)
        stats = await db.referral_stats(user.id, bot_code=bot_code)
        me = await event.client.get_me()
        code = stats["referral_code"]
        link = f"https://t.me/{me.username}?start=ref_{code}" if me.username else f"ref_{code}"
        detail_lines = [
            f"<b>Bot</b>: <code>{html.escape(bot_code)}</code>",
            f"<b>User ID</b>: <code>{user.id}</code>",
        ]
        if user.username:
            detail_lines.append(f"<b>Username</b>: @{html.escape(user.username)}")
        detail_lines.extend(
            [
                f"<b>Saldo Komisi</b>: <b>{format_rupiah(stats['balance'])}</b>",
                f"<b>Referral Link</b>: {html.escape(link)}",
                f"<b>Referral Berhasil</b>: <b>{stats['successful_count']}</b>",
                f"<b>Pending Referral</b>: <b>{stats['pending_count']}</b>",
            ]
        )
        detail_lines_str = "\n".join(detail_lines)
        lines = [
            "<b>Profile & Referral</b>",
            "Dapatkan komisi sebesar <b>50%</b> dari setiap pembelian paket VIP melalui referral link kamu khusus bot ini.",
            "",
            f"<blockquote>{detail_lines_str}</blockquote>",
        ]
        await event.respond("\n".join(lines), parse_mode="html", buttons=main_menu_buttons())
    except Exception as exc:
        LOGGER.exception("Failed to show profile")
        await event.respond("Profile belum bisa ditampilkan. Coba lagi beberapa saat lagi.", buttons=main_menu_buttons())
        await send_log(
            event.client,
            config,
            db,
            f"<b>[{html.escape(bot_code)}] Profile error</b>\nUser: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>",
        )


async def send_withdrawal_menu(event, config, db, bot_code="default"):
    try:
        user = await event.get_sender()
        await db.upsert_user(user, bot_code=bot_code)
        stats = await db.referral_stats(event.sender_id, bot_code=bot_code)
        await event.respond(
            f"Saldo kamu ({html.escape(bot_code)}): <b>{format_rupiah(stats['balance'])}</b>\n\nKlik tombol di bawah untuk tarik saldo.",
            parse_mode="html",
            buttons=[[Button.inline("Tarik Saldo", b"withdraw_start")]],
        )
    except Exception as exc:
        LOGGER.exception("Failed to show withdrawal menu")
        await event.respond("Menu tarik saldo belum bisa ditampilkan. Coba lagi beberapa saat lagi.")
        await send_log(
            event.client,
            config,
            db,
            f"<b>[{html.escape(bot_code)}] Withdrawal menu error</b>\nUser: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>",
        )


async def create_withdrawal_request(event, config, db, user, amount, details, bot_code="default"):
    stats = await db.referral_stats(user.id, bot_code=bot_code)
    if not valid_withdrawal_amount(amount, stats["balance"]):
        await event.respond("Minimal penarikan saldo adalah Rp10.000 dan saldo kamu harus mencukupi.")
        return
    try:
        withdrawal = await db.create_withdrawal(user, amount, details, bot_code=bot_code)
    except Exception as exc:
        await event.respond(f"Gagal mengajukan penarikan: {html.escape(str(exc))}")
        return

    await send_log(
        event.client,
        config,
        db,
        (
            f"<b>[{html.escape(bot_code)}] Withdrawal requested</b>\n"
            f"ID: <code>{withdrawal.get('id')}</code>\n"
            f"Bot: <code>{html.escape(bot_code)}</code>\n"
            f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
            f"Amount: <code>{amount}</code>\n"
            f"No Hp: <code>{html.escape(details['phone'])}</code>\n"
            f"Nama E-Wallet: <code>{html.escape(details['wallet_name'])}</code>\n"
            f"Atas Nama: <code>{html.escape(details['account_name'])}</code>"
        ),
        buttons=[
            [
                Button.inline("Berhasil", f"withdraw_done:{withdrawal.get('id')}".encode()),
                Button.inline("Tolak", f"withdraw_reject:{withdrawal.get('id')}".encode()),
            ]
        ],
    )
    await event.respond(
        "Pengajuan penarikan saldo berhasil. Mohon tunggu 1x24 jam untuk diproses oleh admin.",
        buttons=main_menu_buttons(),
    )


def register_user_handlers(client, config, db, qris_semaphore, user_locks, withdrawal_states, bot_code="default"):
    buy_label, profile_label, withdrawal_label = main_menu_button_labels()

    @client.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
    @private_only
    async def start(event):
        user = await event.get_sender()
        await db.upsert_user(user, bot_code=bot_code)
        await handle_referral_start(event, config, db, event.pattern_match.group(1) or "", bot_code=bot_code)
        bot_name = getattr(client, "bot_name", "") or bot_code
        await event.respond(main_menu_keyboard_text(user, bot_name=bot_name), buttons=main_menu_buttons(), parse_mode="html")
        await send_package_menu(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(pattern=r"^/buy$"))
    @private_only
    async def buy_command(event):
        await send_package_menu(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(func=lambda e: bool(e.is_private and e.raw_text and e.raw_text.strip() == buy_label)))
    @private_only
    async def buy_button(event):
        await send_package_menu(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(pattern=r"^/profile$"))
    @private_only
    async def profile_command(event):
        await send_profile(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(func=lambda e: bool(e.is_private and e.raw_text and e.raw_text.strip() == profile_label)))
    @private_only
    async def profile_button(event):
        await send_profile(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(pattern=r"^/withdraw$"))
    @private_only
    async def withdrawal_command(event):
        await send_withdrawal_menu(event, config, db, bot_code=bot_code)

    @client.on(events.NewMessage(func=lambda e: bool(e.is_private and e.raw_text and e.raw_text.strip() == withdrawal_label)))
    @private_only
    async def withdrawal_button(event):
        await send_withdrawal_menu(event, config, db, bot_code=bot_code)

    @client.on(events.CallbackQuery(data=b"withdraw_start"))
    async def start_withdrawal_flow(event):
        state_key = (event.sender_id, bot_code)
        withdrawal_states[state_key] = {"step": "amount"}
        await event.answer()
        await event.respond(
            "Masukkan nominal penarikan saldo (contoh: <code>50000</code>). Minimal Rp10.000.",
            parse_mode="html",
        )

    @client.on(events.CallbackQuery(pattern=rb"^pkg:(.+)$"))
    async def choose_package(event):
        code = event.pattern_match.group(1).decode()
        package = await db.get_package(code, bot_code=bot_code)
        if not package:
            package = default_package(config, bot_code=bot_code)
        await event.answer()
        message = await event.get_message()
        await send_qris(
            event,
            config,
            db,
            qris_semaphore,
            user_locks,
            package=package,
            invoice_message=message,
            bot_code=bot_code,
        )

    @client.on(events.NewMessage(func=lambda e: bool(e.is_private and (e.sender_id, bot_code) in withdrawal_states and not (e.raw_text or "").startswith("/"))))
    @private_only
    async def withdrawal_step_message(event):
        state_key = (event.sender_id, bot_code)
        state = withdrawal_states.get(state_key)
        if not state:
            return
        user = await event.get_sender()
        text = (event.raw_text or "").strip()
        step = state.get("step")

        if step == "amount":
            amount = parse_withdrawal_amount(text)
            if not amount:
                await event.respond("Nominal harus berupa angka bulat minimal 10000. Masukkan kembali nominal:")
                return
            stats = await db.referral_stats(user.id, bot_code=bot_code)
            if not valid_withdrawal_amount(amount, stats["balance"]):
                await event.respond(
                    f"Saldo tidak cukup atau kurang dari batas minimal. Saldo kamu: <b>{format_rupiah(stats['balance'])}</b>. Masukkan kembali nominal:",
                    parse_mode="html",
                )
                return
            state["amount"] = amount
            state["step"] = "phone"
            await event.respond("Masukkan nomor HP yang terdaftar di E-Wallet:")
            return

        if step == "phone":
            if not text:
                await event.respond("Nomor HP tidak boleh kosong:")
                return
            state["phone"] = text
            state["step"] = "wallet_name"
            await event.respond("Masukkan nama E-Wallet (contoh: DANA, OVO, GoPay):")
            return

        if step == "wallet_name":
            if not text:
                await event.respond("Nama E-Wallet tidak boleh kosong:")
                return
            state["wallet_name"] = text
            state["step"] = "account_name"
            await event.respond("Masukkan nama pemilik rekening / akun E-Wallet:")
            return

        if step == "account_name":
            if not text:
                await event.respond("Nama pemilik akun tidak boleh kosong:")
                return
            state["account_name"] = text
            state["step"] = "confirm"
            confirmation_text = withdrawal_details_text(
                state["amount"],
                state["phone"],
                state["wallet_name"],
                state["account_name"],
            )
            buttons = [
                [
                    Button.inline("Konfirmasi", b"withdraw_confirm"),
                    Button.inline("Batal", b"withdraw_cancel"),
                ]
            ]
            await event.respond(confirmation_text, parse_mode="html", buttons=buttons)
            return

    @client.on(events.CallbackQuery(data=b"withdraw_confirm"))
    async def confirm_withdrawal(event):
        state_key = (event.sender_id, bot_code)
        state = withdrawal_states.get(state_key)
        if not state or state.get("step") != "confirm":
            await event.answer("Sesi penarikan sudah kedaluwarsa.", alert=True)
            return
        user = await event.get_sender()
        amount = state["amount"]
        details = {
            "phone": state["phone"],
            "wallet_name": state["wallet_name"],
            "account_name": state["account_name"],
        }
        withdrawal_states.pop(state_key, None)
        await event.answer()
        await create_withdrawal_request(event, config, db, user, amount, details, bot_code=bot_code)

    @client.on(events.CallbackQuery(data=b"withdraw_cancel"))
    async def cancel_withdrawal(event):
        state_key = (event.sender_id, bot_code)
        withdrawal_states.pop(state_key, None)
        await event.answer("Penarikan saldo dibatalkan.")
        await event.respond("Penarikan saldo dibatalkan.", buttons=main_menu_buttons())
