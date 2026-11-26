import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import saweria_client
from vip_bot.helpers import (
    check_payment_sync,
    create_qris_sync,
    detect_payment_gateway,
    normalize_payment_gateway,
    normalize_sociabuzz_username,
    runtime_gateway_username,
    runtime_payment_gateway,
    runtime_saweria_username,
    runtime_sociabuzz_username,
)
from vip_bot.handlers.admin import build_runtime_settings_panel
from vip_bot.loops import poll_once


def get_button_data(btn):
    if hasattr(btn, "data"):
        return btn.data
    if hasattr(btn, "type") and hasattr(btn.type, "data"):
        return btn.type.data
    return getattr(btn, "_bytes", None)


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON")
        return self._json_data


class TestSaweriaGateway(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        saweria_client.clear_creator_cache()

    def test_normalize_username_variants(self):
        self.assertEqual(saweria_client.normalize_username("kiesha144"), "kiesha144")
        self.assertEqual(saweria_client.normalize_username("@kiesha144"), "kiesha144")
        self.assertEqual(saweria_client.normalize_username("https://saweria.co/kiesha144"), "kiesha144")
        self.assertEqual(saweria_client.normalize_username("https://saweria.co/kiesha144/"), "kiesha144")
        self.assertEqual(saweria_client.normalize_username("saweria.co/kiesha144"), "kiesha144")
        with self.assertRaises(saweria_client.SaweriaError):
            saweria_client.normalize_username("")
        with self.assertRaises(saweria_client.SaweriaError):
            saweria_client.normalize_username("https://saweria.co/")

    def test_normalize_amount(self):
        self.assertEqual(saweria_client.normalize_amount("50.000"), 50000)
        self.assertEqual(saweria_client.normalize_amount(1000), 1000)
        with self.assertRaises(saweria_client.SaweriaError):
            saweria_client.normalize_amount(500)

    def test_create_qris_and_check_pending(self):
        next_data = {
            "props": {
                "pageProps": {
                    "data": {
                        "id": "b48c0bda-2953-4dbf-8db5-9df4689bfbf1",
                        "username": "kiesha144",
                        "isQRISEnabled": True,
                    }
                }
            }
        }
        profile_html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
        create_json = {
            "data": {
                "id": "ff46f60a-adf2-4204-8777-937b194fb6ea",
                "amount": 5000,
                "amount_raw": 5000,
                "created_at": "Thu, 26 Nov 2026 09:23:20 GMT",
                "qr_string": "00020101021226650013CO.XENDIT.WWW01189360084800000000020215TEST540450005802ID6304ABCD",
                "status": "PENDING",
            }
        }

        session = MagicMock()
        session.request.side_effect = [
            FakeResponse(200, text=profile_html),
            FakeResponse(201, json_data=create_json),
            FakeResponse(200, json_data={"data": {"transaction_status": "PENDING"}}),
            FakeResponse(200, json_data={"data": {"transaction_status": "SUCCESS"}}),
            FakeResponse(200, json_data={"data": {"transaction_status": "FAILED"}}),
            FakeResponse(404, text='{"message":"not found"}'),
        ]

        inv_id, payment_url, qris = saweria_client.create_qris(
            session,
            "https://saweria.co/kiesha144",
            5000,
            "Budi Santoso",
            "budi@gmail.com",
            "VIP 123",
        )
        self.assertEqual(inv_id, "ff46f60a-adf2-4204-8777-937b194fb6ea")
        self.assertEqual(payment_url, "https://saweria.co/qris/snap/ff46f60a-adf2-4204-8777-937b194fb6ea")
        self.assertEqual(qris["inv_id"], inv_id)
        self.assertEqual(qris["source_payment"], "saweria")
        self.assertEqual(qris["data"]["amount"], "Rp5.000")
        self.assertTrue(qris["data"]["qr_string"].startswith("000201"))

        st1, _, _ = saweria_client.check_pending(session, inv_id)
        st2, _, _ = saweria_client.check_pending(session, inv_id)
        st3, _, _ = saweria_client.check_pending(session, inv_id)
        st4, _, _ = saweria_client.check_pending(session, inv_id)
        self.assertEqual(st1, "pending")
        self.assertEqual(st2, "paid")
        self.assertEqual(st3, "failed_or_expired")
        self.assertEqual(st4, "failed_or_expired")

    def test_check_pending_5xx_raises(self):
        session = MagicMock()
        session.request.return_value = FakeResponse(502, text="Bad Gateway")
        with self.assertRaises(saweria_client.SaweriaError):
            saweria_client.check_pending(session, "abc-123")

    def test_detect_and_normalize_gateway(self):
        self.assertEqual(normalize_payment_gateway("SOCIABUZZ"), "sociabuzz")
        self.assertEqual(normalize_payment_gateway("saweria"), "saweria")
        with self.assertRaises(ValueError):
            normalize_payment_gateway("paypal")

        self.assertEqual(normalize_sociabuzz_username("https://sociabuzz.com/myaccount/tribe"), "myaccount")
        self.assertEqual(detect_payment_gateway({"payment_gateway": "saweria"}), "saweria")
        self.assertEqual(detect_payment_gateway({"payment_url": "https://saweria.co/qris/snap/123"}), "saweria")
        self.assertEqual(detect_payment_gateway({"payment_url": "https://sociabuzz.com/payment/abc"}), "sociabuzz")

    def test_create_qris_sync_routes_to_saweria(self):
        config = SimpleNamespace(
            payment_gateway="saweria",
            saweria_username="kiesha144",
            sociabuzz_username="sb_user",
            sociabuzz_cookie="",
            payment_amount=50000,
        )
        user = SimpleNamespace(id=999111)
        fake_qris = {
            "status": True,
            "inv_id": "saw-inv-1",
            "source_payment": "saweria",
            "data": {
                "qr_string": "0002010102125802ID5904TEST63041234",
                "amount": "Rp25.000",
                "countdown": "2026-11-26T10:00:00+00:00",
            },
        }

        with patch("vip_bot.helpers.new_saweria_session", return_value=MagicMock()), \
             patch("vip_bot.helpers.create_saweria_qris", return_value=("saw-inv-1", "https://saweria.co/qris/snap/saw-inv-1", fake_qris)) as mock_create:
            _sess, _name, _email, order_id, payment_url, qris, qr_bytes, amount = create_qris_sync(
                config, user, amount=25000, note_prefix="VIP", gateway="saweria", gateway_username="kiesha144"
            )

        mock_create.assert_called_once()
        sent_note = mock_create.call_args[0][5]
        self.assertNotIn("999111", sent_note)
        self.assertNotIn("VIP", sent_note)
        from vip_bot.config import DONATION_MESSAGES
        self.assertIn(sent_note, DONATION_MESSAGES)
        self.assertEqual(order_id, "saw-inv-1")
        self.assertEqual(payment_url, "https://saweria.co/qris/snap/saw-inv-1")
        self.assertEqual(qris["inv_id"], "saw-inv-1")
        self.assertEqual(amount, 25000)
        self.assertTrue(qr_bytes.startswith(b"\x89PNG"))

    def test_check_payment_sync_routes_by_gateway(self):
        config = SimpleNamespace(sociabuzz_cookie="")
        with patch("vip_bot.helpers.new_saweria_session", return_value=MagicMock()), \
             patch("vip_bot.helpers.check_saweria_pending", return_value=("paid", "https://backend.saweria.co/donations/qris/snap/id1", 42)) as mock_saw, \
             patch("vip_bot.helpers.new_session", return_value=MagicMock()), \
             patch("vip_bot.helpers.check_pending", return_value=("pending", "https://sociabuzz.com/payment/pending", 55)) as mock_sb:
            res_saw = check_payment_sync(config, "id1", gateway="saweria")
            res_sb = check_payment_sync(config, "id2", gateway="sociabuzz")

        self.assertEqual(res_saw[0], "paid")
        self.assertEqual(res_sb[0], "pending")
        mock_saw.assert_called_once()
        mock_sb.assert_called_once()

    async def test_runtime_settings_and_panel(self):
        storage = {
            "payment_gateway": "saweria",
            "saweria_username": "kiesha144",
            "sociabuzz_username": "sb_creator",
            "log_chat_id": "-100111",
            "vip_chat_id": "-100222",
        }
        db = MagicMock()
        db.get_setting = AsyncMock(side_effect=lambda k, default="": storage.get(k, default))
        config = SimpleNamespace(
            payment_gateway="sociabuzz",
            saweria_username="",
            sociabuzz_username="",
            log_chat_id=None,
            vip_chat_id=None,
        )

        self.assertEqual(await runtime_payment_gateway(config, db), "saweria")
        self.assertEqual(await runtime_saweria_username(config, db), "kiesha144")
        self.assertEqual(await runtime_sociabuzz_username(config, db), "sb_creator")
        self.assertEqual(await runtime_gateway_username(config, db), "kiesha144")

        text, buttons = await build_runtime_settings_panel(config, db)
        self.assertIn("SAWERIA", text)
        self.assertIn("kiesha144", text)
        self.assertIn("sb_creator", text)
        self.assertEqual(buttons[0][0].text, "SOCIABUZZ")
        self.assertEqual(buttons[0][1].text, "SAWERIA ✅")
        self.assertEqual(get_button_data(buttons[0][0]), b"adm_gw_set:sociabuzz")
        self.assertEqual(get_button_data(buttons[0][1]), b"adm_gw_set:saweria")
        self.assertEqual(get_button_data(buttons[1][1]), b"adm_cfg:saweria_username")

    async def test_poll_once_uses_payment_gateway_per_row(self):
        client = MagicMock()
        config = MagicMock()
        db = MagicMock()
        payment = {
            "inv_id": "saw-uuid-1",
            "bot_code": "default",
            "status": "pending",
            "payment_gateway": "saweria",
            "payment_url": "https://saweria.co/qris/snap/saw-uuid-1",
        }
        with patch("vip_bot.loops.check_payment_sync", return_value=("paid", "https://backend.saweria.co/donations/qris/snap/saw-uuid-1", 10)) as mock_check, \
             patch("vip_bot.loops.process_paid_payment", new_callable=AsyncMock) as mock_paid:
            await poll_once(client, config, db, payment)

        mock_check.assert_called_once_with(config, "saw-uuid-1", gateway="saweria")
        mock_paid.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
