import os
import re
import zoneinfo
from dataclasses import dataclass
from dotenv import load_dotenv

WIB = zoneinfo.ZoneInfo("Asia/Jakarta")
BROADCAST_DISABLED_VALUES = {"", "off", "disabled", "false", "0"}
BROADCAST_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MIN_WITHDRAWAL_AMOUNT = 10000

FIRST_NAMES = [
    "Budi", "Adi", "Agus", "Bambang", "Bayu", "Cahyo", "Dedi", "Denny", "Dimas", "Eko",
    "Fajar", "Galih", "Gilang", "Hadi", "Hendra", "Irfan", "Indra", "Joko", "Riyan", "Yudha",
    "Surya", "Wahyu", "Pratama", "Putra", "Rizky", "Rifky", "Satria", "Tri", "Yoga", "Zainal",
]

LAST_NAMES = [
    "Santoso", "Pratama", "Saputra", "Wijaya", "Setiawan", "Hidayat", "Nugroho", "Wibowo",
    "Kusuma", "Lestari", "Utomo", "Permana", "Kurniawan", "Suryanto", "Gunawan", "Susanto",
    "Purnama", "Siregar", "Nasution", "Pramono",
]

ACTIVE_PAYMENT_STATUSES = (
    "pending",
    "processing_paid",
    "invite_error",
    "processing_delivery",
    "delivery_error",
)

RETRYABLE_PAYMENT_STATUSES = (
    "pending",
    "invite_error",
    "delivery_error",
)


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    database_url: str
    vip_chat_id: int | None
    log_chat_id: int | None
    sociabuzz_username: str
    sociabuzz_cookie: str
    payment_amount: int
    invite_expire_hours: int
    poll_interval_seconds: int
    poll_max_attempts: int
    poll_batch_size: int
    broadcast_batch_size: int
    qris_create_concurrency: int
    admin_user_ids: set[int]
    log_level: str


def env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def load_config() -> Config:
    load_dotenv()
    
    api_id = env_int("TELEGRAM_API_ID", 0)
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/multibot_db").strip()

    raw_vip = os.getenv("VIP_CHAT_ID", "").strip()
    vip_chat_id = int(raw_vip) if raw_vip else None

    raw_log = os.getenv("LOG_CHAT_ID", "").strip()
    log_chat_id = int(raw_log) if raw_log else None

    admin_ids = set()
    raw_admins = os.getenv("ADMIN_USER_IDS", "").strip()
    if raw_admins:
        for part in raw_admins.split(","):
            part = part.strip()
            if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
                admin_ids.add(int(part))

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        database_url=database_url,
        vip_chat_id=vip_chat_id,
        log_chat_id=log_chat_id,
        sociabuzz_username=os.getenv("SOCIABUZZ_USERNAME", "").strip(),
        sociabuzz_cookie=os.getenv("SOCIABUZZ_COOKIE", "").strip(),
        payment_amount=env_int("PAYMENT_AMOUNT", 2000),
        invite_expire_hours=env_int("INVITE_EXPIRE_HOURS", 24),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 3),
        poll_max_attempts=env_int("POLL_MAX_ATTEMPTS", 300),
        poll_batch_size=env_int("POLL_BATCH_SIZE", 20),
        broadcast_batch_size=env_int("BROADCAST_BATCH_SIZE", 20),
        qris_create_concurrency=env_int("QRIS_CREATE_CONCURRENCY", 5),
        admin_user_ids=admin_ids,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
