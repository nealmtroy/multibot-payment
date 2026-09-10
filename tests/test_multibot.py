import sys
from pathlib import Path

# Ensure MultiBot_Payment is first in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock
from vip_bot.handlers.admin import parse_package_add_args
from vip_bot.messages import bot_list_text, package_list_text, package_buttons, default_package, admin_command_list_text
from vip_bot.bot_manager import BotManager, BotInstance


class TestMultiBot(unittest.TestCase):
    def test_parse_package_add_args_multibot(self):
        # Format: /package_add <bot_code> <kode> <Nama Group>|<chat_id>|<harga>
        bot_code, code, name, chat_id, amount = parse_package_add_args("botpayment1 vip1 Group VIP 1|-1001111111111|50000")
        self.assertEqual(bot_code, "botpayment1")
        self.assertEqual(code, "vip1")
        self.assertEqual(name, "Group VIP 1")
        self.assertEqual(chat_id, -1001111111111)
        self.assertEqual(amount, 50000)

    def test_parse_package_add_args_default_bot(self):
        # Format with default bot: /package_add default <kode> <Nama Group>|<chat_id>|<harga>
        bot_code, code, name, chat_id, amount = parse_package_add_args("default vip1 Group VIP 1|-1001111111111|50000")
        self.assertEqual(bot_code, "default")
        self.assertEqual(code, "vip1")
        self.assertEqual(name, "Group VIP 1")
        self.assertEqual(chat_id, -1001111111111)
        self.assertEqual(amount, 50000)

    def test_bot_list_text(self):
        bots = [
            {"bot_code": "botpayment1", "bot_username": "pay1_bot", "bot_name": "botpayment1", "status": "online", "package_count": 3},
            {"bot_code": "botpayment2", "bot_username": "pay2_bot", "bot_name": "botpayment2", "status": "stopped", "package_count": 2},
        ]
        text = bot_list_text(bots)
        self.assertIn("botpayment1", text)
        self.assertIn("@pay1_bot", text)

    def test_package_buttons_per_bot(self):
        store = MagicMock()
        store.list_packages.return_value = [
            {"code": "vip1", "name": "VIP 1", "amount": 50000, "bot_code": "botpayment1"},
            {"code": "vip2", "name": "VIP 2", "amount": 75000, "bot_code": "botpayment1"},
        ]
        config = MagicMock()
        buttons = package_buttons(config, store, bot_code="botpayment1")
        store.list_packages.assert_called_once_with(bot_code="botpayment1")
        self.assertEqual(len(buttons), 2)

    def test_bot_manager_resolve_client(self):
        config = MagicMock()
        store = MagicMock()
        bm = BotManager(config, store, None, {}, {})
        master_client = MagicMock()
        child_client = MagicMock()
        child_client.is_connected.return_value = True

        bm.set_master_client(master_client)
        self.assertEqual(bm.get_client("default"), master_client)
        self.assertEqual(bm.get_client("master"), master_client)
        self.assertEqual(bm.get_client("unknown_bot"), master_client)

        bm.active_bots["botpayment1"] = BotInstance(
            bot_code="botpayment1",
            bot_token="123:abc",
            bot_username="pay1_bot",
            bot_name="botpayment1",
            client=child_client,
            task=MagicMock(),
            status="active",
        )
        self.assertEqual(bm.get_client("botpayment1"), child_client)

    def test_admin_commands_include_per_bot_broadcast(self):
        text = admin_command_list_text()
        self.assertIn("/set_broadcast [nama_bot]", text)
        self.assertIn("/set_broadcasttime &lt;nama_bot&gt;", text)
        self.assertIn("/test_broadcast [nama_bot]", text)
        self.assertIn("/broadcast_status [nama_bot]", text)


    def test_broadcast_time_per_bot_keying(self):
        import asyncio
        from vip_bot.db import Database

        # Test key resolution logic
        db = Database.__new__(Database)
        storage = {}

        async def fake_set_setting(key, val):
            storage[key] = str(val)

        async def fake_get_setting(key, default=""):
            return storage.get(key, default)

        db.set_setting = fake_set_setting
        db.get_setting = fake_get_setting

        async def run_scenario():
            # Set broadcast time for bot1
            await db.set_broadcast_time("09:00", bot_code="bot1")
            # Set broadcast time for bot2
            await db.set_broadcast_time("14:30", bot_code="bot2")

            # Verify isolated keys
            self.assertEqual(await db.get_broadcast_time("bot1"), "09:00")
            self.assertEqual(await db.get_broadcast_time("bot2"), "14:30")
            self.assertEqual(await db.get_broadcast_time("bot3"), "")

        asyncio.run(run_scenario())


if __name__ == "__main__":
    unittest.main()
