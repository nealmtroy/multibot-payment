-- =============================================================================
-- MultiBot Payment System - Native PostgreSQL Schema
-- =============================================================================

CREATE TABLE IF NOT EXISTS bots (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL UNIQUE,
    bot_token TEXT NOT NULL UNIQUE,
    bot_username TEXT NOT NULL DEFAULT '',
    bot_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'stopped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bots_status ON bots (status);

CREATE TABLE IF NOT EXISTS packages (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    vip_chat_id BIGINT NOT NULL,
    amount INT NOT NULL CHECK (amount >= 1000 AND amount <= 10000000),
    invite_expire_hours INT NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT true,
    sort_order INT NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bot_code, code)
);

CREATE INDEX IF NOT EXISTS idx_packages_bot_active ON packages (bot_code, active, sort_order ASC);

CREATE TABLE IF NOT EXISTS users (
    bot_code TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    referral_code TEXT NOT NULL,
    invited_by_user_id BIGINT,
    balance INT NOT NULL DEFAULT 0,
    pending_referrals INT NOT NULL DEFAULT 0,
    successful_referrals INT NOT NULL DEFAULT 0,
    is_bot BOOLEAN NOT NULL DEFAULT false,
    last_broadcast_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_code, user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_referral ON users (bot_code, referral_code);
CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users (bot_code, invited_by_user_id);
CREATE INDEX IF NOT EXISTS idx_users_broadcast ON users (bot_code, is_bot, last_broadcast_at ASC NULLS FIRST, user_id);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL,
    referrer_user_id BIGINT NOT NULL,
    referrer_code TEXT NOT NULL,
    invited_user_id BIGINT NOT NULL,
    invited_username TEXT NOT NULL DEFAULT '',
    invited_full_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    payment_inv_id TEXT,
    package_code TEXT NOT NULL DEFAULT '',
    package_amount INT NOT NULL DEFAULT 0,
    commission_amount INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bot_code, invited_user_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (bot_code, referrer_user_id, status);

CREATE TABLE IF NOT EXISTS withdrawals (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    amount INT NOT NULL CHECK (amount >= 10000),
    phone TEXT NOT NULL,
    wallet_name TEXT NOT NULL,
    account_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected')),
    admin_user_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_bot_status ON withdrawals (bot_code, status, id DESC);

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL DEFAULT 'default',
    user_id BIGINT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    package_code TEXT NOT NULL DEFAULT '',
    package_name TEXT NOT NULL DEFAULT '',
    package_amount INT NOT NULL DEFAULT 0,
    vip_chat_id BIGINT,
    invite_expire_hours INT NOT NULL DEFAULT 0,
    public_invoice_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL UNIQUE,
    payment_url TEXT NOT NULL,
    inv_id TEXT NOT NULL UNIQUE,
    amount INT NOT NULL,
    status TEXT NOT NULL,
    buyer_name TEXT NOT NULL,
    buyer_email TEXT NOT NULL,
    qris_amount TEXT NOT NULL DEFAULT '',
    qris_expires TEXT NOT NULL DEFAULT '',
    qris_chat_id BIGINT,
    qris_message_id BIGINT,
    next_check_at TIMESTAMPTZ,
    poll_attempts INT NOT NULL DEFAULT 0,
    last_polled_at TIMESTAMPTZ,
    invite_link TEXT,
    invite_expires_at TIMESTAMPTZ,
    error TEXT NOT NULL DEFAULT '',
    referral_id BIGINT,
    referrer_user_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_user ON payments (bot_code, user_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_payments_next_check ON payments (next_check_at ASC, id ASC) WHERE status IN ('pending', 'invite_error', 'delivery_error');

-- 1 active payment per user per bot
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_one_active
ON payments (bot_code, user_id)
WHERE status IN ('pending', 'processing_paid', 'invite_error', 'processing_delivery', 'delivery_error');

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS broadcast_messages (
    id SERIAL PRIMARY KEY,
    bot_code TEXT NOT NULL DEFAULT 'default',
    message_text TEXT NOT NULL DEFAULT '',
    media_telegram_file_id TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    entities_json TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_broadcast_bot ON broadcast_messages (bot_code, is_active, id DESC);
