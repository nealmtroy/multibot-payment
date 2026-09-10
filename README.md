# MultiBot VIP Payment & Referral System (Native PostgreSQL)

Sistem pembayaran otomatis Telegram VIP multi-bot berbasis **Native PostgreSQL (`asyncpg`)** dan gateway donasi **SociaBuzz QRIS**.

Setiap bot yang didaftarkan berjalan secara mandiri dan dinamis di dalam satu proses aplikasi tanpa perlu restart VPS/server, dengan **sistem referral dan saldo pengguna yang terisolasi penuh (100% isolated)** per bot.

---

## ✨ Fitur Utama

1. **Native PostgreSQL (`asyncpg`)**:
   - Koneksi langsung dengan connection pool (`asyncpg`).
   - Query SQL teroptimasi dengan row-level locking (`FOR UPDATE`) untuk mencegah race condition pada penarikan saldo.
   - Auto-migrasi skema database saat aplikasi dijalankan (`schema.sql`).
   - **Bebas dari Supabase REST API / Cloud dependencies**.

2. **Multi-Bot Dynamic Engine**:
   - Tambah & jalankan bot baru secara instan via perintah admin tanpa restart server (`/bot_add <nama_bot> <token>`).
   - Hapus & hentikan bot secara instan (`/bot_del <nama_bot>`).
   - Monitor status seluruh bot aktif secara terpusat (`/bot_list`).

3. **Isolasi Penuh Sistem Referral & Pengguna**:
   - Saldo (*balance*), referral code, counter referral sukses/pending, dan riwayat transaksi **100% terpisah antar-bot** (`PRIMARY KEY (bot_code, user_id)`).
   - Pengguna di Bot A tidak akan berbagi saldo atau referral link dengan pengguna di Bot B.

4. **Multi-Configuration Packages Per Bot**:
   - Setiap bot memiliki katalog paket VIP tersendiri (`/package_add <bot_code> <kode> <Nama Group>|<vip_chat_id>|<harga>`).
   - Pengguna di masing-masing bot hanya melihat paket VIP milik bot tersebut.

5. **Otomasi Pembayaran SociaBuzz QRIS**:
   - Generate dynamic QRIS otomatis dengan nominal unik.
   - Polling status pembayaran otomatis di latar belakang.
   - Auto-generate single-use invite link group/channel VIP setelah pembayaran terverifikasi.
   - Auto-delete pesan QRIS setelah kadaluarsa atau lunas.

6. **Interactive Withdrawal System (Tarik Saldo)**:
   - Pengguna dapat mengajukan penarikan komisi referral step-by-step (Nominal -> No HP -> E-Wallet -> Nama Pemilik Akun).
   - Notifikasi pengajuan masuk ke `LOG_CHAT_ID` dengan detail lengkap.
   - Admin dapat menyetujui (`/approve <id>`) atau menolak (`/reject <id>`) langsung dari grup log.

7. **Scheduled Daily Broadcast**:
   - Jadwalkan broadcast harian otomatis (`/set_broadcasttime HH:MM`) atau matikan (`/set_broadcasttime off`).
   - Rate-limiting dan penanganan Telegram `FloodWait` otomatis.

---

## 📋 Skema Database (PostgreSQL)

Tabel utama di `schema.sql`:
- `bots`: Menyimpan token, username, nama, dan status bot (`active`, `paused`, `stopped`).
- `packages`: Menyimpan paket VIP (`bot_code`, `code`, `name`, `vip_chat_id`, `amount`, `invite_expire_hours`).
- `users`: Data pengguna terisolasi (`bot_code`, `user_id`, `balance`, `referral_code`, dll).
- `referrals`: Relasi pengundang dan yang diundang per bot (`bot_code`, `referrer_user_id`, `invited_user_id`).
- `withdrawals`: Antrean penarikan saldo per bot.
- `payments`: Transaksi pembayaran SociaBuzz per bot.
- `settings`: Konfigurasi runtime (`log_chat_id`, `vip_chat_id`, `broadcast_time`, dll).
- `broadcast_messages`: Pesan broadcast aktif per bot.

---

## 🚀 Panduan Instalasi & Menjalankan

### 1. Prasyarat
- Python 3.11+
- PostgreSQL 14+ (Local, VPS, atau Docker)

### 2. Siapkan PostgreSQL
Jika menggunakan **Docker**, Anda bisa menjalankan PostgreSQL dalam hitungan detik:
```bash
docker run -d \
  --name multibot-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=rahasia \
  -e POSTGRES_DB=multibot_db \
  -p 5432:5432 \
  --restart unless-stopped \
  postgres:16-alpine
```

Jika menggunakan **VPS Ubuntu (apt)**:
```bash
sudo apt update && sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql -c "CREATE DATABASE multibot_db;"
sudo -u postgres psql -c "CREATE USER botuser WITH ENCRYPTED PASSWORD 'rahasia';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE multibot_db TO botuser;"
```

### 3. Clone / Siapkan Proyek
```bash
cd D:\PROJECT\Telegram\MultiBot_Payment
python -m venv venv
venv\Scripts\activate   # Di Windows
# source venv/bin/activate # Di Linux
pip install -r requirements.txt
```

### 4. Konfigurasi Environment (`.env`)
Salin file `.env.example` ke `.env`:
```bash
cp .env.example .env
```
Isi konfigurasi berikut:
```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789
TELEGRAM_BOT_TOKEN=123456789:ABCDefgh-MasterBotToken

# Log & VIP fallback
LOG_CHAT_ID=-1001234567890
VIP_CHAT_ID=

# Native PostgreSQL URL
DATABASE_URL=postgresql://postgres:rahasia@localhost:5432/multibot_db

# SociaBuzz Gateway
SOCIABUZZ_USERNAME=akun_sociabuzz
SOCIABUZZ_COOKIE=
PAYMENT_AMOUNT=50000

ADMIN_USER_IDS=123456789
LOG_LEVEL=INFO
```

### 5. Jalankan Bot
```bash
python run.py
```
Aplikasi akan otomatis menginisialisasi skema PostgreSQL (`schema.sql`) jika tabel belum ada, lalu mengaktifkan Master Bot dan seluruh bot anak yang tersimpan di database.

---

## 🤖 Perintah Admin (Lewat `LOG_CHAT_ID`)

Semua manajemen dilakukan lewat Telegram di grup `LOG_CHAT_ID` tanpa perlu SSH ke server:

### 1. Manajemen Bot Anak (Multi-Bot)
| Perintah | Deskripsi |
|---|---|
| `/bot_add <nama_bot> <bot_token>` | Daftarkan bot baru & langsung jalan seketika tanpa restart |
| `/bot_del <nama_bot>` | Hentikan & hapus bot anak dari sistem |
| `/bot_list` | Tampilkan daftar semua bot anak beserta statusnya |

### 2. Manajemen Paket VIP
Format: `/package_add <bot_code> <kode> <Nama Group>|<vip_chat_id>|<harga>`
- Contoh:
  `/package_add botpayment1 vip1 Group VIP Premium 1|-100192837465|50000`
- Daftar paket:
  `/package_list botpayment1`
- Hapus paket:
  `/package_del botpayment1 vip1`

### 3. Manajemen Tarik Saldo (Withdrawal)
| Perintah | Deskripsi |
|---|---|
| `/tarik_list` | Lihat antrean penarikan saldo yang pending |
| `/approve <id>` | Setujui penarikan & kirim notifikasi sukses ke user |
| `/reject <id>` | Tolak penarikan, saldo user otomatis dikembalikan |

### 4. Broadcast (Terisolasi 100% Per Bot)
| Perintah | Deskripsi |
|---|---|
| `/set_broadcast <nama_bot>` | Set pesan broadcast khusus bot (reply ke teks/media) |
| `/set_broadcasttime <nama_bot> HH:MM` | Jadwalkan waktu broadcast harian per bot (WIB) |
| `/set_broadcasttime <nama_bot> off` | Nonaktifkan broadcast otomatis untuk bot tersebut |
| `/test_broadcast <nama_bot>` | Uji coba kirim broadcast bot tersebut ke admin |
| `/broadcast_status [nama_bot]` | Cek status, jadwal, & target user broadcast tiap bot |

---

## 🧪 Menjalankan Unit Tests
Proyek ini dilengkapi test suite lengkap:
```bash
python -m unittest discover -s tests
```
