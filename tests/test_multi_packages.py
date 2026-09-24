import sys
from pathlib import Path

# Ensure MultiBot_Payment is first in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from vip_bot.messages import (
    cart_package_buttons,
    qris_caption_multi,
    paid_message_multi,
    paid_message_buttons_multi,
)
from vip_bot.helpers import create_package_invite_link
from vip_bot.loops import process_paid_payment


def get_button_data(btn):
    if hasattr(btn, "data"):
        return btn.data
    if hasattr(btn, "type") and hasattr(btn.type, "data"):
        return btn.type.data
    return getattr(btn, "_bytes", None)


def get_button_style(btn):
    style_obj = getattr(btn, "style", None)
    if style_obj is None and hasattr(btn, "type"):
        style_obj = getattr(btn.type, "style", None)
    if style_obj:
        if getattr(style_obj, "bg_success", False):
            return "success"
        if getattr(style_obj, "bg_danger", False):
            return "danger"
        if getattr(style_obj, "bg_primary", False):
            return "primary"
    return None


class TestMultiPackageFeatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = MagicMock()
        self.config.payment_amount = 50000
        self.config.vip_chat_id = -1001111111111
        self.config.invite_expire_hours = 24

        self.pkgs = [
            {"code": "vip1", "name": "VIP Anime", "amount": 5000, "vip_chat_id": -1001111111111, "invite_expire_hours": 24},
            {"code": "vip2", "name": "VIP Movie", "amount": 5000, "vip_chat_id": -1002222222222, "invite_expire_hours": 48},
            {"code": "vip3", "name": "VIP Series", "amount": 10000, "vip_chat_id": -1003333333333, "invite_expire_hours": 24},
        ]

    def test_package_buttons_normal_and_multi(self):
        from vip_bot.messages import package_buttons
        # Normal without multi button
        btns_normal = package_buttons(self.config, self.pkgs, include_multi_button=False)
        self.assertEqual(len(btns_normal), 3)
        self.assertEqual(get_button_data(btns_normal[0][0]), b"pkg:vip1")

        # Normal with multi button
        btns_multi = package_buttons(self.config, self.pkgs, include_multi_button=True)
        # 3 packages + 1 buy all + 1 choose multiple = 5 rows
        self.assertEqual(len(btns_multi), 5)
        self.assertEqual(get_button_data(btns_multi[3][0]), b"cart_buy_all")
        self.assertIn("Beli Semua Paket", btns_multi[3][0].text)
        self.assertEqual(get_button_style(btns_multi[3][0]), "success")

        self.assertEqual(get_button_data(btns_multi[4][0]), b"cart_mode_start")
        self.assertIn("Pilih Beberapa Paket", btns_multi[4][0].text)
        self.assertEqual(get_button_style(btns_multi[4][0]), "success")

    def test_cart_package_buttons_empty(self):
        buttons = cart_package_buttons(self.config, self.pkgs, selected_codes=set())
        # 3 package rows + 1 action row + 1 back row = 5 rows
        self.assertEqual(len(buttons), 5)
        # Checkbox unchecked
        self.assertTrue(buttons[0][0].text.startswith("⬜ "))
        self.assertIn("VIP Anime", buttons[0][0].text)
        self.assertEqual(get_button_data(buttons[0][0]), b"cart_toggle:vip1")
        # Bottom action button
        self.assertEqual(get_button_data(buttons[3][0]), b"cart_checkout")
        self.assertIn("Pilih paket di atas", buttons[3][0].text)
        # Back button
        self.assertEqual(get_button_data(buttons[4][0]), b"cart_mode_back")
        self.assertIn("Kembali", buttons[4][0].text)
        self.assertEqual(get_button_style(buttons[4][0]), "danger")

    def test_cart_package_buttons_selected(self):
        buttons = cart_package_buttons(self.config, self.pkgs, selected_codes={"vip1", "vip2"})
        # 3 package rows + 1 checkout + 1 back = 5 rows (reset button removed)
        self.assertEqual(len(buttons), 5)
        # vip1 checked
        self.assertTrue(buttons[0][0].text.startswith("✅ "))
        # vip2 checked
        self.assertTrue(buttons[1][0].text.startswith("✅ "))
        # vip3 unchecked
        self.assertTrue(buttons[2][0].text.startswith("⬜ "))

        # Checkout button text shows 2 Paket (Rp10.000) with style success
        checkout_btn = buttons[3][0]
        self.assertEqual(get_button_data(checkout_btn), b"cart_checkout")
        self.assertIn("Bayar 2 Paket", checkout_btn.text)
        self.assertIn("10.000", checkout_btn.text)
        self.assertEqual(get_button_style(checkout_btn), "success")

        # Back button with style danger
        back_btn = buttons[4][0]
        self.assertEqual(get_button_data(back_btn), b"cart_mode_back")
        self.assertIn("Kembali", back_btn.text)
        self.assertEqual(get_button_style(back_btn), "danger")

    def test_qris_caption_multi(self):
        caption = qris_caption_multi(
            [self.pkgs[0], self.pkgs[1]],
            inv_id="VIP-260924-ABC123",
            checkout_amount=10000,
            final_amount="Rp10.035",
            expires="2026-09-24T12:00:00Z",
        )
        self.assertIn("Akses 2 Group VIP", caption)
        self.assertIn("VIP-260924-ABC123", caption)
        self.assertIn("Total 2 Paket", caption)
        self.assertIn("10.000", caption)
        self.assertIn("VIP Anime", caption)
        self.assertIn("VIP Movie", caption)
        self.assertIn("Rp10.035", caption)

    def test_paid_message_multi(self):
        pkgs_with_links = [
            {
                "code": "vip1",
                "name": "VIP Anime",
                "amount": 5000,
                "vip_chat_id": -1001111111111,
                "invite_link": "https://t.me/+link_anime",
            },
            {
                "code": "vip2",
                "name": "VIP Movie",
                "amount": 5000,
                "vip_chat_id": -1002222222222,
                "invite_link": "https://t.me/+link_movie",
            },
        ]
        msg = paid_message_multi(pkgs_with_links, invite_hours=24)
        self.assertIn("Akses <b>2 Group VIP</b> kamu sudah aktif", msg)
        self.assertIn("1. VIP Anime", msg)
        self.assertIn("https://t.me/+link_anime", msg)
        self.assertIn("2. VIP Movie", msg)
        self.assertIn("https://t.me/+link_movie", msg)
        self.assertIn("24 jam", msg)

    def test_paid_message_buttons_multi(self):
        pkgs = [
            {"name": "VIP Anime", "vip_chat_id": -1001111111111},
            {"name": "VIP Movie", "vip_chat_id": -1002222222222},
        ]
        buttons = paid_message_buttons_multi(pkgs)
        self.assertIsNotNone(buttons)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0][0].text, "Buka VIP Anime")
        self.assertEqual(buttons[1][0].text, "Buka VIP Movie")

    async def test_create_package_invite_link(self):
        client = AsyncMock()
        mock_result = MagicMock()
        mock_result.link = "https://t.me/+mock_invite_link"
        client.return_value = mock_result
        db = MagicMock()

        pkg = {"code": "vip1", "name": "VIP Anime", "vip_chat_id": -1001234567, "invite_expire_hours": 12}
        link, exp = await create_package_invite_link(client, self.config, db, pkg, "INV-123")
        self.assertEqual(link, "https://t.me/+mock_invite_link")
        self.assertTrue(bool(exp))
        client.assert_called_once()

    async def test_process_paid_payment_multi(self):
        client = AsyncMock()
        mock_result = MagicMock()
        mock_result.link = "https://t.me/+mock_link"
        client.return_value = mock_result

        db = MagicMock()
        db.claim_paid_processing = AsyncMock(return_value=True)
        db.mark_delivery_processing = AsyncMock(return_value=True)
        db.mark_delivery_done = AsyncMock()
        db.update_payment_packages = AsyncMock()
        db.get_setting = AsyncMock(return_value=None)
        db.pending_referral_for_user = AsyncMock(return_value=None)

        import json
        payment = {
            "inv_id": "INV-TEST-001",
            "bot_code": "default",
            "user_id": 998877,
            "status": "pending",
            "amount": 10000,
            "package_amount": 10000,
            "packages_json": json.dumps(self.pkgs[:2]),
        }

        with patch("vip_bot.loops.delete_qris_message", new_callable=AsyncMock), \
             patch("vip_bot.loops.safe_send_user", new_callable=AsyncMock) as mock_send, \
             patch("vip_bot.loops.send_log", new_callable=AsyncMock):
            mock_send.return_value = "sent"
            await process_paid_payment(client, self.config, db, payment)

            # Claim was called
            db.claim_paid_processing.assert_called_once_with("INV-TEST-001")
            # mark_delivery_processing was called
            db.mark_delivery_processing.assert_called_once()
            # safe_send_user was called with multi message
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][4]
            self.assertIn("Akses <b>2 Group VIP</b>", call_text)
            self.assertIn("1. VIP Anime", call_text)
            self.assertIn("2. VIP Movie", call_text)
            # mark_delivery_done was called
            db.mark_delivery_done.assert_called_once_with("INV-TEST-001")

    def test_serialize_package_dict_handles_datetime(self):
        import datetime as dt
        from vip_bot.helpers import serialize_package_dict
        pkg = {
            "id": 1,
            "code": "vip1",
            "name": "VIP 1",
            "amount": 5000,
            "vip_chat_id": -100123456,
            "invite_expire_hours": 24,
            "created_at": dt.datetime.now(),
            "updated_at": dt.datetime.now(),
        }
        clean = serialize_package_dict(pkg)
        self.assertNotIn("created_at", clean)
        self.assertNotIn("updated_at", clean)
        self.assertEqual(clean["code"], "vip1")
        self.assertEqual(clean["amount"], 5000)

        import json
        dumped = json.dumps([clean], default=str)
        self.assertIn("vip1", dumped)


if __name__ == "__main__":
    unittest.main()
