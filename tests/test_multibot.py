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


    def test_admin_reply_keyboards_structure(self):
        from vip_bot.messages import (
            admin_main_menu_keyboard,
            admin_bot_menu_keyboard,
            admin_package_menu_keyboard,
            admin_broadcast_menu_keyboard,
            cancel_keyboard,
        )

        main_kb = admin_main_menu_keyboard()
        self.assertEqual(len(main_kb), 3)
        self.assertEqual(main_kb[0][0].button.text, "🤖 Kelola Bot Payment")
        self.assertEqual(main_kb[0][1].button.text, "📦 Kelola Paket VIP")
        self.assertEqual(main_kb[1][0].button.text, "📢 Kelola Broadcast")
        self.assertEqual(main_kb[1][1].button.text, "💰 Antrean Penarikan")

        bot_kb = admin_bot_menu_keyboard()
        self.assertEqual(bot_kb[0][0].button.text, "➕ Tambah Bot Baru")
        self.assertEqual(bot_kb[0][1].button.text, "📋 Daftar Semua Bot")

        pkg_kb = admin_package_menu_keyboard()
        self.assertEqual(pkg_kb[0][0].button.text, "➕ Tambah Paket VIP")

        bc_kb = admin_broadcast_menu_keyboard()
        self.assertEqual(bc_kb[0][0].button.text, "📝 Set Pesan Broadcast")

        c_kb = cancel_keyboard()
        self.assertEqual(c_kb[0][0].button.text, "❌ Batal")

    def test_package_buttons_custom_styles_and_labels(self):
        pkgs = [
            {"code": "vip1", "name": "VIP 1 Bulan", "amount": 50000, "button_label": "MV 1", "button_style": "primary"},
            {"code": "vip2", "name": "VIP 2 Bulan", "amount": 75000, "button_label": "MV 2", "button_style": "success"},
            {"code": "vip3", "name": "VIP 3 Bulan", "amount": 100000, "button_label": "MV 3", "button_style": "danger"},
            {"code": "vip4", "name": "VIP 4 Bulan", "amount": 150000, "button_label": "", "button_style": "default"},
        ]
        config = MagicMock()
        # Default 1 column
        buttons_1col = package_buttons(config, pkgs, columns=1)
        self.assertEqual(len(buttons_1col), 4)
        # Check MV 1 button text and primary style
        self.assertEqual(buttons_1col[0][0].text, "MV 1")
        self.assertTrue(getattr(buttons_1col[0][0].style, "bg_primary", False))

        # Check MV 2 button text and success style
        self.assertEqual(buttons_1col[1][0].text, "MV 2")
        self.assertTrue(getattr(buttons_1col[1][0].style, "bg_success", False))

        # Check MV 3 button text and danger style
        self.assertEqual(buttons_1col[2][0].text, "MV 3")
        self.assertTrue(getattr(buttons_1col[2][0].style, "bg_danger", False))

        # Check MV 4 fallback text (no custom label) and default style (no color flags enabled)
        self.assertIn("VIP 4 Bulan", buttons_1col[3][0].text)
        self.assertFalse(getattr(buttons_1col[3][0].style, "bg_primary", False))
        self.assertFalse(getattr(buttons_1col[3][0].style, "bg_danger", False))
        self.assertFalse(getattr(buttons_1col[3][0].style, "bg_success", False))

    def test_package_buttons_grid_columns_2x2(self):
        # 4 VIP packages with 2 columns:
        # Row 0: [MV 1 | MV 2]
        # Row 1: [MV 3 | MV 4]
        pkgs = [
            {"code": "vip1", "name": "VIP 1", "amount": 50000, "button_label": "MV 1"},
            {"code": "vip2", "name": "VIP 2", "amount": 75000, "button_label": "MV 2"},
            {"code": "vip3", "name": "VIP 3", "amount": 100000, "button_label": "MV 3"},
            {"code": "vip4", "name": "VIP 4", "amount": 150000, "button_label": "MV 4"},
        ]
        config = MagicMock()
        buttons_2cols = package_buttons(config, pkgs, columns=2)
        self.assertEqual(len(buttons_2cols), 2)
        self.assertEqual(len(buttons_2cols[0]), 2)
        self.assertEqual(len(buttons_2cols[1]), 2)

        self.assertEqual(buttons_2cols[0][0].text, "MV 1")
        self.assertEqual(buttons_2cols[0][1].text, "MV 2")
        self.assertEqual(buttons_2cols[1][0].text, "MV 3")
        self.assertEqual(buttons_2cols[1][1].text, "MV 4")

    def test_update_package_field_validation(self):
        import asyncio
        from vip_bot.db import Database

        db = Database.__new__(Database)

        # Testing field validation error on disallowed field
        with self.assertRaises(ValueError):
            asyncio.run(db.update_package_field("default", "vip1", "invalid_field_xyz", "val"))

    def test_package_buttons_explicit_row_index(self):
        # When explicit row_index is set:
        # Row 1: [vip1, vip2]
        # Row 2: [vip3]
        pkgs = [
            {"code": "vip1", "name": "VIP 1", "amount": 50000, "row_index": 1},
            {"code": "vip2", "name": "VIP 2", "amount": 75000, "row_index": 1},
            {"code": "vip3", "name": "VIP 3", "amount": 100000, "row_index": 2},
        ]
        config = MagicMock()
        buttons = package_buttons(config, pkgs)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(len(buttons[1]), 1)

    def test_package_detail_card(self):
        from vip_bot.messages import package_detail_card
        pkg = {
            "code": "vip1",
            "name": "Super VIP",
            "amount": 50000,
            "vip_chat_id": -100123456,
            "invite_expire_hours": 24,
            "button_style": "primary",
            "button_label": "MV 1",
            "sort_order": 10,
            "active": True,
            "bot_code": "botpayment1",
        }
        card = package_detail_card(pkg)
        self.assertIn("Super VIP", card)
        self.assertIn("50.000", card)
        self.assertIn("MV 1", card)
        self.assertIn("Primary", card)
        self.assertIn("botpayment1", card)


if __name__ == "__main__":
    unittest.main()

