from pathlib import Path
import asyncio
import io
import html
import logging
import re
from telethon import events, Button, errors, types
from vip_bot.config import BROADCAST_DISABLED_VALUES, BROADCAST_TIME_PATTERN
from vip_bot.helpers import (
    is_admin,
    runtime_vip_chat_id,
    runtime_log_chat_id,
    send_log,
    parse_chat_setting,
    format_rupiah,
    create_qris_with_retries_sync,
    public_invoice_id,
    telegram_user_link,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    normalize_package_code,
    format_button_amount,
    format_log_datetime,
    safe_send_user,
    entities_to_json,
    validate_broadcast_time,
)
from vip_bot.messages import (
    admin_command_list_text,
    custom_qris_caption,
    package_list_text,
    bot_list_text,
    admin_main_menu_keyboard,
    admin_bot_menu_keyboard,
    admin_package_menu_keyboard,
    admin_broadcast_menu_keyboard,
    cancel_keyboard,
)
from vip_bot.loops import send_broadcast_batch
from sociabuzz_client import SociaBuzzError

LOGGER = logging.getLogger("telegram_vip_bot.handlers.admin")


async def require_admin(event, config, db):
    return is_admin(config, event.sender_id)


async def require_log_chat(event, config, db):
    return True


async def require_admin_logchat(event, config, db):
    return await require_admin(event, config, db)


def parse_package_add_args(raw):
    raw = (raw or "").strip()
    if not raw or "|" not in raw:
        raise ValueError("Format: /package_add <nama_bot> <kode> <Nama Group>|<chat_id>|<harga>")

    pipe_parts = [part.strip() for part in raw.split("|")]
    if len(pipe_parts) != 3:
        raise ValueError("Format: /package_add <nama_bot> <kode> <Nama Group>|<chat_id>|<harga>")

    first_part, chat_id, amount = pipe_parts
    words = first_part.split()
    if len(words) < 3:
        raise ValueError("Format: /package_add <nama_bot> <kode> <Nama Group>|<chat_id>|<harga>")

    bot_code = words[0].lower()
    code = words[1]
    name = first_part.split(None, 2)[2].strip()

    if not name:
        raise ValueError("Nama paket wajib diisi.")
    digits = "".join(ch for ch in amount if ch.isdigit())
    if not digits:
        raise ValueError("Nominal paket harus angka.")
    amount_value = int(digits)
    chat_id_value = int(chat_id)
    return bot_code, code, name, chat_id_value, amount_value


def register_admin_handlers(client, config, db, qris_semaphore, user_locks, bot_manager=None):
    # In-memory multi-step wizard state per admin: {sender_id: {"action": "...", "step": "...", "data": {...}}}
    admin_states = {}

    # -------------------------------------------------------------------------
    # Helper: Send System Dashboard
    # -------------------------------------------------------------------------
    async def send_dashboard(event):
        bots = await bot_manager.list_all() if bot_manager else []
        active_count = sum(1 for b in bots if b["status"] == "online")
        pending_withdrawals = len(await db.list_pending_withdrawals())
        all_packages = await db.list_all_packages()

        text = (
            f"👋 <b>Halo Administrator!</b>\n"
            f"Selamat datang di <b>Master Management Bot</b>.\n\n"
            f"📊 <b>Ringkasan Sistem:</b>\n"
            f"• Status Server: 🟢 <b>Online</b>\n"
            f"• Bot Payment Aktif: <b>{active_count}/{len(bots)} bot</b>\n"
            f"• Total Paket VIP: <b>{len(all_packages)} paket</b>\n"
            f"• Antrean Tarik Saldo: <b>{pending_withdrawals} pending</b>\n\n"
            f"Pilih menu di bawah keyboard untuk mengelola sistem secara interaktif."
        )
        await event.respond(text, parse_mode="html", buttons=admin_main_menu_keyboard())

    # -------------------------------------------------------------------------
    # /start or /menu: Welcome & ReplyKeyboardMarkup
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/(?:start|menu|commands?|help)(?:@\w+)?(?:\s+.*)?$"))
    async def admin_start_handler(event):
        if not is_admin(config, event.sender_id):
            return
        admin_states.pop(event.sender_id, None)
        await send_dashboard(event)

    # -------------------------------------------------------------------------
    # /custom <nominal>: Admin Custom QRIS in LOG_CHAT_ID
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/custom(?:@\w+)?(?:\s+(.+))?$"))
    async def admin_custom_qris_handler(event):
        if not is_admin(config, event.sender_id):
            return

        log_id = await runtime_log_chat_id(config, db)
        if not event.is_private and log_id and int(event.chat_id) != int(log_id):
            return

        raw_arg = event.pattern_match.group(1) if event.pattern_match else None
        if not raw_arg or not raw_arg.strip():
            await event.reply(
                "ℹ️ <b>Format Command:</b>\n"
                "<code>/custom &lt;nominal&gt;</code>\n\n"
                "Contoh:\n"
                "• <code>/custom 30000</code>\n"
                "• <code>/custom 50.000</code>",
                parse_mode="html",
            )
            return

        cleaned_digits = re.sub(r"[^\d]", "", raw_arg.strip())
        if not cleaned_digits:
            await event.reply(
                "❌ Nominal tidak valid. Masukkan angka nominal pembayaran.\nContoh: <code>/custom 50.000</code>",
                parse_mode="html",
            )
            return

        checkout_amount = int(cleaned_digits)
        if checkout_amount < 1000 or checkout_amount > 10000000:
            await event.reply(
                "❌ Nominal pembayaran harus antara <b>Rp 1.000</b> hingga <b>Rp 10.000.000</b>.",
                parse_mode="html",
            )
            return

        loading_msg = await event.reply("⏳ <i>Membuat Custom QRIS...</i>", parse_mode="html")
        try:
            user = await event.get_sender()
            if not user:
                user = types.User(id=event.sender_id, first_name="Admin")

            await db.upsert_user(user, bot_code="master")

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
                ) = await asyncio.to_thread(
                    create_qris_with_retries_sync,
                    config,
                    user,
                    checkout_amount,
                    "CUSTOM",
                )

            socia_invoice_id = qris.get("inv_id")
            if not socia_invoice_id:
                raise SociaBuzzError(f"QRIS response missing inv_id: {qris}")

            buyer_invoice_id = public_invoice_id()
            qr_file = io.BytesIO(qr_bytes)
            qr_file.name = f"{buyer_invoice_id}.png"
            payload = qris.get("data", {})

            caption_text = custom_qris_caption(
                inv_id=buyer_invoice_id,
                checkout_amount=checkout_amount,
                final_amount=payload.get("amount") or "",
                expires=payload.get("countdown") or "",
                user=user,
            )

            qris_msg = await event.reply(
                caption_text,
                file=qr_file,
                parse_mode="html",
            )
            try:
                await client.delete_messages(event.chat_id, [loading_msg.id])
            except Exception:
                pass

            await db.create_payment(
                user=user,
                public_invoice_id=buyer_invoice_id,
                order_id=order_id,
                payment_url=payment_url,
                inv_id=socia_invoice_id,
                amount=checkout_amount,
                buyer_name=buyer_name,
                buyer_email=buyer_email,
                qris_data=qris,
                qris_chat_id=event.chat_id,
                qris_message_id=qris_msg.id,
                package={
                    "code": "CUSTOM",
                    "name": "Custom QRIS",
                    "amount": checkout_amount,
                    "vip_chat_id": 0,
                    "invite_expire_hours": 0,
                },
                referral=None,
                bot_code="master",
            )
        except Exception as exc:
            LOGGER.exception("Failed to create custom QRIS: %s", exc)
            err_msg = f"❌ <b>Gagal membuat Custom QRIS:</b>\n<code>{html.escape(str(exc))}</code>"
            try:
                await client.edit_message(event.chat_id, loading_msg.id, err_msg, parse_mode="html")
            except Exception:
                await event.reply(err_msg, parse_mode="html")

    @client.on(events.NewMessage(func=lambda e: is_admin(config, e.sender_id) and e.raw_text in (
        "🤖 Kelola Bot Payment",
        "📦 Kelola Paket VIP",
        "📢 Kelola Broadcast",
        "💰 Antrean Penarikan",
        "📊 Status Sistem",
        "⚙️ Pengaturan",
        "🔙 Menu Utama",
        "❌ Batal",
    )))
    async def admin_main_menu_router(event):
        text = event.raw_text.strip()

        # Handle cancel / back to main
        if text in ("❌ Batal", "🔙 Menu Utama"):
            admin_states.pop(event.sender_id, None)
            await send_dashboard(event)
            return

        admin_states.pop(event.sender_id, None)

        if text == "🤖 Kelola Bot Payment":
            await event.respond(
                "🤖 <b>Menu Kelola Bot Payment</b>\n\n"
                "Pilih aksi di bawah:\n"
                "• <b>➕ Tambah Bot Baru</b>: Hubungkan bot payment baru via Bot Token\n"
                "• <b>📋 Daftar Semua Bot</b>: Cek status online & paket bot\n"
                "• <b>⏹️ Hentikan Bot</b> / <b>▶️ Hidupkan Bot</b>: Kontrol status bot\n"
                "• <b>🗑️ Hapus Bot</b>: Hapus bot dari sistem",
                parse_mode="html",
                buttons=admin_bot_menu_keyboard(),
            )
            return

        if text == "📦 Kelola Paket VIP":
            bots = await bot_manager.list_all() if bot_manager else []
            if not bots:
                await event.respond(
                    "⚠️ <b>Belum Ada Bot Payment Terdaftar</b>\n\n"
                    "Untuk mengelola paket VIP, kamu harus menambahkan dan menghubungkan bot payment terlebih dahulu.\n\n"
                    "👉 Silakan buka menu:\n"
                    "<b>🤖 Kelola Bot Payment</b> ➡️ <b>➕ Tambah Bot Baru</b>",
                    parse_mode="html",
                    buttons=admin_main_menu_keyboard(),
                )
                return

            buttons = [
                [Button.inline(f"🤖 {b['bot_code']}" + (f" (@{b['bot_username']})" if b.get('bot_username') else ""), data=f"adm_pkgbot_menu:{b['bot_code']}")]
                for b in bots
            ]
            await event.respond(
                "📦 <b>Menu Kelola Paket VIP</b>\n\n"
                "Silakan pilih bot payment yang ingin kamu kelola paket VIP-nya:",
                parse_mode="html",
                buttons=buttons,
            )
            return

        if text == "📢 Kelola Broadcast":
            await event.respond(
                "📢 <b>Menu Kelola Broadcast Harian (Per Bot)</b>\n\n"
                "Pilih aksi di bawah:\n"
                "• <b>📝 Set Pesan Broadcast</b>: Simpan pesan promosi per bot\n"
                "• <b>⏰ Set Jadwal Broadcast</b>: Atur jam kirim otomatis (WIB)\n"
                "• <b>🧪 Test Broadcast</b>: Coba kirim pesan ke akun admin\n"
                "• <b>📊 Status Broadcast</b>: Cek konfigurasi & target user",
                parse_mode="html",
                buttons=admin_broadcast_menu_keyboard(),
            )
            return

        if text == "💰 Antrean Penarikan":
            pending = await db.list_pending_withdrawals(limit=30)
            if not pending:
                await event.respond(
                    "✅ <b>Tidak ada antrean penarikan saat ini.</b>\n"
                    "Semua pengajuan penarikan komisi referral sudah diproses.",
                    parse_mode="html",
                    buttons=admin_main_menu_keyboard(),
                )
                return

            await event.respond(
                f"💰 <b>Ada {len(pending)} Pengajuan Penarikan Pending:</b>",
                parse_mode="html",
            )
            for w in pending:
                card = (
                    f"💳 <b>Penarikan #{w['id']}</b> [{html.escape(w.get('bot_code', 'default'))}]\n"
                    f"• Pemohon: <b>{html.escape(w.get('full_name') or str(w['user_id']))}</b> (<code>{w['user_id']}</code>)\n"
                    f"• Nominal: <b>{format_rupiah(w['amount'])}</b>\n"
                    f"• No HP: <code>{html.escape(w.get('phone', ''))}</code>\n"
                    f"• E-Wallet: <b>{html.escape(w.get('wallet_name', ''))}</b>\n"
                    f"• Atas Nama: <b>{html.escape(w.get('account_name', ''))}</b>\n"
                    f"• Waktu: <code>{format_log_datetime(w.get('created_at'))}</code>"
                )
                action_buttons = [
                    [
                        Button.inline("✅ Setujui (Approve)", data=f"adm_appr:{w['id']}"),
                        Button.inline("❌ Tolak (Reject)", data=f"adm_rejc:{w['id']}"),
                    ]
                ]
                await event.respond(card, parse_mode="html", buttons=action_buttons)
            return

        if text == "📊 Status Sistem":
            bots = await bot_manager.list_all() if bot_manager else []
            active_count = sum(1 for b in bots if b["status"] == "online")
            packages = await db.list_all_packages()
            pending = await db.list_pending_withdrawals()
            vip_id = await runtime_vip_chat_id(config, db)
            log_id = await runtime_log_chat_id(config, db)

            status_text = (
                f"📊 <b>STATUS SISTEM MULTIBOT PAYMENT</b>\n\n"
                f"• Master Bot: @{client.bot_username}\n"
                f"• Database: <b>PostgreSQL (Active)</b>\n"
                f"• Bot Payment Aktif: <b>{active_count}/{len(bots)}</b>\n"
                f"• Total Paket VIP: <b>{len(packages)}</b>\n"
                f"• Pending Penarikan: <b>{len(pending)}</b>\n\n"
                f"⚙️ <b>Konfigurasi Runtime:</b>\n"
                f"• LOG_CHAT_ID: <code>{log_id or '-'}</code>\n"
                f"• VIP_CHAT_ID (Fallback): <code>{vip_id or '-'}</code>\n"
                f"• Gateway: SociaBuzz (<code>{config.sociabuzz_username}</code>)"
            )
            await event.respond(status_text, parse_mode="html", buttons=admin_main_menu_keyboard())
            return

        if text == "⚙️ Pengaturan":
            log_id = await runtime_log_chat_id(config, db)
            vip_id = await runtime_vip_chat_id(config, db)
            settings_text = (
                f"⚙️ <b>PENGATURAN RUNTIME</b>\n\n"
                f"• <b>LOG_CHAT_ID</b>: <code>{log_id or '-'}</code>\n"
                f"• <b>VIP_CHAT_ID</b>: <code>{vip_id or '-'}</code>\n\n"
                f"Klik tombol di bawah untuk memperbarui setting:"
            )
            buttons = [
                [Button.inline("📝 Ubah LOG_CHAT_ID", b"adm_cfg:log_chat_id")],
                [Button.inline("📝 Ubah VIP_CHAT_ID", b"adm_cfg:vip_chat_id")],
            ]
            await event.respond(settings_text, parse_mode="html", buttons=buttons)
            return

    # -------------------------------------------------------------------------
    # Submenu Buttons Routing (Kelola Bot Payment)
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(func=lambda e: is_admin(config, e.sender_id) and e.raw_text in (
        "➕ Tambah Bot Baru",
        "📋 Daftar Semua Bot",
        "⏹️ Hentikan Bot",
        "▶️ Hidupkan Bot",
        "🗑️ Hapus Bot",
    )))
    async def admin_bot_actions(event):
        text = event.raw_text.strip()

        if text == "📋 Daftar Semua Bot":
            if not bot_manager:
                await event.respond("Bot Manager belum aktif.")
                return
            bots = await bot_manager.list_all()
            await event.respond(bot_list_text(bots), parse_mode="html")
            return

        if text == "➕ Tambah Bot Baru":
            admin_states[event.sender_id] = {
                "action": "add_bot",
                "step": "code",
                "data": {},
            }
            await event.respond(
                "🤖 <b>Tambah Bot Payment Baru (Langkah 1/2)</b>\n\n"
                "Ketik <b>nama/kode bot</b> yang unik (hanya huruf kecil, angka, dan underscore).\n"
                "Contoh: <code>botpayment1</code>\n\n"
                "<i>Klik tombol <b>❌ Batal</b> di bawah jika ingin membatalkan.</i>",
                parse_mode="html",
                buttons=cancel_keyboard(),
            )
            return

        if text == "⏹️ Hentikan Bot":
            bots = await bot_manager.list_all() if bot_manager else []
            online_bots = [b for b in bots if b["status"] == "online"]
            if not online_bots:
                await event.respond("Tidak ada bot payment yang sedang online.")
                return
            buttons = [
                [Button.inline(f"⏹️ Hentikan {b['bot_code']}", data=f"adm_stop:{b['bot_code']}")]
                for b in online_bots
            ]
            await event.respond("Pilih bot yang ingin dihentikan:", buttons=buttons)
            return

        if text == "▶️ Hidupkan Bot":
            bots = await bot_manager.list_all() if bot_manager else []
            stopped_bots = [b for b in bots if b["status"] != "online"]
            if not stopped_bots:
                await event.respond("Semua bot payment sudah dalam status online.")
                return
            buttons = [
                [Button.inline(f"▶️ Hidupkan {b['bot_code']}", data=f"adm_start:{b['bot_code']}")]
                for b in stopped_bots
            ]
            await event.respond("Pilih bot yang ingin dihidupkan kembali:", buttons=buttons)
            return

        if text == "🗑️ Hapus Bot":
            bots = await bot_manager.list_all() if bot_manager else []
            if not bots:
                await event.respond("Belum ada bot yang terdaftar.")
                return
            buttons = [
                [Button.inline(f"🗑️ Hapus {b['bot_code']}", data=f"adm_delbot:{b['bot_code']}")]
                for b in bots
            ]
            await event.respond("Pilih bot yang ingin dihapus permanen:", buttons=buttons)
            return

    # -------------------------------------------------------------------------
    # Submenu Buttons Routing (Kelola Paket VIP)
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(func=lambda e: is_admin(config, e.sender_id) and e.raw_text in (
        "➕ Tambah Paket VIP",
        "📑 Daftar Paket VIP",
        "🗑️ Hapus Paket VIP",
    )))
    async def admin_package_actions(event):
        text = event.raw_text.strip()

        bots = await bot_manager.list_all() if bot_manager else []
        if not bots:
            await event.respond(
                "⚠️ <b>Belum Ada Bot Payment Terdaftar</b>\n\n"
                "Untuk mengelola paket VIP, kamu harus menambahkan bot payment terlebih dahulu.\n\n"
                "👉 Silakan buka menu:\n"
                "<b>🤖 Kelola Bot Payment</b> ➡️ <b>➕ Tambah Bot Baru</b>",
                parse_mode="html",
                buttons=admin_main_menu_keyboard(),
            )
            return

        if text == "📑 Daftar Paket VIP":
            if len(bots) == 1:
                pkgs = await db.list_all_packages(bot_code=bots[0]["bot_code"])
                await event.respond(package_list_text(pkgs, bot_code=bots[0]["bot_code"]), parse_mode="html")
            else:
                buttons = [
                    [Button.inline(f"🤖 {b['bot_code']}", data=f"adm_pkgbot_menu:{b['bot_code']}")]
                    for b in bots
                ]
                await event.respond("Pilih bot payment untuk melihat dan mengelola paket VIP:", buttons=buttons)
            return

        if text == "➕ Tambah Paket VIP":
            if len(bots) == 1:
                admin_states[event.sender_id] = {
                    "action": "add_package",
                    "step": "code",
                    "data": {"bot_code": bots[0]["bot_code"]},
                }
                await event.respond(
                    f"📦 <b>Tambah Paket VIP untuk [{bots[0]['bot_code']}] (Langkah 1/4)</b>\n\n"
                    f"Ketik <b>kode paket</b> (huruf kecil & angka tanpa spasi).\n"
                    f"Contoh: <code>vip1</code>",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
            else:
                buttons = [
                    [Button.inline(f"🤖 {b['bot_code']}", data=f"adm_addpkg_bot:{b['bot_code']}")]
                    for b in bots
                ]
                await event.respond("Pilih bot payment yang ingin ditambahkan paket VIP:", buttons=buttons)
            return

        if text == "🗑️ Hapus Paket VIP":
            if len(bots) == 1:
                pkgs = await db.list_all_packages(bot_code=bots[0]["bot_code"])
                if not pkgs:
                    await event.respond(f"Belum ada paket VIP untuk bot [{bots[0]['bot_code']}].")
                    return
                buttons = [
                    [Button.inline(f"🗑️ {p['code']} - {format_button_amount(p['amount'])}", data=f"adm_delpkg:{bots[0]['bot_code']}:{p['code']}")]
                    for p in pkgs
                ]
                await event.respond(f"Pilih paket VIP bot [{bots[0]['bot_code']}] yang ingin dihapus:", buttons=buttons)
            else:
                buttons = [
                    [Button.inline(f"🤖 {b['bot_code']}", data=f"adm_delpkg_bot:{b['bot_code']}")]
                    for b in bots
                ]
                await event.respond("Pilih bot payment yang paketnya ingin dihapus:", buttons=buttons)
            return

    # -------------------------------------------------------------------------
    # Submenu Buttons Routing (Kelola Broadcast)
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(func=lambda e: is_admin(config, e.sender_id) and e.raw_text in (
        "📝 Set Pesan Broadcast",
        "⏰ Set Jadwal Broadcast",
        "🧪 Test Broadcast",
        "📊 Status Broadcast",
    )))
    async def admin_broadcast_actions(event):
        text = event.raw_text.strip()

        if text == "📊 Status Broadcast":
            bots = await bot_manager.list_all() if bot_manager else []
            bot_codes = ["default"] + [b["bot_code"] for b in bots if b["bot_code"] != "default"]

            lines = ["📢 <b>Status Konfigurasi Broadcast Multi-Bot:</b>\n"]
            for b_code in bot_codes:
                b_time = await db.get_broadcast_time(bot_code=b_code) or "OFF"
                last_date = await db.get_last_broadcast_date(bot_code=b_code) or "-"
                msg = await db.get_active_broadcast_message(bot_code=b_code)
                user_count = await db.count_broadcast_targets(bot_code=b_code)
                msg_status = "✅ Ada" if msg else "❌ Belum diset"
                if msg and msg.get("media_type"):
                    msg_status += f" ({msg['media_type']})"

                lines.append(
                    f"🤖 <b>{html.escape(b_code)}</b>:\n"
                    f"• Jadwal: <b>{html.escape(b_time)} WIB</b>\n"
                    f"• Pesan: {msg_status}\n"
                    f"• Terakhir Kirim: <code>{html.escape(last_date)}</code>\n"
                    f"• Target Member: <b>{user_count} orang</b>\n"
                )
            await event.respond("\n".join(lines), parse_mode="html")
            return

        bots = await bot_manager.list_all() if bot_manager else []
        bot_codes = [b["bot_code"] for b in bots]
        if not bot_codes:
            bot_codes = ["default"]

        if text == "📝 Set Pesan Broadcast":
            if len(bot_codes) == 1:
                admin_states[event.sender_id] = {
                    "action": "set_broadcast_msg",
                    "step": "message",
                    "data": {"bot_code": bot_codes[0]},
                }
                await event.respond(
                    f"📢 <b>Set Pesan Broadcast untuk [{bot_codes[0]}]</b>\n\n"
                    f"Kirimkan pesan teks atau media (foto/video) yang ingin dijadikan materi broadcast.\n"
                    f"Formatting (Bold, Italic, Link) akan tersimpan otomatis.",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
            else:
                buttons = [
                    [Button.inline(f"🤖 {code}", data=f"adm_setbc_bot:{code}")]
                    for code in bot_codes
                ]
                await event.respond("Pilih bot yang ingin diset pesan broadcastnya:", buttons=buttons)
            return

        if text == "⏰ Set Jadwal Broadcast":
            if len(bot_codes) == 1:
                admin_states[event.sender_id] = {
                    "action": "set_broadcast_time",
                    "step": "time",
                    "data": {"bot_code": bot_codes[0]},
                }
                quick_time_keyboard = [
                    [Button.text("09:00", resize=True), Button.text("12:00"), Button.text("15:00")],
                    [Button.text("19:00"), Button.text("21:00"), Button.text("off")],
                    [Button.text("❌ Batal")],
                ]
                await event.respond(
                    f"⏰ <b>Atur Jadwal Broadcast [{bot_codes[0]}]</b>\n\n"
                    f"Pilih jam cepat di bawah atau ketik jam manual (format <code>HH:MM</code>, contoh <code>09:30</code> WIB).\n"
                    f"Ketik <code>off</code> untuk menonaktifkan broadcast otomatis.",
                    parse_mode="html",
                    buttons=quick_time_keyboard,
                )
            else:
                buttons = [
                    [Button.inline(f"🤖 {code}", data=f"adm_setbct_bot:{code}")]
                    for code in bot_codes
                ]
                await event.respond("Pilih bot yang ingin diatur jadwal broadcastnya:", buttons=buttons)
            return

        if text == "🧪 Test Broadcast":
            buttons = [
                [Button.inline(f"🧪 Test {code}", data=f"adm_testbc_bot:{code}")]
                for code in bot_codes
            ]
            await event.respond("Pilih bot yang ingin diuji coba broadcastnya:", buttons=buttons)
            return

    # -------------------------------------------------------------------------
    # Multi-Step Input Handler (Captures Text & Media during Active Wizard)
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(func=lambda e: is_admin(config, e.sender_id) and e.sender_id in admin_states))
    async def admin_multistep_input_processor(event):
        state = admin_states.get(event.sender_id)
        if not state:
            return

        raw_text = (event.raw_text or "").strip()
        action = state.get("action")
        step = state.get("step")

        # ---------------------------------------------------------------------
        # Wizard: Add Bot
        # ---------------------------------------------------------------------
        if action == "add_bot":
            if step == "code":
                code = raw_text.lower()
                if not re.match(r"^[a-z0-9_]{3,30}$", code):
                    await event.respond(
                        "⚠️ <b>Kode bot tidak valid.</b>\n"
                        "Gunakan hanya huruf kecil, angka, dan underscore (3-30 karakter).\n"
                        "Contoh: <code>botpayment1</code>",
                        parse_mode="html",
                        buttons=cancel_keyboard(),
                    )
                    return
                state["data"]["bot_code"] = code
                state["step"] = "token"
                await event.respond(
                    f"🤖 <b>Tambah Bot Payment Baru (Langkah 2/2)</b>\n\n"
                    f"Kode Bot: <code>{code}</code>\n\n"
                    f"Sekarang kirimkan <b>Bot Token</b> dari @BotFather.\n"
                    f"Contoh: <code>123456789:AAHx...</code>",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
                return

            if step == "token":
                token = raw_text.strip()
                code = state["data"]["bot_code"]
                status_msg = await event.respond(
                    f"⏳ Sedang memvalidasi token dan menghubungkan <code>{code}</code> ke Telegram...",
                    parse_mode="html",
                )
                try:
                    res = await bot_manager.spawn_bot({
                        "bot_code": code,
                        "bot_token": token,
                        "bot_name": code,
                    })
                    admin_states.pop(event.sender_id, None)

                    # Notify LOG_CHAT_ID
                    await send_log(
                        client,
                        config,
                        db,
                        (
                            "🤖 <b>Bot Payment Baru Aktif!</b>\n"
                            f"• Nama Bot: <b>{html.escape(res.get('bot_name', code))}</b>\n"
                            f"• Username: @{html.escape(res.get('bot_username', ''))}\n"
                            f"• Kode Bot: <code>{html.escape(code)}</code>\n"
                            f"• Ditambahkan oleh: <code>{event.sender_id}</code>\n"
                            "• Status: 🟢 Online & Siap Digunakan"
                        ),
                    )

                    await status_msg.edit(
                        f"✅ <b>Bot Payment Berhasil Ditambahkan & Langsung Aktif!</b>\n\n"
                        f"• Kode: <code>{html.escape(code)}</code>\n"
                        f"• Username: @{html.escape(res.get('bot_username', ''))}\n"
                        f"• Status: 🟢 <b>Online (Running)</b>\n\n"
                        f"Bot siap melayani pembayaran! Kamu bisa menambahkan paket VIP untuk bot ini melalui menu <b>📦 Kelola Paket VIP</b>.",
                        parse_mode="html",
                    )
                    await event.respond("Pilih menu:", buttons=admin_main_menu_keyboard())
                except Exception as exc:
                    LOGGER.exception("Error spawning bot %s", code)
                    await status_msg.edit(
                        f"❌ <b>Gagal menghubungkan bot:</b>\n<code>{html.escape(str(exc))}</code>\n\n"
                        f"Pastikan token benar, atau klik <b>❌ Batal</b> untuk keluar.",
                        parse_mode="html",
                        buttons=cancel_keyboard(),
                    )
                return

        # ---------------------------------------------------------------------
        # Wizard: Add Package
        # ---------------------------------------------------------------------
        if action == "add_package":
            bot_code = state["data"]["bot_code"]
            if step == "code":
                try:
                    pkg_code = normalize_package_code(raw_text)
                except ValueError as err:
                    await event.respond(f"⚠️ {err}\nMasukkan kode paket (contoh: <code>vip1</code>):", parse_mode="html", buttons=cancel_keyboard())
                    return
                state["data"]["code"] = pkg_code
                state["step"] = "name"
                await event.respond(
                    f"📦 <b>Tambah Paket VIP [{bot_code}] (Langkah 2/4)</b>\n\n"
                    f"Kode: <code>{pkg_code}</code>\n\n"
                    f"Masukkan <b>nama grup/channel VIP</b>.\n"
                    f"Contoh: <code>Group VIP Premium 1 Bulan</code>",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
                return

            if step == "name":
                if not raw_text:
                    await event.respond("Nama paket tidak boleh kosong. Masukkan nama paket:", buttons=cancel_keyboard())
                    return
                state["data"]["name"] = raw_text
                state["step"] = "chat_id"
                await event.respond(
                    f"📦 <b>Tambah Paket VIP [{bot_code}] (Langkah 3/4)</b>\n\n"
                    f"Nama: <b>{html.escape(raw_text)}</b>\n\n"
                    f"Masukkan <b>Chat ID Group/Channel VIP Telegram</b> (biasanya berawalan <code>-100...</code>).\n"
                    f"Contoh: <code>-100192837465</code>",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
                return

            if step == "chat_id":
                try:
                    vip_chat_id = int(raw_text)
                except ValueError:
                    await event.respond("Chat ID harus angka (contoh: <code>-100192837465</code>):", parse_mode="html", buttons=cancel_keyboard())
                    return
                state["data"]["vip_chat_id"] = vip_chat_id
                state["step"] = "amount"
                await event.respond(
                    f"📦 <b>Tambah Paket VIP [{bot_code}] (Langkah 4/4)</b>\n\n"
                    f"Chat ID: <code>{vip_chat_id}</code>\n\n"
                    f"Masukkan <b>harga paket</b> dalam Rupiah (angka saja).\n"
                    f"Contoh: <code>50000</code>",
                    parse_mode="html",
                    buttons=cancel_keyboard(),
                )
                return

            if step == "amount":
                digits = "".join(ch for ch in raw_text if ch.isdigit())
                if not digits:
                    await event.respond("Harga harus berupa angka. Masukkan harga paket:", buttons=cancel_keyboard())
                    return
                amount = int(digits)
                data = state["data"]
                admin_states.pop(event.sender_id, None)

                try:
                    await db.upsert_package(
                        code=data["code"],
                        name=data["name"],
                        vip_chat_id=data["vip_chat_id"],
                        amount=amount,
                        bot_code=bot_code,
                    )
                    await event.respond(
                        f"✅ <b>Paket VIP Berhasil Disimpan!</b>\n\n"
                        f"• Bot: <b>{html.escape(bot_code)}</b>\n"
                        f"• Kode Paket: <code>{html.escape(data['code'])}</code>\n"
                        f"• Nama: <b>{html.escape(data['name'])}</b>\n"
                        f"• VIP Chat ID: <code>{data['vip_chat_id']}</code>\n"
                        f"• Harga: <b>{format_rupiah(amount)}</b>",
                        parse_mode="html",
                        buttons=admin_main_menu_keyboard(),
                    )
                except Exception as exc:
                    LOGGER.exception("Error saving package")
                    await event.respond(f"❌ Gagal menyimpan paket: {html.escape(str(exc))}", buttons=admin_main_menu_keyboard())
                return

        # ---------------------------------------------------------------------
        # Wizard: Set Broadcast Message
        # ---------------------------------------------------------------------
        if action == "set_broadcast_msg":
            bot_code = state["data"]["bot_code"]
            text_content = event.raw_text or ""
            media_path_str = ""
            media_type = ""
            if getattr(event, "media", None):
                broadcast_dir = Path("data/broadcast_media")
                broadcast_dir.mkdir(parents=True, exist_ok=True)
                for old_f in broadcast_dir.glob(f"bc_{bot_code}.*"):
                    try:
                        old_f.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    downloaded = await event.download_media(file=str(broadcast_dir / f"bc_{bot_code}"))
                    if downloaded:
                        media_path_str = Path(downloaded).as_posix()
                        media_type = getattr(getattr(event, "file", None), "mime_type", "") or ""
                except Exception as down_exc:
                    LOGGER.warning("Failed to download broadcast media: %s", down_exc)

            if not text_content and not media_path_str:
                await event.respond("Pesan harus memiliki teks atau media. Silakan kirimkan kembali:", buttons=cancel_keyboard())
                return

            admin_states.pop(event.sender_id, None)
            entities_json = entities_to_json(event.entities or [])
            await db.set_broadcast_message(
                text_content, media_path_str, media_type, entities_json, bot_code=bot_code
            )
            await event.respond(
                f"✅ <b>Pesan broadcast untuk bot <code>{html.escape(bot_code)}</code> berhasil disimpan!</b>\n\n"
                f"Kamu bisa menguji pengiriman lewat menu <b>📢 Kelola Broadcast -> 🧪 Test Broadcast</b>.",
                parse_mode="html",
                buttons=admin_main_menu_keyboard(),
            )
            return

        # ---------------------------------------------------------------------
        # Wizard: Set Broadcast Time
        # ---------------------------------------------------------------------
        if action == "set_broadcast_time":
            bot_code = state["data"]["bot_code"]
            raw_time = raw_text.lower()
            try:
                time_value = validate_broadcast_time(raw_time)
            except ValueError:
                await event.respond("Format jam salah. Gunakan `HH:MM` (contoh `09:30`) atau `off`:", buttons=cancel_keyboard())
                return

            admin_states.pop(event.sender_id, None)
            await db.set_broadcast_time(time_value, bot_code=bot_code)
            await db.set_last_broadcast_date("", bot_code=bot_code)
            if not time_value:
                await event.respond(
                    f"✅ Broadcast otomatis untuk bot <b>{html.escape(bot_code)}</b> dinonaktifkan.",
                    parse_mode="html",
                    buttons=admin_main_menu_keyboard(),
                )
            else:
                await event.respond(
                    f"✅ Broadcast otomatis untuk bot <b>{html.escape(bot_code)}</b> dijadwalkan setiap <b>{html.escape(time_value)} WIB</b>.",
                    parse_mode="html",
                    buttons=admin_main_menu_keyboard(),
                )
            return

        # ---------------------------------------------------------------------
        # Wizard: Runtime Settings
        # ---------------------------------------------------------------------
        if action == "set_config":
            setting_key = state["data"]["key"]
            try:
                int_val = int(raw_text)
                await db.set_setting(setting_key, str(int_val))
                admin_states.pop(event.sender_id, None)
                await event.respond(
                    f"✅ Pengaturan <code>{setting_key}</code> berhasil diubah menjadi <code>{int_val}</code>.",
                    parse_mode="html",
                    buttons=admin_main_menu_keyboard(),
                )
            except ValueError:
                await event.respond("Nilai harus berupa angka chat ID Telegram:", buttons=cancel_keyboard())
            return

    # -------------------------------------------------------------------------
    # Callback Query Handlers (Inline Buttons)
    # -------------------------------------------------------------------------
    @client.on(events.CallbackQuery())
    async def admin_callback_dispatcher(event):
        if not is_admin(config, event.sender_id):
            return

        data = event.data.decode(errors="ignore")

        # 1. Approve Withdrawal
        if data.startswith("adm_appr:"):
            w_id = int(data.split(":")[1])
            withdrawal = await db.update_withdrawal_status(w_id, "pending", "completed", event.sender_id)
            if not withdrawal:
                await event.answer("Pengajuan sudah pernah diproses.", alert=True)
                return
            await event.answer("Withdrawal disetujui.")
            await event.edit(
                f"✅ <b>Withdrawal #{w_id} Disetujui (Completed)</b>\n"
                f"Diproses oleh Admin: <code>{event.sender_id}</code>",
                parse_mode="html",
            )
            bot_code = withdrawal.get("bot_code") or "default"
            target_client = bot_manager.get_client(bot_code) if bot_manager else client
            await safe_send_user(
                target_client,
                config,
                db,
                withdrawal["user_id"],
                f"🎉 Penarikan saldo sebesar <b>{format_rupiah(withdrawal['amount'])}</b> berhasil ditransfer ke akun E-Wallet kamu!",
                parse_mode="html",
            )
            return

        # 2. Reject Withdrawal
        if data.startswith("adm_rejc:"):
            w_id = int(data.split(":")[1])
            withdrawal = await db.update_withdrawal_status(w_id, "pending", "rejected", event.sender_id)
            if not withdrawal:
                await event.answer("Pengajuan sudah pernah diproses.", alert=True)
                return
            await event.answer("Withdrawal ditolak.")
            await event.edit(
                f"❌ <b>Withdrawal #{w_id} Ditolak (Saldo Dikembalikan)</b>\n"
                f"Diproses oleh Admin: <code>{event.sender_id}</code>",
                parse_mode="html",
            )
            bot_code = withdrawal.get("bot_code") or "default"
            target_client = bot_manager.get_client(bot_code) if bot_manager else client
            await safe_send_user(
                target_client,
                config,
                db,
                withdrawal["user_id"],
                f"⚠️ Pengajuan penarikan saldo sebesar <b>{format_rupiah(withdrawal['amount'])}</b> ditolak oleh Admin. Saldo telah dikembalikan ke akun kamu.",
                parse_mode="html",
            )
            return

        # 3. Stop Bot
        if data.startswith("adm_stop:"):
            bot_code = data.split(":")[1]
            if bot_manager:
                await bot_manager.stop_bot(bot_code)
                await event.edit(f"⏹️ Bot <b>{html.escape(bot_code)}</b> berhasil dihentikan.", parse_mode="html")
                await send_log(client, config, db, f"<b>Bot Stopped</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
            return

        # 4. Start Bot
        if data.startswith("adm_start:"):
            bot_code = data.split(":")[1]
            if bot_manager:
                bot_data = await db.get_bot(bot_code)
                if bot_data:
                    await bot_manager.spawn_bot(bot_data)
                    await event.edit(f"▶️ Bot <b>{html.escape(bot_code)}</b> berhasil diaktifkan kembali.", parse_mode="html")
                    await send_log(client, config, db, f"<b>Bot Started</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
            return

        # 5. Delete Bot
        if data.startswith("adm_delbot:"):
            bot_code = data.split(":")[1]
            if bot_manager:
                await bot_manager.delete_bot(bot_code)
                await event.edit(f"🗑️ Bot <b>{html.escape(bot_code)}</b> berhasil dihapus dari database.", parse_mode="html")
                await send_log(client, config, db, f"<b>Bot Deleted</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
            return

        # 5b. Select Bot Package Menu Dashboard
        if data.startswith("adm_pkgbot_menu:"):
            bot_code = data.split(":")[1]
            pkgs = await db.list_all_packages(bot_code=bot_code)
            bots = await bot_manager.list_all() if bot_manager else []
            bot_info = next((b for b in bots if b["bot_code"] == bot_code), None)

            status_badge = "🟢 Aktif / Online" if (bot_info and bot_info["status"] == "online") else "🔴 Nonaktif"
            bot_username = f"@{bot_info['bot_username']}" if (bot_info and bot_info.get("bot_username")) else "-"
            bot_name = bot_info.get("bot_name") or bot_code if bot_info else bot_code

            lines = [
                f"📦 <b>Kelola Paket VIP — [{html.escape(bot_code)}]</b>\n",
                f"• Bot: <b>{html.escape(bot_name)}</b> ({html.escape(bot_username)})",
                f"• Status: <b>{status_badge}</b>",
                f"• Total Paket VIP: <b>{len(pkgs)} paket</b>\n",
            ]

            if pkgs:
                lines.append("<b>Daftar Paket Terdaftar:</b>")
                for p in pkgs:
                    p_status = "🟢" if p.get("active", True) else "🔴"
                    lines.append(f"{p_status} <code>{html.escape(p['code'])}</code>: {html.escape(p['name'])} — <b>{format_button_amount(p['amount'])}</b>")
            else:
                lines.append("<i>Belum ada paket VIP untuk bot ini.</i>")

            lines.append("\nPilih aksi di bawah:")

            buttons = [
                [Button.inline("➕ Tambah Paket VIP", data=f"adm_addpkg_bot:{bot_code}")],
            ]
            if pkgs:
                buttons.append([Button.inline("🗑️ Hapus Paket VIP", data=f"adm_delpkg_bot:{bot_code}")])
            buttons.append([Button.inline("🔙 Pilih Bot Lain", data="adm_pkg_choose_bot")])

            await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
            return

        if data == "adm_pkg_choose_bot":
            bots = await bot_manager.list_all() if bot_manager else []
            if not bots:
                await event.edit(
                    "⚠️ <b>Belum Ada Bot Payment Terdaftar</b>\n\n"
                    "Silakan tambah bot payment terlebih dahulu di menu <b>🤖 Kelola Bot Payment</b>.",
                    parse_mode="html",
                )
                return
            buttons = [
                [Button.inline(f"🤖 {b['bot_code']}" + (f" (@{b['bot_username']})" if b.get('bot_username') else ""), data=f"adm_pkgbot_menu:{b['bot_code']}")]
                for b in bots
            ]
            await event.edit(
                "📦 <b>Menu Kelola Paket VIP</b>\n\n"
                "Silakan pilih bot payment yang ingin kamu kelola paket VIP-nya:",
                parse_mode="html",
                buttons=buttons,
            )
            return

        if data.startswith("adm_delpkg_bot:"):
            bot_code = data.split(":")[1]
            pkgs = await db.list_all_packages(bot_code=bot_code)
            if not pkgs:
                await event.edit(
                    f"Belum ada paket VIP untuk bot <b>[{html.escape(bot_code)}]</b>.",
                    parse_mode="html",
                    buttons=[[Button.inline(f"🔙 Kembali ke [{bot_code}]", data=f"adm_pkgbot_menu:{bot_code}")]],
                )
                return

            buttons = [
                [Button.inline(f"🗑️ {p['code']} - {format_button_amount(p['amount'])}", data=f"adm_delpkg:{bot_code}:{p['code']}")]
                for p in pkgs
            ]
            buttons.append([Button.inline(f"🔙 Batal / Kembali", data=f"adm_pkgbot_menu:{bot_code}")])
            await event.edit(
                f"Pilih paket VIP bot <b>[{html.escape(bot_code)}]</b> yang ingin dihapus:",
                parse_mode="html",
                buttons=buttons,
            )
            return

        # 6. Delete Package
        if data.startswith("adm_delpkg:"):
            parts = data.split(":")
            bot_code = parts[1]
            pkg_code = parts[2]
            await db.delete_package(pkg_code, bot_code=bot_code)
            await event.edit(
                f"🗑️ Paket <code>{html.escape(pkg_code)}</code> ({html.escape(bot_code)}) berhasil dihapus.",
                parse_mode="html",
                buttons=[[Button.inline(f"🔙 Kembali ke Paket [{bot_code}]", data=f"adm_pkgbot_menu:{bot_code}")]],
            )
            return

        # 7. Select Bot for Add Package
        if data.startswith("adm_addpkg_bot:"):
            bot_code = data.split(":")[1]
            admin_states[event.sender_id] = {
                "action": "add_package",
                "step": "code",
                "data": {"bot_code": bot_code},
            }
            await event.edit(
                f"📦 <b>Tambah Paket VIP [{html.escape(bot_code)}] (Langkah 1/4)</b>\n\n"
                f"Ketik <b>kode paket</b> (huruf kecil & angka tanpa spasi).\n"
                f"Contoh: <code>vip1</code>",
                parse_mode="html",
            )
            await event.respond("Ketik kode paket atau klik Batal:", buttons=cancel_keyboard())
            return

        # 8. Select Bot for Set Broadcast Message
        if data.startswith("adm_setbc_bot:"):
            bot_code = data.split(":")[1]
            admin_states[event.sender_id] = {
                "action": "set_broadcast_msg",
                "step": "message",
                "data": {"bot_code": bot_code},
            }
            await event.edit(
                f"📢 <b>Set Pesan Broadcast untuk [{html.escape(bot_code)}]</b>\n\n"
                f"Silakan kirimkan pesan teks atau media yang ingin kamu jadikan konten broadcast.",
                parse_mode="html",
            )
            await event.respond("Kirim pesan / media sekarang:", buttons=cancel_keyboard())
            return

        # 9. Select Bot for Set Broadcast Time
        if data.startswith("adm_setbct_bot:"):
            bot_code = data.split(":")[1]
            admin_states[event.sender_id] = {
                "action": "set_broadcast_time",
                "step": "time",
                "data": {"bot_code": bot_code},
            }
            quick_time_keyboard = [
                [Button.text("09:00", resize=True), Button.text("12:00"), Button.text("15:00")],
                [Button.text("19:00"), Button.text("21:00"), Button.text("off")],
                [Button.text("❌ Batal")],
            ]
            await event.edit(
                f"⏰ <b>Atur Jadwal Broadcast [{html.escape(bot_code)}]</b>\n\n"
                f"Pilih jam cepat atau ketik jam pengiriman (format <code>HH:MM</code>, contoh <code>09:30</code> WIB).",
                parse_mode="html",
            )
            await event.respond("Pilih jam pengiriman:", buttons=quick_time_keyboard)
            return

        # 10. Select Bot for Test Broadcast
        if data.startswith("adm_testbc_bot:"):
            bot_code = data.split(":")[1]
            msg = await db.get_active_broadcast_message(bot_code=bot_code)
            if not msg:
                await event.answer("Belum ada pesan broadcast untuk bot ini.", alert=True)
                return
            target_client = bot_manager.get_client(bot_code) if bot_manager else client
            if not target_client or not getattr(target_client, "is_connected", lambda: False)():
                await event.answer(f"Bot [{bot_code}] sedang tidak aktif atau terputus.", alert=True)
                return

            admin_ids = list(config.admin_user_ids) or [event.sender_id]
            admin_targets = [{"user_id": uid, "access_hash": 0} for uid in admin_ids]

            media_handle = None
            media_file = msg.get("media_telegram_file_id") or ""
            if media_file and Path(media_file).is_file():
                try:
                    media_handle = await target_client.upload_file(media_file)
                except Exception as up_exc:
                    LOGGER.warning("Could not pre-upload test broadcast media: %s", up_exc)

            totals = await send_broadcast_batch(
                target_client, db, msg, admin_targets, concurrency=1, bot_code=bot_code, uploaded_media=media_handle, is_test=True
            )
            await event.edit(
                f"✅ <b>Test Broadcast Selesai [{html.escape(bot_code)}]</b>\n"
                f"• Terkirim ke Admin: <code>{totals['sent']}/{len(admin_ids)}</code>\n"
                f"• Error: <code>{totals['error']}</code>",
                parse_mode="html",
            )
            return

        # 11. Config Updates
        if data.startswith("adm_cfg:"):
            key = data.split(":")[1]
            admin_states[event.sender_id] = {
                "action": "set_config",
                "step": "value",
                "data": {"key": key},
            }
            await event.edit(f"Ketik ID Telegram baru untuk <code>{key}</code>:", parse_mode="html")
            await event.respond("Masukkan ID baru:", buttons=cancel_keyboard())
            return

    # -------------------------------------------------------------------------
    # Backward Compatibility Slash Commands (/bot_add, /package_add, etc.)
    # -------------------------------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/bot_add(?:@\w+)?(?:\s+(.+))?$"))
    async def legacy_bot_add(event):
        if not await require_admin(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return
        raw = (event.pattern_match.group(1) or "").strip()
        parts = raw.split()
        if len(parts) < 2:
            await event.respond("Format: `/bot_add <nama_bot> <bot_token>`")
            return
        bot_code, bot_token = parts[0].lower(), parts[1]
        try:
            res = await bot_manager.spawn_bot({"bot_code": bot_code, "bot_token": bot_token, "bot_name": bot_code})
            await send_log(
                client, config, db,
                f"🤖 <b>Bot Payment Baru Aktif!</b>\n• Nama: <b>{bot_code}</b>\n• Status: 🟢 Online"
            )
            await event.respond(f"✅ Bot <b>{bot_code}</b> berhasil diaktifkan!", parse_mode="html", buttons=admin_main_menu_keyboard())
        except Exception as exc:
            await event.respond(f"❌ Gagal: {exc}")

    @client.on(events.NewMessage(pattern=r"^/package_add(?:@\w+)?(?:\s+(.+))?$"))
    async def legacy_package_add(event):
        if not await require_admin(event, config, db):
            return
        raw = (event.pattern_match.group(1) or "").strip()
        try:
            bot_code, code, name, chat_id, amount = parse_package_add_args(raw)
            await db.upsert_package(code, name, chat_id, amount, bot_code=bot_code)
            await event.respond(f"✅ Paket <code>{code}</code> ({bot_code}) berhasil disimpan!", parse_mode="html", buttons=admin_main_menu_keyboard())
        except Exception as exc:
            await event.respond(f"❌ Format salah: {exc}")

    @client.on(events.NewMessage(pattern=r"^/tarik_list(?:@\w+)?$"))
    async def legacy_tarik_list(event):
        if not await require_admin(event, config, db):
            return
        pending = await db.list_pending_withdrawals()
        if not pending:
            await event.respond("✅ Tidak ada antrean penarikan pending.")
            return
        for w in pending:
            await event.respond(
                f"💳 <b>Penarikan #{w['id']}</b> ({w['bot_code']}) - <b>{format_rupiah(w['amount'])}</b>\n"
                f"User: <code>{w['user_id']}</code> | No HP: <code>{w['phone']}</code>",
                parse_mode="html",
                buttons=[[Button.inline("Approve", f"adm_appr:{w['id']}"), Button.inline("Reject", f"adm_rejc:{w['id']}")]]
            )
