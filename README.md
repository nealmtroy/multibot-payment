# MultiBot VIP Payment & Referral System (Native PostgreSQL)

Sistem pembayaran otomatis Telegram VIP multi-bot berbasis **Native PostgreSQL (`asyncpg`)** dan gateway donasi **SociaBuzz QRIS**.

Sistem ini memiliki **1 Master Management Bot** khusus administrator untuk mengontrol seluruh bot payment, paket, broadcast, dan penarikan saldo secara langsung melalui **Private Chat (DM)** tanpa perlu mengetik perintah di grup log.

---

## ✨ Fitur Utama

1. **Master Management Bot Khusus Admin**:
   - Token bot di `.env` (`TELEGRAM_BOT_TOKEN`) difungsikan secara eksklusif sebagai **Master Management Bot**.
   - **Keamanan Ketat**: Hanya akun Telegram yang terdaftar di `ADMIN_USER_IDS` yang dapat mengakses dan menjalankan perintah di bot ini. Pengguna umum yang mengirim pesan ke bot ini akan langsung ditolak (`⛔ Akses Ditolak`).
   - **Private Chat Management**: Kelola seluruh bot, paket, broadcast, dan approval penarikan saldo langsung dari DM pribadi dengan Management Bot (tidak perlu lagi mengetik command di grup `LOG_CHAT_ID`).
   - **Pengirim Log Terpusat**: Seluruh notifikasi sistem, transaksi pembayaran QRIS, pengajuan penarikan, dan pendaftaran bot baru dikirim ke `LOG_CHAT_ID` melalui Management Bot ini.

2. **Notifikasi Startup & Bot Baru**:
   - Saat aplikasi dijalankan, Management Bot mengirimkan rekap bot yang aktif ke `LOG_CHAT_ID`:
     - Jika ada bot aktif:
       ```text
       🚀 MultiBot Payment has been started!
       Bot yang berjalan:
       1. 🟢 botpayment1 (@pay1_bot) - Paket VIP: 3
       2. 🟢 botpayment2 (@pay2_bot) - Paket VIP: 2
       ```
     - Jika belum ada bot aktif:
       ```text
       🚀 MultiBot Payment has been started!
       Tidak ada bot yang aktif saat ini.
       ```
   - Saat admin menambahkan bot payment baru (`/bot_add`), Management Bot otomatis mengirim log notifikasi ke `LOG_CHAT_ID`:
     ```text
     🤖 Bot Payment Baru Aktif!
     • Nama Bot: botpayment1
     • Username: @pay1_bot
     • Kode Bot: botpayment1
     • Ditambahkan oleh Admin: 123456789
     • Status: 🟢 Online & Siap Digunakan
     ```

3. **Bot Payment Anak (Child Bots)**:
   - Didaftarkan via `/bot_add <nama_bot> <bot_token>` dan langsung jalan otomatis tanpa restart server.
   - Bertugas khusus melayani pembeli/member (Menu Pembelian VIP, QRIS SociaBuzz, Profil, Referral, dan Tarik Saldo).

4. **Isolasi Penuh Sistem Referral & Saldo (100% Isolated)**:
   - Saldo (*balance*), referral code, counter referral sukses/pending, dan riwayat transaksi **100% terpisah antar-bot** (`PRIMARY KEY (bot_code, user_id)`).
   - Pengguna di Bot A tidak akan berbagi saldo atau referral link dengan pengguna di Bot B.

5. **Isolasi Broadcast Per Bot**:
   - Setiap bot memiliki pesan broadcast, jadwal kirim harian (`HH:MM WIB`), dan target audiens tersendiri.

6. **Native PostgreSQL (`asyncpg`)**:
   - Menggunakan connection pool performa tinggi.
   - Query SQL teroptimasi dengan row-level locking (`FOR UPDATE`) pada saat penarikan saldo.
   - Auto-migrasi skema database saat startup (`schema.sql`). Bebas dari Supabase/Cloud API.

---

## 🚀 Panduan Setup & Menjalankan

### 1. Prasyarat
- Python 3.11+
- PostgreSQL 14+ (Local, VPS, atau Docker)

### 2. Siapkan PostgreSQL
Jika menggunakan **Docker**:
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

### 3. Konfigurasi Environment (`.env`)
Salin file `.env.example` ke `.env`:
```bash
cp .env.example .env
```
Isi konfigurasi:
```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789

# Master Management Bot Token (Khusus Administrator)
TELEGRAM_BOT_TOKEN=123456789:ABCDefgh-ManagementBotToken

# Log Chat & Fallback VIP
LOG_CHAT_ID=-1001234567890
VIP_CHAT_ID=

# Native PostgreSQL URL
DATABASE_URL=postgresql://postgres:rahasia@localhost:5432/multibot_db

# SociaBuzz Gateway
SOCIABUZZ_USERNAME=akun_sociabuzz
SOCIABUZZ_COOKIE=
PAYMENT_AMOUNT=50000

# User ID Telegram Admin yang berhak memakai Management Bot
ADMIN_USER_IDS=123456789
LOG_LEVEL=INFO
```

### 4. Jalankan Aplikasi
```bash
python run.py
```
Aplikasi akan otomatis menginisialisasi skema PostgreSQL (`schema.sql`), menyalakan Master Management Bot, mengaktifkan seluruh payment bot anak yang tersimpan di database, dan mengirim notifikasi startup ke `LOG_CHAT_ID`.

---

## 🤖 Perintah Admin (Langsung di Private Chat Management Bot)

Buka Private Chat dengan Master Management Bot, lalu kirim perintah berikut:

### 1. Manajemen Bot Payment
| Perintah | Deskripsi |
|---|---|
| `/start` | Tampilkan dashboard status sistem dan daftar perintah |
| `/bot_add <nama_bot> <bot_token>` | Tambah & jalankan bot payment baru seketika tanpa restart |
| `/bot_del <nama_bot>` | Hapus bot payment dari sistem |
| `/bot_stop <nama_bot>` | Matikan/pause bot payment sementara |
| `/bot_start <nama_bot>` | Hidupkan kembali bot payment yang mati |
| `/bot_list` | Tampilkan status seluruh bot payment |

### 2. Manajemen Paket VIP (Per Bot)
Format: `/package_add <nama_bot> <kode> <Nama Group>|<vip_chat_id>|<harga>`
- Contoh:
  `/package_add botpayment1 vip1 Group VIP Premium 1|-100192837465|50000`
- Daftar paket:
  `/package_list botpayment1`
- Hapus paket:
  `/package_delete botpayment1 vip1`

### 3. Manajemen Tarik Saldo (Withdrawal)
| Perintah | Deskripsi |
|---|---|
| `/tarik_list` | Lihat antrean penarikan saldo komisi yang pending |
| `/approve <id>` | Setujui penarikan & kirim notifikasi sukses ke user |
| `/reject <id>` | Tolak penarikan, saldo user otomatis dikembalikan |

### 4. Broadcast (Terisolasi Per Bot)
| Perintah | Deskripsi |
|---|---|
| `/set_broadcast <nama_bot>` | Set pesan broadcast khusus bot (reply ke pesan teks/media) |
| `/set_broadcasttime <nama_bot> HH:MM` | Jadwalkan waktu broadcast harian (WIB) |
| `/set_broadcasttime <nama_bot> off` | Nonaktifkan broadcast otomatis bot tersebut |
| `/test_broadcast <nama_bot>` | Uji coba kirim pesan broadcast bot ke admin |
| `/broadcast_status [nama_bot]` | Cek status jadwal dan target user broadcast tiap bot |

---

## 🧪 Menjalankan Unit Tests
```bash
python -m unittest discover -s tests
```
