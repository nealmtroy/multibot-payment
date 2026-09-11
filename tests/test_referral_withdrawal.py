import unittest
from types import SimpleNamespace

from telethon.tl.types import MessageEntityBold, MessageEntityItalic, MessageEntityTextUrl

from vip_bot.messages import (
    admin_command_list_text,
    main_menu_keyboard_text,
    main_menu_button_labels,
    paid_message,
)
from vip_bot.helpers import (
    entities_to_json,
    format_referral_code,
    referral_user_log_text,
    parse_referral_payload,
    parse_withdrawal_amount,
    internal_telegram_chat_url,
    referral_commission,
    should_create_referral,
    updated_referral_counters,
    json_to_entities,
    valid_withdrawal_amount,
    validate_broadcast_time,
    withdrawal_details_text,
)


class ReferralWithdrawalTest(unittest.TestCase):
    def test_referral_code_differs_per_bot(self):
        c1 = format_referral_code(123456789, 'botpayment1')
        c2 = format_referral_code(123456789, 'botpayment2')
        self.assertNotEqual(c1, c2)
        self.assertEqual(len(c1), 6)
        self.assertEqual(len(c2), 6)

    def test_referral_code_is_short_alphanumeric(self):
        code = format_referral_code(123456789)
        self.assertRegex(code, r"^[A-Z0-9]{5,6}$")

    def test_parse_referral_payload_accepts_short_code(self):
        self.assertEqual(parse_referral_payload("ref_AB12C"), "AB12C")
        self.assertEqual(parse_referral_payload("AB12C"), "AB12C")

    def test_referral_commission_is_half_package_amount(self):
        self.assertEqual(referral_commission({"package_amount": 50000, "amount": 51000}), 25000)

    def test_parse_withdrawal_amount_accepts_dots(self):
        self.assertEqual(parse_withdrawal_amount("50.000"), 50000)
        self.assertEqual(parse_withdrawal_amount("50000"), 50000)

    def test_withdrawal_details_text_requires_three_lines(self):
        data = withdrawal_details_text("No Hp: 0812\nNama E-Wallet: Dana\nAtas Nama: Budi")
        self.assertEqual(data["phone"], "0812")
        self.assertEqual(data["wallet_name"], "Dana")
        self.assertEqual(data["account_name"], "Budi")

    def test_existing_invited_user_is_not_reassigned_to_new_referrer(self):
        self.assertFalse(should_create_referral(invited_user_id=123, referrer_user_id=999, existing_referral={"referrer_user_id": 111}))

    def test_self_referral_is_ignored(self):
        self.assertFalse(should_create_referral(invited_user_id=123, referrer_user_id=123, existing_referral=None))

    def test_new_invited_user_can_create_referral(self):
        self.assertTrue(should_create_referral(invited_user_id=123, referrer_user_id=999, existing_referral=None))

    def test_referral_counters_never_go_below_zero(self):
        counters = updated_referral_counters({"pending_referrals": 0, "successful_referrals": 2, "balance": 10000}, commission=5000)
        self.assertEqual(counters["pending_referrals"], 0)
        self.assertEqual(counters["successful_referrals"], 3)
        self.assertEqual(counters["balance"], 15000)

    def test_withdrawal_amount_must_not_exceed_balance(self):
        self.assertTrue(valid_withdrawal_amount(50000, 50000))
        self.assertFalse(valid_withdrawal_amount(50001, 50000))
        self.assertFalse(valid_withdrawal_amount(0, 50000))

    def test_withdrawal_amount_minimum_is_ten_thousand(self):
        self.assertTrue(valid_withdrawal_amount(10000, 10000))
        self.assertFalse(valid_withdrawal_amount(9999, 10000))

    def test_main_menu_keyboard_text_greets_user(self):
        user = SimpleNamespace(first_name="Budi", last_name="", username="budi")
        text = main_menu_keyboard_text(user)
        self.assertIn("Halo Budi", text)
        self.assertNotIn("boboinaja", text)
        self.assertNotIn("Menu tersedia", text)

    def test_main_menu_button_labels_include_buy_profile_and_withdrawal(self):
        self.assertEqual(main_menu_button_labels(), ["🛒 Beli Group VIP", "👤 Profile", "💰 Tarik Saldo"])

    def test_referral_user_log_text_includes_name_username_profile_and_id(self):
        text = referral_user_log_text({"user_id": 123456789, "full_name": "John Doe", "username": "johndoe"})
        self.assertIn("John Doe", text)
        self.assertIn("@johndoe", text)
        self.assertIn('tg://user?id=123456789', text)
        self.assertIn("<code>123456789</code>", text)

    def test_internal_telegram_chat_url_opens_first_post(self):
        self.assertEqual(internal_telegram_chat_url(-1001234567890), "https://t.me/c/1234567890/4")

    def test_paid_message_uses_package_specific_clickable_group_link(self):
        text = paid_message(
            "https://t.me/+invite",
            package_name="Group A",
            invite_hours=24,
            group_url="https://t.me/c/1234567890/4",
        )
        self.assertIn('<a href="https://t.me/c/1234567890/4">Buka Group A</a>', text)
        self.assertNotIn("tombol di bawah", text)

    def test_broadcast_entities_round_trip(self):
        raw = entities_to_json(
            [
                MessageEntityBold(offset=0, length=4),
                MessageEntityItalic(offset=5, length=4),
                MessageEntityTextUrl(offset=10, length=4, url="https://example.com"),
            ]
        )
        entities = json_to_entities(raw)
        self.assertIsInstance(entities[0], MessageEntityBold)
        self.assertIsInstance(entities[1], MessageEntityItalic)
        self.assertIsInstance(entities[2], MessageEntityTextUrl)
        self.assertEqual(entities[2].url, "https://example.com")

    def test_validate_broadcast_time_accepts_time_and_off(self):
        self.assertEqual(validate_broadcast_time("09:30"), "09:30")
        self.assertEqual(validate_broadcast_time("off"), "")
        with self.assertRaises(ValueError):
            validate_broadcast_time("25:00")

    def test_admin_command_list_includes_broadcast_commands(self):
        text = admin_command_list_text()
        self.assertIn("/set_broadcast", text)
        self.assertIn("/set_broadcasttime", text)
        self.assertIn("/test_broadcast", text)
        self.assertIn("/commands", text)


    def test_send_profile_does_not_leak_bot_code(self):
        import asyncio
        from vip_bot.handlers.user import send_profile
        from unittest.mock import AsyncMock, MagicMock

        async def _run():
            event = MagicMock()
            event.client.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
            event.client.bot_name = "secret_bot_code"
            user = SimpleNamespace(id=123456, username="tester", first_name="Test", last_name="User")
            event.get_sender = AsyncMock(return_value=user)
            event.respond = AsyncMock()
            db = MagicMock()
            db.upsert_user = AsyncMock()
            db.referral_stats = AsyncMock(return_value={
                "referral_code": "ref123",
                "balance": 50000,
                "successful_count": 5,
                "pending_count": 2,
            })
            await send_profile(event, None, db, bot_code="secret_bot_code")
            self.assertTrue(event.respond.called)
            sent_text = event.respond.call_args[0][0]
            self.assertNotIn("secret_bot_code", sent_text)
            self.assertNotIn("Bot:", sent_text)
            self.assertNotIn("khusus bot ini", sent_text)
            self.assertIn("Profile & Referral", sent_text)
            self.assertIn("123456", sent_text)
            self.assertIn("Rp50.000", sent_text)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
