import asyncio
import html
import logging
from telethon import TelegramClient
from vip_bot.config import load_config
from vip_bot.db import Database
from vip_bot.helpers import send_log
from vip_bot.loops import polling_loop, broadcast_loop
from vip_bot.handlers.admin import register_admin_handlers
from vip_bot.bot_manager import BotManager

LOGGER = logging.getLogger("telegram_vip_bot")


async def start_bot():
    config = load_config()
    db = await Database.create(config.database_url)
    await db.init_schema()

    master_client = TelegramClient("vip_bot_master", config.api_id, config.api_hash)
    qris_semaphore = asyncio.Semaphore(config.qris_create_concurrency)
    user_locks = {}
    withdrawal_states = {}

    bot_manager = BotManager(
        config=config,
        db=db,
        qris_semaphore=qris_semaphore,
        user_locks=user_locks,
        withdrawal_states=withdrawal_states,
    )
    bot_manager.set_master_client(master_client)

    # Master bot is exclusively the Management Bot (Admin only)
    register_admin_handlers(
        master_client,
        config,
        db,
        qris_semaphore,
        user_locks,
        bot_manager=bot_manager,
    )

    await master_client.start(bot_token=config.bot_token)
    me = await master_client.get_me()
    master_client.bot_code = "master"
    master_client.bot_username = getattr(me, "username", "") or ""
    master_client.bot_name = "Management Bot"

    LOGGER.info("Management Bot started (@%s)", master_client.bot_username)

    # Auto-start all registered payment bots from database
    await bot_manager.start_all_from_db()

    # Formulate startup notification message
    bots = await bot_manager.list_all()
    active_bots = [b for b in bots if b["status"] == "online"]

    if active_bots:
        bot_lines = []
        for idx, b in enumerate(active_bots, 1):
            uname = f"(@{b['bot_username']})" if b.get("bot_username") else ""
            bot_lines.append(
                f"{idx}. 🟢 <b>{html.escape(b['bot_code'])}</b> {uname} - Paket VIP: <b>{b['package_count']}</b>"
            )
        bot_list_str = "\n".join(bot_lines)
        startup_msg = (
            "🚀 <b>MultiBot Payment has been started!</b>\n\n"
            "<b>Bot yang berjalan:</b>\n"
            f"{bot_list_str}"
        )
    else:
        startup_msg = (
            "🚀 <b>MultiBot Payment has been started!</b>\n\n"
            "<i>Tidak ada bot yang aktif saat ini.</i>\n\n"
            f"Gunakan menu <b>➕ Tambah Bot Baru</b> di Private Chat @{master_client.bot_username} untuk menambahkan bot baru."
        )

    await send_log(master_client, config, db, startup_msg)

    asyncio.create_task(polling_loop(bot_manager, config, db))
    asyncio.create_task(broadcast_loop(bot_manager, config, db))

    try:
        await master_client.run_until_disconnected()
    finally:
        LOGGER.info("Shutting down MultiBot Payment engine...")
        for code in list(bot_manager.active_bots.keys()):
            try:
                await bot_manager.stop_bot(code, update_db=False)
            except Exception as exc:
                LOGGER.warning("Error stopping bot %s during shutdown: %s", code, exc)
        await db.close()
        LOGGER.info("Shutdown complete.")


def run():
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass
