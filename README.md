# MultiBot Payment System (Telegram VIP Bot)

Sistem pembayaran otomatis QRIS SociaBuzz untuk banyak bot Telegram (**Multi-Bot & Multi-Configuration**) dalam 1 instance running, dengan sistem referral, saldo komisi, dan grup VIP yang **100% terisolasi antar bot**.

Sistem ini mendukung **Zero Downtime / Hot Reload**: Bot baru dapat ditambahkan, dihidupkan, dan dimatikan langsung melalui chat room admin (`LOG_CHAT_ID`) tanpa perlu restart server atau PM2.

---

## 🚀 Fitur Utama

1. **Multi-Bot Dinamis (Tanpa Restart Server)**
   - Tambah bot baru via command `/bot_add <nama_bot> <bot_token>`.
   - Bot baru langsung aktif dan memproses chat pengguna detik itu juga.
   - Menggunakan `asyncio.Task` dan sesi Telethon terisolasi (`sessions/bot_{bot_code}.session`).
2. **Isolasi Penuh 100% Antar Bot**
   - **Group VIP / Paket**: Group VIP terikat pada bot tertentu (`botpayment1` -> Group 1, 2, 3; `botpayment2` -> Group 4, 5, 6).
   - **Referral & Saldo**: User profile dan saldo komisi dicatat per `(bot_code, user_id)`. Saldo komisi di bot 1 tidak akan pernah tercampur ke bot 2.
   - **Link Referral Otomatis**: Link referral digenerate sesuai username masing-masing bot (`https://t.me/<username>?start=ref_<code>`).
   - **Withdrawal / Tarik Saldo**: Pengajuan penarikan dana terisolasi per bot, dengan detail bot tercantum di notifikasi admin.
3. **Pembayaran Otomatis & Pengiriman Link VIP**
   - Menggunakan SociaBuzz QRIS.
   - Centralized polling loop memeriksa status pembayaran dan secara otomatis meminta bot terkait mengirimkan link invite 1x pakai kepada pembeli.

---

## 📋 Daftar Command Admin (di `LOG_CHAT_ID`)

Semua manajemen dilakukan langsung dari grup/channel log admin:

### 1. Manajemen Bot:
- **`/bot_add <nama_bot> <bot_token>`**  
  Menambahkan bot baru dan langsung menjalankannya tanpa restart VPS.  
  *Contoh:* `/bot_add botpayment1 123456789:AAHxxxxxx...`
- **`/bot_list`**  
  Melihat daftar semua bot terdaftar, statusnya (🟢 Online / 🔴 Stopped), username, dan jumlah group VIP-nya.
- **`/bot_stop <nama_bot>`**  
  Mematikan sementara bot tertentu tanpa mengganggu bot lain.  
  *Contoh:* `/bot_stop botpayment1`
- **`/bot_start <nama_bot>`**  
  Menjalankan kembali bot yang sebelumnya di-stop.  
  *Contoh:* `/bot_start botpayment1`
- **`/bot_delete <nama_bot>`**  
  Menghapus bot dari memori dan database.  
  *Contoh:* `/bot_delete botpayment1`

### 2. Manajemen Group VIP (Paket Per Bot):
- **`/package_add <nama_bot> <kode> <Nama Group>|<chat_id>|<harga>`**  
  Mendaftarkan group VIP khusus untuk bot tertentu.  
  *Contoh untuk botpayment1:*  
  ```
  /package_add botpayment1 vip1 Group VIP 1|-1001111111111|50000
  /package_add botpayment1 vip2 Group VIP 2|-1002222222222|75000
  /package_add botpayment1 vip3 Group VIP 3|-1003333333333|100000
  ```
  *Contoh untuk botpayment2:*  
  ```
  /package_add botpayment2 vip4 Group VIP 4|-1004444444444|50000
  /package_add botpayment2 vip5 Group VIP 5|-1005555555555|75000
  /package_add botpayment2 vip6 Group VIP 6|-1006666666666|100000
  ```
- **`/package_list [nama_bot]`**  
  Melihat daftar group VIP untuk bot tertentu (atau semua bot jika nama bot dikosongkan).
- **`/package_delete <nama_bot> <kode>`**  
  Menonaktifkan group VIP tertentu.

### 3. Utilitas & Setting:
- `/custom <nominal>` - Membuat QRIS nominal manual khusus admin.
- `/chatid` - Mengetahui chat ID grup saat ini.
- `/setvip <chat_id|here>` - Mengatur default chat ID VIP fallback.
- `/setlog <chat_id|here>` - Mengatur grup/channel log admin runtime.
- `/config` - Menampilkan status konfigurasi dan jumlah bot aktif.
- `/commands` - Menampilkan daftar panduan command.

---

## 🛠️ Setup & Database Migration

1. **Jalankan Skema Supabase**:  
   Buka SQL Editor di Dashboard Supabase kamu, lalu copy & paste seluruh isi file [`supabase_schema.sql`](supabase_schema.sql) dan klik **Run**.
2. **Setup File `.env`**:
   Salin `.env.example` ke `.env` dan isi token Master Bot dan kredensial Supabase.
   ```bash
   TELEGRAM_API_ID=...
   TELEGRAM_API_HASH=...
   TELEGRAM_BOT_TOKEN=...       # Token Master Bot (Admin Controller)
   LOG_CHAT_ID=-100...          # ID Chatroom Admin
   SUPABASE_URL=...
   SUPABASE_SERVICE_ROLE_KEY=...
   SOCIABUZZ_USERNAME=...
   ```
3. **Jalankan Aplikasi**:
   ```bash
   python telegram_vip_bot.py
   ```
   Atau menggunakan PM2:
   ```bash
   pm2 start telegram_vip_bot.py --name multibot-pay --interpreter venv/bin/python
   pm2 save
   ```

---

## 🧪 Pengujian Unit Test

Jalankan seluruh suite unit test:
```bash
python -m unittest discover -s tests
```
Semua 24 unit test mencakup pengujian isolasi referral, kalkulasi penarikan saldo, parsing argumen multi-bot, dan routing client.
