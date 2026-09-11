import asyncio
import datetime as dt
import logging
from pathlib import Path
import asyncpg

LOGGER = logging.getLogger("telegram_vip_bot.db")


def record_to_dict(record):
    if record is None:
        return None
    return dict(record)


def records_to_dicts(records):
    return [dict(r) for r in (records or [])]


class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def create(cls, database_url: str, min_size: int = 2, max_size: int = 10):
        LOGGER.info("Connecting to PostgreSQL at %s ...", database_url.split("@")[-1] if "@" in database_url else database_url)
        pool = await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self):
        await self.pool.close()

    async def init_schema(self, schema_path: str = None):
        if schema_path is None:
            schema_path = str(Path(__file__).resolve().parent.parent / "schema.sql")
        p = Path(schema_path)
        if not p.exists():
            LOGGER.warning("Schema file not found at %s", schema_path)
            return
        sql = p.read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_hash BIGINT NOT NULL DEFAULT 0;")
            except Exception as e:
                LOGGER.warning("Could not run access_hash migration: %s", e)
        LOGGER.info("PostgreSQL schema initialized successfully.")

    # -------------------------------------------------------------------------
    # Bot Management Methods
    # -------------------------------------------------------------------------

    async def list_active_bots(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bots WHERE status = 'active' ORDER BY id ASC")
            return records_to_dicts(rows)

    async def list_all_bots(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bots ORDER BY id ASC")
            return records_to_dicts(rows)

    async def get_bot(self, bot_code: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM bots WHERE bot_code = $1", bot_code)
            return record_to_dict(row)

    async def upsert_bot(self, bot_code: str, bot_token: str, bot_username: str = "", bot_name: str = "", status: str = "active") -> dict:
        query = """
        INSERT INTO bots (bot_code, bot_token, bot_username, bot_name, status, updated_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (bot_code) DO UPDATE SET
            bot_token = EXCLUDED.bot_token,
            bot_username = EXCLUDED.bot_username,
            bot_name = EXCLUDED.bot_name,
            status = EXCLUDED.status,
            updated_at = now()
        RETURNING *;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, bot_code, bot_token, bot_username, bot_name or bot_code, status)
            return record_to_dict(row)

    async def set_bot_status(self, bot_code: str, status: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute("UPDATE bots SET status = $1, updated_at = now() WHERE bot_code = $2", status, bot_code)
            return "UPDATE 1" in res

    async def delete_bot(self, bot_code: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute("DELETE FROM bots WHERE bot_code = $1", bot_code)
            return "DELETE 1" in res

    # -------------------------------------------------------------------------
    # Package Management Methods
    # -------------------------------------------------------------------------

    async def list_packages(self, bot_code: str = "default") -> list[dict]:
        query = "SELECT * FROM packages WHERE bot_code = $1 AND active = true ORDER BY sort_order ASC, amount ASC"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, bot_code)
            return records_to_dicts(rows)

    async def list_all_packages(self, bot_code: str = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if bot_code:
                rows = await conn.fetch("SELECT * FROM packages WHERE bot_code = $1 ORDER BY sort_order ASC, amount ASC", bot_code)
            else:
                rows = await conn.fetch("SELECT * FROM packages ORDER BY bot_code ASC, sort_order ASC, amount ASC")
            return records_to_dicts(rows)

    async def get_package(self, code: str, bot_code: str = "default") -> dict | None:
        query = "SELECT * FROM packages WHERE bot_code = $1 AND code = $2"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, bot_code, code)
            return record_to_dict(row)

    async def upsert_package(self, code: str, name: str, vip_chat_id: int, amount: int, invite_expire_hours: int = 0, bot_code: str = "default") -> dict:
        query = """
        INSERT INTO packages (bot_code, code, name, vip_chat_id, amount, invite_expire_hours, active, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, true, now())
        ON CONFLICT (bot_code, code) DO UPDATE SET
            name = EXCLUDED.name,
            vip_chat_id = EXCLUDED.vip_chat_id,
            amount = EXCLUDED.amount,
            invite_expire_hours = EXCLUDED.invite_expire_hours,
            active = true,
            updated_at = now()
        RETURNING *;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, bot_code, code, name.strip(), int(vip_chat_id), int(amount), int(invite_expire_hours or 0))
            return record_to_dict(row)

    async def delete_package(self, code: str, bot_code: str = "default") -> bool:
        query = "DELETE FROM packages WHERE bot_code = $1 AND code = $2"
        async with self.pool.acquire() as conn:
            res = await conn.execute(query, bot_code, code)
            return "DELETE 1" in res

    # -------------------------------------------------------------------------
    # User & Referral Methods (Scoped per bot_code)
    # -------------------------------------------------------------------------

    async def upsert_user(self, user, bot_code: str = "default") -> dict:
        from vip_bot.helpers import format_referral_code, display_name
        code = format_referral_code(user.id, bot_code=bot_code)
        name = display_name(user)
        username = user.username or ""
        is_bot = bool(getattr(user, "bot", False))
        access_hash = int(getattr(user, "access_hash", 0) or 0)

        query = """
        INSERT INTO users (bot_code, user_id, username, full_name, referral_code, is_bot, access_hash, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        ON CONFLICT (bot_code, user_id) DO UPDATE SET
            username = EXCLUDED.username,
            full_name = EXCLUDED.full_name,
            referral_code = EXCLUDED.referral_code,
            is_bot = EXCLUDED.is_bot,
            access_hash = CASE WHEN EXCLUDED.access_hash <> 0 THEN EXCLUDED.access_hash ELSE users.access_hash END,
            updated_at = now()
        RETURNING *;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, bot_code, user.id, username, name, code, is_bot, access_hash)
            return record_to_dict(row)

    async def get_user(self, user_id: int, bot_code: str = "default") -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE bot_code = $1 AND user_id = $2", bot_code, int(user_id))
            return record_to_dict(row)

    async def get_user_by_referral_code(self, code: str, bot_code: str = "default") -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE bot_code = $1 AND referral_code = $2", bot_code, code)
            return record_to_dict(row)

    async def create_referral_if_absent(self, referrer: dict, invited_user, bot_code: str = "default") -> tuple[dict | None, bool]:
        from vip_bot.helpers import display_name, should_create_referral
        invited = await self.upsert_user(invited_user, bot_code=bot_code)
        if not should_create_referral(invited_user.id, referrer.get("user_id") if referrer else 0, invited.get("invited_by_user_id")):
            return None, False

        ref_code = referrer["referral_code"]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Check if already referred in this bot
                exists = await conn.fetchval("SELECT id FROM referrals WHERE bot_code = $1 AND invited_user_id = $2", bot_code, invited_user.id)
                if exists:
                    return None, False

                row = await conn.fetchrow(
                    """
                    INSERT INTO referrals (bot_code, referrer_user_id, referrer_code, invited_user_id, invited_username, invited_full_name, status, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending', now(), now())
                    RETURNING *;
                    """,
                    bot_code,
                    int(referrer["user_id"]),
                    ref_code,
                    invited_user.id,
                    invited_user.username or "",
                    display_name(invited_user),
                )
                await conn.execute(
                    "UPDATE users SET invited_by_user_id = $1, updated_at = now() WHERE bot_code = $2 AND user_id = $3 AND invited_by_user_id IS NULL",
                    int(referrer["user_id"]),
                    bot_code,
                    invited_user.id,
                )
                await conn.execute(
                    "UPDATE users SET pending_referrals = pending_referrals + 1, updated_at = now() WHERE bot_code = $1 AND user_id = $2",
                    bot_code,
                    int(referrer["user_id"]),
                )
                return record_to_dict(row), True

    async def pending_referral_for_user(self, user_id: int, bot_code: str = "default") -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM referrals WHERE bot_code = $1 AND invited_user_id = $2 AND status = 'pending' LIMIT 1",
                bot_code,
                int(user_id),
            )
            return record_to_dict(row)

    async def referral_stats(self, user_id: int, bot_code: str = "default") -> dict:
        from vip_bot.helpers import format_referral_code
        user = await self.get_user(user_id, bot_code=bot_code) or {}
        return {
            "pending_count": int(user.get("pending_referrals") or 0),
            "successful_count": int(user.get("successful_referrals") or 0),
            "balance": int(user.get("balance") or 0),
            "referral_code": user.get("referral_code") or format_referral_code(user_id, bot_code=bot_code),
            "invited_by_user_id": user.get("invited_by_user_id"),
            "phone": user.get("phone") or "",
        }

    async def mark_referral_paid(self, referral_id: int, payment: dict, commission: int) -> dict | None:
        bot_code = payment.get("bot_code") or "default"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE referrals
                    SET status = 'paid',
                        payment_inv_id = $1,
                        package_code = $2,
                        package_amount = $3,
                        commission_amount = $4,
                        updated_at = now()
                    WHERE id = $5 AND status = 'pending'
                    RETURNING *;
                    """,
                    payment["inv_id"],
                    payment.get("package_code") or "",
                    int(payment.get("package_amount") or 0),
                    int(commission),
                    int(referral_id),
                )
                if not row:
                    return None
                referral = record_to_dict(row)
                referrer_id = int(referral["referrer_user_id"])
                await conn.execute(
                    """
                    UPDATE users
                    SET balance = balance + $1,
                        pending_referrals = GREATEST(pending_referrals - 1, 0),
                        successful_referrals = successful_referrals + 1,
                        updated_at = now()
                    WHERE bot_code = $2 AND user_id = $3;
                    """,
                    int(commission),
                    bot_code,
                    referrer_id,
                )
                return referral

    # -------------------------------------------------------------------------
    # Withdrawal Methods (ACID Transactions)
    # -------------------------------------------------------------------------

    async def create_withdrawal(self, user, amount: int, details: dict, bot_code: str = "default") -> dict:
        from vip_bot.helpers import display_name
        amount = int(amount)
        if amount < 10000:
            raise ValueError("Nominal minimal penarikan adalah Rp10.000")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current_balance = await conn.fetchval(
                    "SELECT balance FROM users WHERE bot_code = $1 AND user_id = $2 FOR UPDATE",
                    bot_code,
                    user.id,
                )
                if current_balance is None or current_balance < amount:
                    raise ValueError("Insufficient balance")

                await conn.execute(
                    "UPDATE users SET balance = balance - $1, phone = $2, updated_at = now() WHERE bot_code = $3 AND user_id = $4",
                    amount,
                    details["phone"],
                    bot_code,
                    user.id,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO withdrawals (bot_code, user_id, username, full_name, amount, phone, wallet_name, account_name, status, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', now(), now())
                    RETURNING *;
                    """,
                    bot_code,
                    user.id,
                    user.username or "",
                    display_name(user),
                    amount,
                    details["phone"],
                    details["wallet_name"],
                    details["account_name"],
                )
                return record_to_dict(row)

    async def list_pending_withdrawals(self, limit: int = 20, bot_code: str = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if bot_code:
                rows = await conn.fetch(
                    "SELECT * FROM withdrawals WHERE status = 'pending' AND bot_code = $1 ORDER BY id ASC LIMIT $2",
                    bot_code,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC LIMIT $1",
                    limit,
                )
            return records_to_dicts(rows)

    async def get_withdrawal(self, withdrawal_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM withdrawals WHERE id = $1", int(withdrawal_id))
            return record_to_dict(row)

    async def update_withdrawal_status(self, withdrawal_id: int, from_status: str, to_status: str, admin_user_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE withdrawals SET status = $1, admin_user_id = $2, updated_at = now() WHERE id = $3 AND status = $4 RETURNING *",
                    to_status,
                    int(admin_user_id),
                    int(withdrawal_id),
                    from_status,
                )
                if not row:
                    return None
                withdrawal = record_to_dict(row)
                if to_status == "rejected":
                    # Refund balance to user
                    bot_code = withdrawal.get("bot_code") or "default"
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1, updated_at = now() WHERE bot_code = $2 AND user_id = $3",
                        int(withdrawal["amount"]),
                        bot_code,
                        int(withdrawal["user_id"]),
                    )
                return withdrawal

    # -------------------------------------------------------------------------
    # Payment Methods
    # -------------------------------------------------------------------------

    async def create_payment(
        self,
        user,
        public_invoice_id: str,
        order_id: str,
        payment_url: str,
        inv_id: str,
        amount: int,
        buyer_name: str,
        buyer_email: str,
        qris_data: dict,
        qris_chat_id: int,
        qris_message_id: int,
        package: dict = None,
        referral: dict = None,
        bot_code: str = "default",
    ):
        from vip_bot.helpers import parse_iso_datetime, next_poll_at, display_name
        payload = qris_data.get("data", {})
        package = package or {}
        expires_at = parse_iso_datetime(payload.get("countdown") or "")
        next_check = next_poll_at(dt.datetime.now(dt.UTC), expires_at, attempts=0, error="")
        next_check_dt = parse_iso_datetime(next_check) if next_check else None

        query = """
        INSERT INTO payments (
            bot_code, user_id, username, full_name, package_code, package_name, package_amount,
            vip_chat_id, invite_expire_hours, public_invoice_id, order_id, payment_url, inv_id,
            amount, status, buyer_name, buyer_email, qris_amount, qris_expires, qris_chat_id,
            qris_message_id, next_check_at, poll_attempts, referral_id, referrer_user_id, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, 'pending',
            $15, $16, $17, $18, $19, $20, $21, 0, $22, $23, now(), now()
        );
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                query,
                bot_code,
                user.id,
                user.username or "",
                display_name(user),
                package.get("code") or "",
                package.get("name") or "",
                int(package.get("amount") or amount),
                package.get("vip_chat_id"),
                int(package.get("invite_expire_hours") or 0),
                public_invoice_id,
                order_id,
                payment_url,
                inv_id,
                amount,
                buyer_name,
                buyer_email,
                payload.get("amount") or "",
                payload.get("countdown") or "",
                qris_chat_id,
                qris_message_id,
                next_check_dt,
                referral.get("id") if referral else None,
                referral.get("referrer_user_id") if referral else None,
            )

    async def latest_pending_for_user(self, user_id: int, bot_code: str = "default") -> dict | None:
        query = """
        SELECT * FROM payments
        WHERE bot_code = $1 AND user_id = $2
          AND status IN ('pending', 'processing_paid', 'invite_error', 'processing_delivery', 'delivery_error')
        ORDER BY id DESC LIMIT 1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, bot_code, int(user_id))
            return record_to_dict(row)

    async def retryable_payments(self, due_before_iso: str, limit: int) -> list[dict]:
        from vip_bot.helpers import parse_iso_datetime
        due_dt = parse_iso_datetime(due_before_iso) or dt.datetime.now(dt.UTC)
        query = """
        SELECT * FROM payments
        WHERE status IN ('pending', 'invite_error', 'delivery_error')
          AND (next_check_at IS NULL OR next_check_at <= $1)
        ORDER BY next_check_at ASC, id ASC
        LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, due_dt, limit)
            return records_to_dicts(rows)

    async def recover_stale_processing(self, older_than_seconds: int = 300):
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=older_than_seconds)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE payments SET status = 'invite_error', error = 'Recovered stale paid processing', updated_at = now() WHERE status = 'processing_paid' AND updated_at < $1",
                cutoff,
            )
            await conn.execute(
                "UPDATE payments SET status = 'delivery_error', error = 'Recovered stale delivery processing', updated_at = now() WHERE status = 'processing_delivery' AND updated_at < $1",
                cutoff,
            )

    async def claim_paid_processing(self, inv_id: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE payments SET status = 'processing_paid', updated_at = now() WHERE inv_id = $1 AND status IN ('pending', 'invite_error')",
                inv_id,
            )
            return "UPDATE 1" in res

    async def mark_delivery_processing(self, inv_id: str, invite_link: str, invite_expires_at: str) -> bool:
        from vip_bot.helpers import parse_iso_datetime
        exp_dt = parse_iso_datetime(invite_expires_at) if invite_expires_at else None
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE payments SET status = 'processing_delivery', invite_link = $1, invite_expires_at = $2, updated_at = now() WHERE inv_id = $3 AND status = 'processing_paid'",
                invite_link,
                exp_dt,
                inv_id,
            )
            return "UPDATE 1" in res

    async def mark_delivery_done(self, inv_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = 'paid', error = '', updated_at = now() WHERE inv_id = $1", inv_id)

    async def mark_delivery_error(self, inv_id: str, error: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = 'delivery_error', error = $1, updated_at = now() WHERE inv_id = $2", str(error), inv_id)

    async def mark_delivery_blocked(self, inv_id: str, error: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = 'delivery_blocked', error = $1, updated_at = now() WHERE inv_id = $2", str(error), inv_id)

    async def mark_invite_error(self, inv_id: str, error: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = 'invite_error', error = $1, updated_at = now() WHERE inv_id = $2", str(error), inv_id)

    async def mark_payment_pending(self, inv_id: str, attempts: int, next_check_iso: str, error: str = ""):
        from vip_bot.helpers import parse_iso_datetime
        next_dt = parse_iso_datetime(next_check_iso) if next_check_iso else None
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE payments SET status = 'pending', poll_attempts = $1, next_check_at = $2, last_polled_at = now(), error = $3, updated_at = now() WHERE inv_id = $4 AND status = 'pending'",
                attempts,
                next_dt,
                error or "",
                inv_id,
            )

    async def mark_payment_timeout(self, inv_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = 'timeout', updated_at = now() WHERE inv_id = $1 AND status = 'pending'", inv_id)

    async def mark_payment_failed(self, inv_id: str, status_name: str, error: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status = $1, error = $2, updated_at = now() WHERE inv_id = $3 AND status = 'pending'", status_name, str(error), inv_id)

    async def ensure_payment_schema_ready(self):
        async with self.pool.acquire() as conn:
            await conn.fetchrow("SELECT id FROM payments LIMIT 1")

    # -------------------------------------------------------------------------
    # Settings & Broadcast Methods
    # -------------------------------------------------------------------------

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
            return val if val is not None else default

    async def set_setting(self, key: str, value: str):
        query = """
        INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, key, str(value))

    async def get_active_broadcast_message(self, bot_code: str = "default") -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM broadcast_messages WHERE bot_code = $1 AND is_active = true ORDER BY id DESC LIMIT 1",
                bot_code or "default",
            )
            return record_to_dict(row)

    async def set_broadcast_message(
        self,
        message_text: str,
        media_file_id: str,
        media_type: str,
        entities_json: str,
        bot_code: str = "default",
    ) -> dict:
        code = bot_code or "default"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE broadcast_messages SET is_active = false, updated_at = now() WHERE bot_code = $1 AND is_active = true",
                    code,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO broadcast_messages (
                        bot_code, message_text, media_telegram_file_id, media_type, entities_json, is_active, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, true, now(), now())
                    RETURNING *;
                    """,
                    code,
                    message_text or "",
                    media_file_id or "",
                    media_type or "",
                    entities_json or "",
                )
                return record_to_dict(row)

    async def get_broadcast_targets(
        self, bot_code: str = "default", before_iso: str = None, limit: int = 1000
    ) -> list[dict]:
        from vip_bot.helpers import parse_iso_datetime
        code = bot_code or "default"
        before_dt = parse_iso_datetime(before_iso) if before_iso else None
        async with self.pool.acquire() as conn:
            if before_dt:
                rows = await conn.fetch(
                    """
                    SELECT user_id, access_hash FROM users
                    WHERE bot_code = $1 AND is_bot = false AND (last_broadcast_at IS NULL OR last_broadcast_at < $2)
                    ORDER BY last_broadcast_at ASC NULLS FIRST LIMIT $3
                    """,
                    code,
                    before_dt,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT user_id, access_hash FROM users
                    WHERE bot_code = $1 AND is_bot = false
                    ORDER BY last_broadcast_at ASC NULLS FIRST LIMIT $2
                    """,
                    code,
                    limit,
                )
            return records_to_dicts(rows)

    async def mark_user_broadcasted(self, user_id: int, bot_code: str = "default"):
        code = bot_code or "default"
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_broadcast_at = now(), updated_at = now() WHERE bot_code = $1 AND user_id = $2",
                code,
                int(user_id),
            )

    async def count_broadcast_targets(self, bot_code: str = "default") -> int:
        code = bot_code or "default"
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM users WHERE bot_code = $1 AND is_bot = false", code) or 0

    async def set_broadcast_time(self, time_str: str, bot_code: str = "default"):
        code = bot_code or "default"
        key = f"broadcast_time:{code}"
        await self.set_setting(key, time_str or "")
        if code == "default":
            await self.set_setting("broadcast_time", time_str or "")

    async def get_broadcast_time(self, bot_code: str = "default") -> str:
        code = bot_code or "default"
        key = f"broadcast_time:{code}"
        val = await self.get_setting(key, "")
        if not val and code == "default":
            val = await self.get_setting("broadcast_time", "")
        return val or ""

    async def set_last_broadcast_date(self, date_str: str, bot_code: str = "default"):
        code = bot_code or "default"
        key = f"last_broadcast_date:{code}"
        await self.set_setting(key, date_str or "")
        if code == "default":
            await self.set_setting("last_broadcast_date", date_str or "")

    async def get_last_broadcast_date(self, bot_code: str = "default") -> str:
        code = bot_code or "default"
        key = f"last_broadcast_date:{code}"
        val = await self.get_setting(key, "")
        if not val and code == "default":
            val = await self.get_setting("last_broadcast_date", "")
        return val or ""
