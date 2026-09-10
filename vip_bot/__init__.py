import asyncio
import logging
from telethon import TelegramClient
from vip_bot.config import load_config
from vip_bot.db_store import PaymentStore
from vip_bot.helpers import send_log
from vip_bot.loops import polling_loop, broadcast_loop
from vip_bot.handlers import register_handlers
from vip_bot.bot_manager import BotManager

LOGGER = logging.getLogger("telegram_vip_bot")


async def start_bot():
    config = load_config()
    store = PaymentStore(config)
    
    # Inisialisasi Master Client (untuk handle admin commands di LOG_CHAT_ID)
    master_client = TelegramClient("vip_bot_master", config.api_id, config.api_hash)
    qris_semaphore = asyncio.Semaphore(config.qris_create_concurrency)
    user_locks = {}
    withdrawal_states = {}

    # Inisialisasi BotManager
    bot_manager = BotManager(
        config=config,
        store=store,
        qris_semaphore=qris_semaphore,
        user_locks=user_locks,
        withdrawal_states=withdrawal_states,
    )
    bot_manager.set_master_client(master_client)

    # Daftarkan handlers di Master Client (termasuk admin bot management)
    register_handlers(
        master_client,
        config,
        store,
        qris_semaphore,
        user_locks,
        withdrawal_states,
        bot_manager=bot_manager,
        bot_code="default",
    )

    await master_client.start(bot_token=config.bot_token)
    me = await master_client.get_me()
    master_client.bot_code = "default"
    master_client.bot_username = getattr(me, "username", "") or ""
    master_client.bot_name = "Master Bot"

    LOGGER.info("Master bot started (@%s)", master_client.bot_username)
    await send_log(master_client, config, store, f"<b>MultiBot Payment System started</b>\nMaster Bot: @{master_client.bot_username}")

    # Otomatis nyalakan semua child bots yang terdaftar dan aktif di Supabase
    await bot_manager.start_all_from_db()

    # Jalankan centralized polling loop & broadcast loop
    asyncio.create_task(polling_loop(bot_manager, config, store))
    asyncio.create_task(broadcast_loop(bot_manager, config, store))

    await master_client.run_until_disconnected()


def run():
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass
