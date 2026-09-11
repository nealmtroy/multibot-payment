import os
import re
import zoneinfo
from dataclasses import dataclass
from dotenv import load_dotenv

WIB = zoneinfo.ZoneInfo("Asia/Jakarta")
BROADCAST_DISABLED_VALUES = {"", "off", "disabled", "false", "0"}
BROADCAST_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MIN_WITHDRAWAL_AMOUNT = 10000

FIRST_NAMES_MAN = [
    "Aditya", "Agus", "Ahmad", "Aldi", "Alif", "Andi", "Andre", "Andrian", "Anjar", "Arif",
    "Aris", "Arya", "Bagus", "Bambang", "Bayu", "Bima", "Bobby", "Budi", "Cahyo", "Chandra",
    "Daffa", "Danang", "Dedi", "Denny", "Dika", "Dimas", "Doni", "Dwi", "Eko", "Faisal",
    "Fajar", "Farhan", "Faris", "Fauzi", "Fikri", "Galih", "Gede", "Gilang", "Hadi", "Hafiz",
    "Hendra", "Heru", "Ihsan", "Ilham", "Indra", "Iqbal", "Irfan", "Joko", "Kevin", "Lukman",
    "Made", "Maulana", "Muhammad", "Nyoman", "Pandu", "Pratama", "Putra", "Raden", "Rafi", "Raka",
    "Rama", "Rangga", "Rendy", "Reza", "Rifky", "Rio", "Riyan", "Rizky", "Rudi", "Sandy",
    "Satria", "Sigit", "Surya", "Taufik", "Teguh", "Tri", "Vicky", "Wahyu", "Wayan", "Yoga",
    "Yudha", "Zainal", "Zaki",
]

LAST_NAMES_MAN = [
    "Alamsyah", "Baskoro", "Budiman", "Darmawan", "Firmansyah", "Gunawan", "Harahap", "Hartanto",
    "Hermawan", "Hidayat", "Hutapea", "Kurniawan", "Kusuma", "Lubis", "Nasution", "Nugroho",
    "Pasaribu", "Perkasa", "Permana", "Pramono", "Prasetyo", "Pratama", "Purnama", "Ramadhan",
    "Santoso", "Saputra", "Setiawan", "Setyo", "Simanjuntak", "Siregar", "Subagyo", "Sugiarto",
    "Suhendra", "Suryanto", "Susanto", "Syahputra", "Tanjung", "Utomo", "Waskito", "Wibowo",
    "Wicaksono", "Wijaya",
]

FIRST_NAMES_WOMAN = [
    "Adinda", "Alya", "Amanda", "Amelia", "Anisa", "Annisa", "Aprilia", "Aulia", "Ayu", "Bella",
    "Cantika", "Citra", "Cut", "Desi", "Dewi", "Dian", "Dina", "Dini", "Dwi", "Elsa",
    "Fadhilah", "Febri", "Fitri", "Fitria", "Gita", "Hana", "Indah", "Intan", "Irma", "Kartika",
    "Laras", "Lestari", "Maya", "Mega", "Melati", "Mira", "Nabila", "Nadia", "Nadya", "Nia",
    "Novita", "Nur", "Nurul", "Oktavia", "Poppy", "Putri", "Rahma", "Rani", "Ratna", "Rina",
    "Rini", "Riska", "Salma", "Salsabila", "Sarah", "Sari", "Silvia", "Siti", "Suci", "Tania",
    "Tari", "Tasya", "Tiara", "Tri", "Ulfa", "Vina", "Widya", "Winda", "Wulan", "Yulia",
    "Yuni", "Zahra",
]

LAST_NAMES_WOMAN = [
    "Anggraini", "Damayanti", "Febrianti", "Handayani", "Hapsari", "Hasanah", "Indriani", "Kusuma",
    "Lestari", "Maharani", "Novitasari", "Nuraini", "Nurhaliza", "Oktaviani", "Permatasari", "Pratiwi",
    "Puspitasari", "Putri", "Rahayu", "Rahmawati", "Rosita", "Safitri", "Setiawati", "Sucipto",
    "Susanti", "Utami", "Wardani", "Widyaningrum", "Wulandari", "Yuliana", "Harahap", "Lubis",
    "Nasution", "Simanjuntak", "Siregar", "Sitompul",
]

FIRST_NAMES = FIRST_NAMES_MAN + FIRST_NAMES_WOMAN
LAST_NAMES = LAST_NAMES_MAN + LAST_NAMES_WOMAN

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
