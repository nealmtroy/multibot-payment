import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from telethon import TelegramClient

LOGGER = logging.getLogger("telegram_vip_bot.bot_manager")


@dataclass
class BotInstance:
    bot_code: str
    bot_token: str
    bot_username: str
    bot_name: str
    client: TelegramClient
    task: asyncio.Task
    status: str


class BotManager:
    def __init__(self, config, db, qris_semaphore, user_locks, withdrawal_states):
        self.config = config
        self.db = db
        self.qris_semaphore = qris_semaphore
        self.user_locks = user_locks
        self.withdrawal_states = withdrawal_states
        self.active_bots: dict[str, BotInstance] = {}
        self.master_client: TelegramClient | None = None
        self.sessions_dir = Path("sessions")
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def set_master_client(self, client: TelegramClient):
        self.master_client = client

    def get_client(self, bot_code: str) -> TelegramClient | None:
        if not bot_code or bot_code in ("default", "master"):
            return self.master_client
        instance = self.active_bots.get(bot_code)
        if instance and instance.client and instance.client.is_connected():
            return instance.client
        return self.master_client

    async def spawn_bot(self, bot_data: dict) -> dict:
        from vip_bot.handlers.user import register_user_handlers
        bot_code = bot_data["bot_code"]
        bot_token = bot_data["bot_token"]
        bot_name = bot_data.get("bot_name") or bot_code

        # Stop existing instance if already running
        if bot_code in self.active_bots:
            await self.stop_bot(bot_code)

        session_path = str(self.sessions_dir / f"bot_{bot_code}")
        client = TelegramClient(session_path, self.config.api_id, self.config.api_hash)

        await client.start(bot_token=bot_token)
        me = await client.get_me()
        bot_username = getattr(me, "username", "") or ""
        client.bot_code = bot_code
        client.bot_username = bot_username
        client.bot_name = bot_name
        client.master_client = self.master_client

        # Register user handlers for this bot instance
        register_user_handlers(
            client,
            self.config,
            self.db,
            self.qris_semaphore,
            self.user_locks,
            self.withdrawal_states,
            bot_code=bot_code,
        )

        task = asyncio.create_task(client.run_until_disconnected())
        instance = BotInstance(
            bot_code=bot_code,
            bot_token=bot_token,
            bot_username=bot_username,
            bot_name=bot_name,
            client=client,
            task=task,
            status="active",
        )
        self.active_bots[bot_code] = instance

        # Sync to PostgreSQL
        await self.db.upsert_bot(
            bot_code=bot_code,
            bot_token=bot_token,
            bot_username=bot_username,
            bot_name=bot_name,
            status="active",
        )

        LOGGER.info("Bot %s (@%s) spawned successfully", bot_code, bot_username)
        return {
            "bot_code": bot_code,
            "bot_username": bot_username,
            "bot_name": bot_name,
            "status": "active",
        }

    async def stop_bot(self, bot_code: str) -> bool:
        instance = self.active_bots.get(bot_code)
        if not instance:
            await self.db.set_bot_status(bot_code, "stopped")
            return False

        try:
            if instance.client.is_connected():
                await instance.client.disconnect()
        except Exception as exc:
            LOGGER.warning("Error disconnecting bot %s: %s", bot_code, exc)

        try:
            if not instance.task.done():
                instance.task.cancel()
        except Exception as exc:
            LOGGER.warning("Error cancelling task for bot %s: %s", bot_code, exc)

        del self.active_bots[bot_code]
        await self.db.set_bot_status(bot_code, "stopped")
        LOGGER.info("Bot %s stopped successfully", bot_code)
        return True

    async def delete_bot(self, bot_code: str) -> bool:
        await self.stop_bot(bot_code)
        return await self.db.delete_bot(bot_code)

    async def start_all_from_db(self):
        try:
            bots = await self.db.list_active_bots()
        except Exception as exc:
            LOGGER.warning("Failed to load active bots from DB: %s", exc)
            return

        for bot in bots:
            try:
                LOGGER.info("Auto-starting bot %s from DB...", bot["bot_code"])
                await self.spawn_bot(bot)
            except Exception as exc:
                LOGGER.error("Failed to spawn bot %s: %s", bot.get("bot_code"), exc)

    async def list_all(self) -> list[dict]:
        db_bots = {b["bot_code"]: b for b in (await self.db.list_all_bots())}
        result = []
        for code, b in db_bots.items():
            is_online = code in self.active_bots and self.active_bots[code].client.is_connected()
            packages = await self.db.list_packages(bot_code=code)
            result.append({
                "bot_code": code,
                "bot_username": b.get("bot_username") or "",
                "bot_name": b.get("bot_name") or code,
                "status": "online" if is_online else b.get("status", "stopped"),
                "package_count": len(packages),
            })
        return result
