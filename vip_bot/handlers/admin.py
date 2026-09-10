import asyncio
import io
import html
import logging
import re
from telethon import events, Button, errors
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
    safe_send_user,
    entities_to_json,
)
from vip_bot.messages import (
    admin_command_list_text,
    custom_qris_caption,
    package_list_text,
    bot_list_text,
)
from vip_bot.loops import send_broadcast_batch
from sociabuzz_client import SociaBuzzError

LOGGER = logging.getLogger("telegram_vip_bot.handlers.admin")


async def require_admin(event, config, db):
    if is_admin(config, event.sender_id):
        return True
    if event.is_private:
        await event.respond("⛔ <b>Akses Ditolak</b>\nBot ini khusus manajemen admin.", parse_mode="html")
    return False


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
    if amount_value < 1000:
        raise ValueError("Nominal paket minimal Rp1.000.")
    if amount_value > 10_000_000:
        raise ValueError("Nominal paket maksimal Rp10.000.000.")
    return bot_code, normalize_package_code(code), name, int(chat_id), amount_value


def register_admin_handlers(client, config, db, qris_semaphore, user_locks, bot_manager=None):
    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?(?:\s+.*)?$"))
    async def admin_start_handler(event):
        if not is_admin(config, event.sender_id):
            if event.is_private:
                await event.respond(
                    "⛔ <b>Akses Ditolak</b>\n"
                    "Bot ini adalah <b>Master Management Bot</b> dan hanya dapat diakses oleh Administrator terdaftar.",
                    parse_mode="html",
                )
            return

        bots = await bot_manager.list_all() if bot_manager else []
        active_count = sum(1 for b in bots if b["status"] == "online")
        dashboard_text = (
            f"👋 <b>Halo Admin! Selamat datang di Master Management Bot</b>\n\n"
            f"• Status Sistem: 🟢 <b>Online</b>\n"
            f"• Bot Payment Berjalan: <b>{active_count} bot</b>\n\n"
            f"{admin_command_list_text()}"
        )
        await event.respond(dashboard_text, parse_mode="html")

    @client.on(events.NewMessage(func=lambda e: e.is_private and not is_admin(config, e.sender_id)))
    async def reject_unauthorized_private(event):
        text = (event.raw_text or "").strip()
        if not text.startswith("/start"):
            await event.respond(
                "⛔ <b>Akses Ditolak</b>\n"
                "Bot ini hanya dapat digunakan oleh Administrator terdaftar.",
                parse_mode="html",
            )

    # -------------------------------------------------------------------------
    # Bot Management Commands
    # -------------------------------------------------------------------------

    @client.on(events.NewMessage(pattern=r"^/bot_add(?:@\w+)?(?:\s+(.+))?$"))
    async def bot_add(event):
        if not await require_admin_logchat(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return

        raw_args = (event.pattern_match.group(1) or "").strip()
        parts = raw_args.split()
        if len(parts) < 2:
            await event.respond("Format: <code>/bot_add &lt;nama_bot&gt; &lt;bot_token&gt;</code>\nContoh: <code>/bot_add botpayment1 123456789:AAHx...</code>", parse_mode="html")
            return

        bot_code = parts[0].strip().lower()
        bot_token = parts[1].strip()

        if not re.match(r"^[a-z0-9_]{3,30}$", bot_code):
            await event.respond("Nama bot hanya boleh huruf kecil, angka, dan underscore (3-30 karakter). Contoh: <code>botpayment1</code>", parse_mode="html")
            return

        status_msg = await event.respond(f"⏳ Memvalidasi & menjalankan <code>{html.escape(bot_code)}</code>...", parse_mode="html")
        try:
            bot_data = {
                "bot_code": bot_code,
                "bot_token": bot_token,
                "bot_name": bot_code,
            }
            res = await bot_manager.spawn_bot(bot_data)
            username_str = f"@{res['bot_username']}" if res.get("bot_username") else "-"
            await status_msg.edit(
                "✅ <b>Bot Berhasil Ditambahkan & Langsung Aktif!</b>\n\n"
                f"• Nama: <code>{html.escape(bot_code)}</code>\n"
                f"• Username: <b>{username_str}</b>\n"
                "• Status: 🟢 <b>Online (Running)</b>\n\n"
                "Bot siap melayani pembayaran! Gunakan <code>/package_add</code> untuk menambahkan group VIP ke bot ini.",
                parse_mode="html",
            )
            await send_log(
                client,
                config,
                db,
                (
                    "🤖 <b>Bot Payment Baru Aktif!</b>\n"
                    f"• Nama Bot: <b>{html.escape(res.get('bot_name', bot_code))}</b>\n"
                    f"• Username: <b>{username_str}</b>\n"
                    f"• Kode Bot: <code>{html.escape(bot_code)}</code>\n"
                    f"• Ditambahkan oleh Admin: <code>{event.sender_id}</code>\n"
                    "• Status: 🟢 Online & Siap Digunakan"
                ),
            )
        except Exception as exc:
            LOGGER.exception("Failed to add bot")
            await status_msg.edit(f"❌ <b>Gagal menambahkan bot:</b>\n<code>{html.escape(str(exc))}</code>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/bot_list(?:@\w+)?$"))
    async def bot_list(event):
        if not await require_admin_logchat(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return
        bots = await bot_manager.list_all()
        await event.respond(bot_list_text(bots), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/bot_stop(?:@\w+)?(?:\s+(.+))?$"))
    async def bot_stop(event):
        if not await require_admin_logchat(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return
        bot_code = (event.pattern_match.group(1) or "").strip().lower()
        if not bot_code:
            await event.respond("Format: <code>/bot_stop &lt;nama_bot&gt;</code>", parse_mode="html")
            return
        stopped = await bot_manager.stop_bot(bot_code)
        if stopped:
            await event.respond(f"🔴 Bot <code>{html.escape(bot_code)}</code> berhasil dimatikan.", parse_mode="html")
            await send_log(client, config, db, f"<b>Bot Stopped</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
        else:
            await event.respond(f"Bot <code>{html.escape(bot_code)}</code> tidak sedang berjalan atau tidak ditemukan.", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/bot_start(?:@\w+)?(?:\s+(.+))?$"))
    async def bot_start(event):
        if not await require_admin_logchat(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return
        bot_code = (event.pattern_match.group(1) or "").strip().lower()
        if not bot_code:
            await event.respond("Format: <code>/bot_start &lt;nama_bot&gt;</code>", parse_mode="html")
            return
        bot_data = await db.get_bot(bot_code)
        if not bot_data:
            await event.respond(f"Bot <code>{html.escape(bot_code)}</code> tidak terdaftar di database.", parse_mode="html")
            return
        try:
            await bot_manager.spawn_bot(bot_data)
            await event.respond(f"🟢 Bot <code>{html.escape(bot_code)}</code> berhasil dijalankan kembali.", parse_mode="html")
            await send_log(client, config, db, f"<b>Bot Started</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
        except Exception as exc:
            await event.respond(f"❌ Gagal menyalakan bot <code>{html.escape(bot_code)}</code>:\n<code>{html.escape(str(exc))}</code>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/bot_delete(?:@\w+)?(?:\s+(.+))?$"))
    async def bot_delete(event):
        if not await require_admin_logchat(event, config, db):
            return
        if not bot_manager:
            await event.respond("Bot Manager tidak aktif.")
            return
        bot_code = (event.pattern_match.group(1) or "").strip().lower()
        if not bot_code:
            await event.respond("Format: <code>/bot_delete &lt;nama_bot&gt;</code>", parse_mode="html")
            return
        deleted = await bot_manager.delete_bot(bot_code)
        if deleted:
            await event.respond(f"🗑️ Bot <code>{html.escape(bot_code)}</code> berhasil dihapus dari sistem.", parse_mode="html")
            await send_log(client, config, db, f"<b>Bot Deleted</b>\nCode: <code>{html.escape(bot_code)}</code>\nAdmin: <code>{event.sender_id}</code>")
        else:
            await event.respond(f"Bot <code>{html.escape(bot_code)}</code> tidak ditemukan di database.", parse_mode="html")

    # -------------------------------------------------------------------------
    # Package Management Commands
    # -------------------------------------------------------------------------

    @client.on(events.NewMessage(pattern=r"^/package_add(?:@\w+)?(?:\s+(.+))?$"))
    async def package_add(event):
        if not await require_admin_logchat(event, config, db):
            return
        try:
            bot_code, code, name, vip_chat_id, amount = parse_package_add_args(event.pattern_match.group(1) or "")
            await db.upsert_package(code, name, vip_chat_id, amount, config.invite_expire_hours, bot_code=bot_code)
            await event.respond(
                f"✅ <b>Group VIP Disimpan!</b>\n"
                f"• Bot: <code>{html.escape(bot_code)}</code>\n"
                f"• Kode: <code>{html.escape(code)}</code>\n"
                f"• Nama: {html.escape(name)}\n"
                f"• VIP Chat ID: <code>{vip_chat_id}</code>\n"
                f"• Harga: <b>{format_button_amount(amount)}</b>",
                parse_mode="html",
            )
            await send_log(
                client,
                config,
                db,
                (
                    "<b>Package updated</b>\n"
                    f"Bot: <code>{html.escape(bot_code)}</code>\n"
                    f"Code: <code>{html.escape(code)}</code>\n"
                    f"Name: <code>{html.escape(name)}</code>\n"
                    f"VIP chat: <code>{vip_chat_id}</code>\n"
                    f"Amount: <code>{amount}</code>\n"
                    f"Admin: <code>{event.sender_id}</code>"
                ),
            )
        except Exception as exc:
            LOGGER.exception("Failed to add package")
            await event.respond(f"Gagal tambah paket: {html.escape(str(exc))}")

    @client.on(events.NewMessage(pattern=r"^/package_list(?:@\w+)?(?:\s+(.+))?$"))
    async def package_list(event):
        if not await require_admin_logchat(event, config, db):
            return
        bot_code = (event.pattern_match.group(1) or "").strip().lower() or None
        packages = await db.list_all_packages(bot_code=bot_code)
        await event.respond(package_list_text(packages, bot_code=bot_code), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/package_delete(?:@\w+)?(?:\s+(.+))?$"))
    async def package_delete(event):
        if not await require_admin_logchat(event, config, db):
            return
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            await event.respond("Format: <code>/package_delete &lt;nama_bot&gt; &lt;kode&gt;</code>", parse_mode="html")
            return
        tokens = raw.split()
        if len(tokens) >= 2:
            bot_code = tokens[0].lower()
            code = tokens[1]
        else:
            bot_code = "default"
            code = tokens[0]
        try:
            normalized = normalize_package_code(code)
            changed = await db.delete_package(normalized, bot_code=bot_code)
            if changed:
                await event.respond(f"Paket <code>{html.escape(normalized)}</code> ({html.escape(bot_code)}) dinonaktifkan.", parse_mode="html")
                await send_log(
                    client,
                    config,
                    db,
                    f"<b>Package disabled</b>\nBot: <code>{html.escape(bot_code)}</code>\nCode: <code>{html.escape(normalized)}</code>\nAdmin: <code>{event.sender_id}</code>",
                )
            else:
                await event.respond("Paket tidak ditemukan atau sudah nonaktif.")
        except Exception as exc:
            LOGGER.exception("Failed to delete package")
            await event.respond("Gagal hapus paket. Detail error dikirim ke log admin.")
            await send_log(client, config, db, f"<b>Package delete error</b>\nAdmin: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")

    # -------------------------------------------------------------------------
    # Withdrawal Admin Actions
    # -------------------------------------------------------------------------

    @client.on(events.CallbackQuery(pattern=rb"^withdraw_(done|reject):(\d+)$"))
    async def withdrawal_admin_action(event):
        if not is_admin(config, event.sender_id):
            await event.answer("Khusus admin.", alert=True)
            return
        action = event.pattern_match.group(1).decode()
        withdrawal_id = event.pattern_match.group(2).decode()
        status = "completed" if action == "done" else "rejected"
        label = "berhasil diproses" if action == "done" else "ditolak"
        try:
            withdrawal = await db.update_withdrawal_status(withdrawal_id, "pending", status, event.sender_id)
            if not withdrawal:
                await event.answer("Pengajuan sudah diproses.", alert=True)
                return
            await event.answer(f"Withdrawal {label}.")

            bot_code = withdrawal.get("bot_code") or "default"
            target_client = bot_manager.get_client(bot_code) if bot_manager else client
            await safe_send_user(
                target_client,
                config,
                db,
                withdrawal["user_id"],
                f"Pengajuan penarikan saldo {format_rupiah(withdrawal['amount'])} {label} oleh admin.",
            )
            message = await event.get_message()
            await event.edit(
                f"{message.raw_text}\n\nStatus: <b>{html.escape(status)}</b>\nAdmin: <code>{event.sender_id}</code>",
                parse_mode="html",
                buttons=None,
            )
        except Exception as exc:
            LOGGER.exception("Withdrawal admin action error")
            await event.answer("Gagal memproses withdrawal. Cek log.", alert=True)
            await send_log(client, config, db, f"<b>Withdrawal admin action error</b>\nID: <code>{html.escape(withdrawal_id)}</code>\n<code>{html.escape(str(exc))}</code>")

    # -------------------------------------------------------------------------
    # Utility Commands
    # -------------------------------------------------------------------------

    @client.on(events.NewMessage(pattern=r"^/chatid(?:@\w+)?$"))
    async def chat_id(event):
        if not await require_admin_logchat(event, config, db):
            return
        await event.respond(f"chat_id: `{event.chat_id}`")

    @client.on(events.NewMessage(pattern=r"^/setvip(?:@\w+)?(?:\s+(.+))?$"))
    async def set_vip(event):
        if not await require_admin_logchat(event, config, db):
            return
        raw_value = event.pattern_match.group(1)
        if not raw_value:
            await event.respond("Format: `/setvip <chat_id>` atau `/setvip here`")
            return
        try:
            chat_id_value = parse_chat_setting(event, raw_value)
            await db.set_setting("vip_chat_id", chat_id_value)
            await event.respond(f"VIP chat default diset ke `{chat_id_value}`.")
            await send_log(client, config, db, f"<b>Config updated</b>\n<code>vip_chat_id={chat_id_value}</code>")
        except Exception as exc:
            LOGGER.exception("Failed to set VIP chat")
            await event.respond("Gagal set VIP chat.")

    @client.on(events.NewMessage(pattern=r"^/setlog(?:@\w+)?(?:\s+(.+))?$"))
    async def set_log(event):
        if not await require_admin_logchat(event, config, db):
            return
        raw_value = event.pattern_match.group(1)
        if not raw_value:
            await event.respond("Format: `/setlog <chat_id>` atau `/setlog here`")
            return
        try:
            chat_id_value = parse_chat_setting(event, raw_value)
            await db.set_setting("log_chat_id", chat_id_value)
            await event.respond(f"Log chat diset ke `{chat_id_value}`.")
            await send_log(client, config, db, f"<b>Config updated</b>\n<code>log_chat_id={chat_id_value}</code>")
        except Exception as exc:
            LOGGER.exception("Failed to set log chat")
            await event.respond("Gagal set log chat.")

    @client.on(events.NewMessage(pattern=r"^/config(?:@\w+)?$"))
    async def show_config(event):
        if not await require_admin_logchat(event, config, db):
            return
        vip_chat_id = await runtime_vip_chat_id(config, db)
        log_chat_id = await runtime_log_chat_id(config, db)
        total_bots = len(bot_manager.active_bots) if bot_manager else 1
        await event.respond(
            "Config aktif:\n"
            f"VIP_CHAT_ID: `{vip_chat_id or 'belum diset'}`\n"
            f"LOG_CHAT_ID: `{log_chat_id or 'belum diset'}`\n"
            f"ACTIVE_BOTS: `{total_bots}`\n"
            f"PAYMENT_AMOUNT: `{config.payment_amount}`\n"
            f"INVITE_EXPIRE_HOURS: `{config.invite_expire_hours}`"
        )

    @client.on(events.NewMessage(pattern=r"^/commands?(?:@\w+)?$"))
    async def commands(event):
        if not await require_admin_logchat(event, config, db):
            return
        await event.respond(admin_command_list_text(), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/set_broadcast(?:@\w+)?(?:\s+(\S+))?$"))
    async def set_broadcast(event):
        if not await require_admin_logchat(event, config, db):
            return
        bot_code = (event.pattern_match.group(1) or "default").strip()
        replied = await event.get_reply_message()
        if not replied:
            await event.respond(
                f"Balas (reply) ke pesan yang mau dijadikan broadcast untuk bot <b>{html.escape(bot_code)}</b>.\n"
                f"Contoh: reply pesan lalu kirim <code>/set_broadcast {html.escape(bot_code)}</code>",
                parse_mode="html",
            )
            return
        text = replied.raw_text or ""
        media_file_id = getattr(getattr(replied, "file", None), "id", "") or ""
        media_type = getattr(getattr(replied, "file", None), "mime_type", "") or ""
        if not text and not media_file_id:
            await event.respond("Pesan harus memiliki teks atau media.")
            return
        saved = await db.set_broadcast_message(
            text, media_file_id, media_type, entities_to_json(replied.entities or []), bot_code=bot_code
        )
        await event.respond(
            f"✅ <b>Pesan broadcast untuk bot <code>{html.escape(bot_code)}</code> berhasil disimpan!</b>\n\n"
            f"• Uji coba: <code>/test_broadcast {html.escape(bot_code)}</code>\n"
            f"• Atur jadwal harian: <code>/set_broadcasttime {html.escape(bot_code)} 09:00</code>",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/set_broadcasttime(?:@\w+)?(?:\s+(.+))?$"))
    async def set_broadcast_time(event):
        if not await require_admin_logchat(event, config, db):
            return
        raw_value = (event.pattern_match.group(1) or "").strip()
        from vip_bot.helpers import validate_broadcast_time
        parts = raw_value.split()
        
        if len(parts) == 1:
            val = parts[0].lower()
            if val in BROADCAST_DISABLED_VALUES or BROADCAST_TIME_PATTERN.fullmatch(val):
                bot_code = "default"
                raw_time = val
            else:
                await event.respond(
                    "Format:\n"
                    "• <code>/set_broadcasttime &lt;bot_code&gt; HH:MM</code> (contoh: <code>/set_broadcasttime botpayment1 09:00</code>)\n"
                    "• <code>/set_broadcasttime &lt;bot_code&gt; off</code>",
                    parse_mode="html",
                )
                return
        elif len(parts) >= 2:
            bot_code = parts[0]
            raw_time = parts[1]
        else:
            await event.respond(
                "Format:\n"
                "• <code>/set_broadcasttime &lt;bot_code&gt; HH:MM</code> (contoh: <code>/set_broadcasttime botpayment1 09:00</code>)\n"
                "• <code>/set_broadcasttime &lt;bot_code&gt; off</code>",
                parse_mode="html",
            )
            return

        try:
            time_value = validate_broadcast_time(raw_time)
        except ValueError:
            await event.respond("Format jam salah. Gunakan `HH:MM` (contoh `09:30`) atau `off`.")
            return

        await db.set_broadcast_time(time_value, bot_code=bot_code)
        await db.set_last_broadcast_date("", bot_code=bot_code)
        if not time_value:
            await event.respond(f"✅ Broadcast otomatis untuk bot <b>{html.escape(bot_code)}</b> dinonaktifkan.", parse_mode="html")
            return
        await event.respond(
            f"✅ Broadcast otomatis untuk bot <b>{html.escape(bot_code)}</b> dijadwalkan setiap <b>{html.escape(time_value)} WIB</b>.",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/test_broadcast(?:@\w+)?(?:\s+(\S+))?$"))
    async def test_broadcast(event):
        if not await require_admin_logchat(event, config, db):
            return
        bot_code = (event.pattern_match.group(1) or "default").strip()
        broadcast_message = await db.get_active_broadcast_message(bot_code=bot_code)
        if not broadcast_message:
            await event.respond(
                f"Belum ada broadcast yang disimpan untuk bot <b>{html.escape(bot_code)}</b>.\n"
                f"Balas (reply) ke pesan lalu ketik <code>/set_broadcast {html.escape(bot_code)}</code> terlebih dahulu.",
                parse_mode="html",
            )
            return
        admin_ids = sorted(config.admin_user_ids)
        if not admin_ids:
            admin_ids = [event.sender_id]
        
        target_client = bot_manager.get_client(bot_code) if (bot_manager and hasattr(bot_manager, "get_client")) else client
        totals = await send_broadcast_batch(target_client, db, broadcast_message, admin_ids, config.qris_create_concurrency, bot_code=bot_code)
        await event.respond(
            f"✅ <b>Test Broadcast Selesai [{html.escape(bot_code)}]</b>\n"
            f"• Terkirim ke Admin: <code>{totals['sent']}/{len(admin_ids)}</code>\n"
            f"• Blocked: <code>{totals['blocked']}</code>\n"
            f"• Deactivated: <code>{totals['deactivated']}</code>\n"
            f"• Error: <code>{totals['error']}</code>",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/broadcast_(?:status|info|list)(?:@\w+)?(?:\s+(\S+))?$"))
    async def broadcast_status_cmd(event):
        if not await require_admin_logchat(event, config, db):
            return
        requested_bot = (event.pattern_match.group(1) or "").strip()
        if requested_bot:
            bots_to_show = [requested_bot]
        else:
            bots_to_show = ["default"]
            try:
                active = await db.list_all_bots()
                for b in active:
                    if b["bot_code"] not in bots_to_show:
                        bots_to_show.append(b["bot_code"])
            except Exception:
                pass

        lines = ["📢 <b>Status Konfigurasi Broadcast Multi-Bot:</b>\n"]
        for b_code in bots_to_show:
            b_time = await db.get_broadcast_time(bot_code=b_code) or "OFF"
            last_date = await db.get_last_broadcast_date(bot_code=b_code) or "-"
            msg = await db.get_active_broadcast_message(bot_code=b_code)
            user_count = await db.count_broadcast_targets(bot_code=b_code)
            
            if msg:
                msg_status = "✅ Ada"
                if msg.get("media_type"):
                    msg_status += f" ({msg['media_type']})"
            else:
                msg_status = "❌ Belum diset"

            lines.append(
                f"🤖 <b>{html.escape(b_code)}</b>:\n"
                f"• Jadwal: <b>{html.escape(b_time)} WIB</b>\n"
                f"• Pesan: {msg_status}\n"
                f"• Terakhir Kirim: <code>{html.escape(last_date)}</code>\n"
                f"• Total User: <b>{user_count} orang</b>\n"
            )
        await event.respond("\n".join(lines), parse_mode="html")
