import html
from telethon import Button
from vip_bot.helpers import (
    format_custom_qris_expiry,
    format_rupiah,
    format_button_amount,
    telegram_user_link,
    internal_telegram_chat_url,
    normalize_package_code,
)

def qris_caption(package, inv_id, checkout_amount, final_amount, expires):
    public_invoice = html.escape(inv_id)
    package_name = html.escape(package["name"])
    human_expires = format_custom_qris_expiry(expires) if expires else ""
    detail_lines = [
        f"<b>Kode Pesanan</b>: <code>{public_invoice}</code>",
        f"<b>Paket</b>: {package_name}",
        f"<b>Nominal Paket</b>: {format_rupiah(checkout_amount)}",
    ]
    if final_amount:
        detail_lines.append(f"<b>Nominal QRIS</b>: {html.escape(final_amount)}")
    if human_expires:
        detail_lines.append(f"<b>⏳ Batas Bayar</b>: {human_expires}")
    detail_lines_str = "\n".join(detail_lines)
    lines = [
        f"<b>Akses {package_name}</b>",
        "",
        f"<blockquote>{detail_lines_str}</blockquote>",
        "",
        "📌 <b>Aturan pembayaran</b>",
        "• Scan QRIS ini lalu bayar sesuai nominal QRIS.",
        "• Bayar 1 kali saja, jangan diulang.",
        "• QRIS ini unik khusus pesanan kamu.",
        "• Status dicek otomatis, tidak perlu kirim bukti transfer.",
        "",
        f"Setelah pembayaran terdeteksi, link akses {package_name} akan langsung dikirim otomatis.",
    ]
    return "\n".join(lines)


def custom_qris_caption(inv_id, checkout_amount, final_amount, expires, user):
    public_invoice = html.escape(inv_id)
    human_expires = format_custom_qris_expiry(expires) if expires else ""
    detail_lines = [
        f"<b>Kode Pesanan</b>: <code>{public_invoice}</code>",
        f"<b>Requester</b>: {telegram_user_link(user)} (<code>{user.id}</code>)",
        f"<b>Nominal Custom</b>: {format_rupiah(checkout_amount)}",
    ]
    if final_amount:
        detail_lines.append(f"<b>Nominal QRIS</b>: {html.escape(final_amount)}")
    if human_expires:
        detail_lines.append(f"<b>⏳ Batas Bayar</b>: {human_expires}")
    detail_lines_str = "\n".join(detail_lines)
    lines = [
        "🧾 <b>Custom QRIS</b>",
        "",
        f"<blockquote>{detail_lines_str}</blockquote>",
        "",
        "📌 <b>Aturan pembayaran</b>",
        "• Bayar <b>sesuai nominal QRIS</b>.",
        "• Bayar <b>1 kali saja</b>, jangan diulang.",
        "• Status akan dicek otomatis.",
    ]
    return "\n".join(lines)


def paid_message(invite_link, package_name="VIP", invite_hours=24, group_url=""):
    safe_package_name = html.escape(package_name)
    safe_group_url = html.escape(group_url or "")
    group_link = f'<a href="{safe_group_url}">Buka {safe_package_name}</a>' if safe_group_url else f"Buka {safe_package_name}"
    return (
        "✅ <b>Pembayaran berhasil terdeteksi</b>\n\n"
        f"Akses <b>{safe_package_name}</b> kamu sudah aktif.\n\n"
        "1️⃣ Join group lewat link ini dulu:\n"
        f"{html.escape(invite_link)}\n\n"
        "2️⃣ Setelah sudah join, buka group lagi lewat link ini:\n"
        f"{group_link}\n\n"
        f"⚠️ Link join hanya bisa dipakai <b>1 kali</b> dan berlaku <b>{int(invite_hours)} jam</b>."
    )


def paid_message_buttons(payment):
    url = internal_telegram_chat_url(payment.get("vip_chat_id"))
    if not url:
        return None
    package_name = (payment.get("package_name") or "VIP").strip() or "VIP"
    return [[Button.url(f"Buka {package_name}", url)]]


def invalid_payment_message():
    return (
        "⚠️ <b>QRIS sebelumnya sudah tidak aktif</b>\n\n"
        "Slot VIP kamu masih bisa diamankan. Buat QRIS baru sekarang, selesaikan pembayaran 1 kali, "
        "dan link member VIP akan dikirim otomatis setelah pembayaran terdeteksi."
    )


def timeout_payment_message():
    return (
        "⏳ <b>Invoice VIP sudah kedaluwarsa</b>\n\n"
        "QRIS lama sudah ditutup supaya tidak salah scan. Klik tombol di bawah untuk checkout ulang "
        "dan lanjut masuk to group member VIP."
    )


def main_menu_button_labels():
    return ["🛒 Beli Group VIP", "👤 Profile", "💰 Tarik Saldo"]


def main_menu_buttons():
    buy_label, profile_label, withdrawal_label = main_menu_button_labels()
    return [[Button.text(buy_label, resize=True)], [Button.text(profile_label, resize=True), Button.text(withdrawal_label, resize=True)]]


def main_menu_keyboard_text(user, bot_name=""):
    from vip_bot.helpers import display_name
    name = display_name(user) or "kak"
    return f"Halo {html.escape(name)}, selamat datang!\nSilakan pilih menu di bawah untuk mengakses layanan VIP."


def default_package(config, bot_code="default", vip_chat_id=None):
    return {
        "bot_code": bot_code,
        "code": "default",
        "name": "VIP",
        "amount": config.payment_amount,
        "vip_chat_id": vip_chat_id or config.vip_chat_id,
        "invite_expire_hours": config.invite_expire_hours,
    }


def telethon_button_style(style_str):
    if not style_str:
        return None
    s = str(style_str).lower().strip()
    if s in ("primary", "danger", "success"):
        return s
    return None


def button_style_badge(style_str):
    s = str(style_str or "").lower().strip()
    if s == "primary":
        return "🔵 Primary (Biru)"
    if s == "danger":
        return "🔴 Danger (Merah)"
    if s == "success":
        return "🟢 Success (Hijau)"
    return "⚪ Standar"


def package_label(package):
    if package.get("button_label"):
        return package["button_label"]
    return f"{package['name']} - {format_button_amount(package['amount'])}"


def package_buttons(config, store_or_packages, bot_code="default", vip_chat_id=None, columns=1):
    if isinstance(store_or_packages, list):
        packages = store_or_packages
    elif hasattr(store_or_packages, "list_packages"):
        res = store_or_packages.list_packages(bot_code=bot_code)
        import inspect
        packages = [] if inspect.iscoroutine(res) else (res or [])
    else:
        packages = []

    if not packages:
        packages = [default_package(config, bot_code=bot_code, vip_chat_id=vip_chat_id)]

    # Check if explicit row_index is used by any package
    has_explicit_rows = any(int(p.get("row_index") or 0) > 0 for p in packages)
    if has_explicit_rows:
        from collections import defaultdict
        row_map = defaultdict(list)
        for p in packages:
            r = int(p.get("row_index") or 0)
            if r == 0:
                r = 9999
            btn = Button.inline(
                package_label(p),
                data=f"pkg:{p['code']}",
                style=telethon_button_style(p.get("button_style")),
            )
            row_map[r].append(btn)
        return [row_map[r] for r in sorted(row_map.keys())]

    cols = max(1, min(int(columns or 1), 3))
    raw_buttons = [
        Button.inline(
            package_label(p),
            data=f"pkg:{p['code']}",
            style=telethon_button_style(p.get("button_style")),
        )
        for p in packages
    ]

    rows = []
    for i in range(0, len(raw_buttons), cols):
        rows.append(raw_buttons[i : i + cols])
    return rows


def package_detail_card(pkg):
    status = "🟢 Aktif" if pkg.get("active", True) else "🔴 Nonaktif"
    style_badge = button_style_badge(pkg.get("button_style"))
    custom_lbl = f"<code>{html.escape(pkg['button_label'])}</code>" if pkg.get("button_label") else "<i>(Default: Nama - Harga)</i>"
    order_num = pkg.get("sort_order", 100)
    expire_h = pkg.get("invite_expire_hours", 0)
    expire_text = f"{expire_h} Jam" if expire_h > 0 else "Permanen (0 jam)"

    return (
        f"📦 <b>Detail & Konfigurasi Paket VIP</b>\n"
        f"• Bot: <code>{html.escape(pkg.get('bot_code', 'default'))}</code>\n"
        f"• Kode Paket: <code>{html.escape(pkg['code'])}</code>\n\n"
        f"📝 <b>Nama Paket</b>: <b>{html.escape(pkg['name'])}</b>\n"
        f"💰 <b>Harga</b>: <b>{format_rupiah(pkg['amount'])}</b>\n"
        f"🆔 <b>VIP Chat ID</b>: <code>{pkg['vip_chat_id']}</code>\n"
        f"⏳ <b>Durasi Link Expire</b>: <b>{expire_text}</b>\n"
        f"🎨 <b>Warna Tombol</b>: {style_badge}\n"
        f"🏷️ <b>Label Tombol</b>: {custom_lbl}\n"
        f"↕️ <b>Nilai Urutan (Sort Order)</b>: <code>{order_num}</code>\n"
        f"📌 <b>Status</b>: {status}\n\n"
        f"<i>Pilih data yang ingin diubah melalui tombol di bawah:</i>"
    )


def package_list_text(packages, bot_code=None, columns=1):
    title = f"📦 <b>Daftar Paket VIP ({html.escape(bot_code)}):</b>\n" if bot_code else "📦 <b>Daftar Paket VIP:</b>\n"
    if not packages:
        return title + "<i>Belum ada paket/group yang didaftarkan.</i>"
    lines = [title]
    for idx, pkg in enumerate(packages, 1):
        status = "🟢 Aktif" if pkg.get("active", True) else "🔴 Nonaktif"
        style_badge = button_style_badge(pkg.get("button_style"))
        lbl = f" [Label: <code>{html.escape(pkg['button_label'])}</code>]" if pkg.get("button_label") else ""
        lines.append(
            f"{idx}. <code>{html.escape(pkg['code'])}</code> ({html.escape(pkg.get('bot_code', 'default'))}) - "
            f"<b>{html.escape(pkg['name'])}</b> | <code>{pkg['vip_chat_id']}</code> | "
            f"<b>{format_button_amount(pkg['amount'])}</b> [{status}]\n"
            f"   • Tombol: {style_badge}{lbl} | Urutan: <code>{pkg.get('sort_order', 100)}</code>"
        )
    return "\n".join(lines)


def bot_list_text(bots):
    if not bots:
        return "🤖 <b>Daftar Bot Payment:</b>\n<i>Belum ada bot yang didaftarkan. Gunakan menu ➕ Tambah Bot Baru</i>"
    lines = ["🤖 <b>Daftar Bot Payment Aktif:</b>\n"]
    for idx, b in enumerate(bots, 1):
        status_icon = "🟢" if b["status"] == "online" else "🔴"
        username = f"(@{b['bot_username']})" if b.get("bot_username") else ""
        lines.append(
            f"{idx}. {status_icon} <b>{html.escape(b['bot_code'])}</b> {username}\n"
            f"   Nama: {html.escape(b['bot_name'])} | Status: <code>{b['status']}</code> | Group: <b>{b['package_count']}</b>"
        )
    return "\n".join(lines)


def admin_command_list_text():
    return (
        "🤖 <b>Command Multi-Bot:</b>\n"
        "• <code>/bot_add &lt;nama_bot&gt; &lt;bot_token&gt;</code> - Tambah & jalankan bot langsung tanpa restart\n"
        "• <code>/bot_list</code> - Cek status semua bot\n"
        "• <code>/bot_stop &lt;nama_bot&gt;</code> - Matikan bot tertentu\n"
        "• <code>/bot_start &lt;nama_bot&gt;</code> - Hidupkan bot kembali\n"
        "• <code>/bot_delete &lt;nama_bot&gt;</code> - Hapus bot dari sistem\n\n"
        "📦 <b>Command Group VIP (Per Bot):</b>\n"
        "• <code>/package_add &lt;nama_bot&gt; &lt;kode&gt; &lt;Nama Group&gt;|&lt;chat_id&gt;|&lt;harga&gt;</code>\n"
        "• <code>/package_list [nama_bot]</code> - List group VIP per bot\n"
        "• <code>/package_delete &lt;nama_bot&gt; &lt;kode&gt;</code> - Nonaktifkan group\n\n"
        "⚙️ <b>Command Setting & Utilitas:</b>\n"
        "• <code>/menu</code> (atau <code>/commands</code>) - Buka menu dashboard interaktif\n"
        "• <code>/custom &lt;nominal&gt;</code> - Buat QRIS manual khusus admin\n"
        "• <code>/chatid</code> - Cek ID chat grup ini\n"
        "• <code>/setvip &lt;chat_id|here&gt;</code> - Set VIP chat default\n"
        "• <code>/setlog &lt;chat_id|here&gt;</code> - Set channel log admin\n"
        "• <code>/config</code> - Lihat konfigurasi global\n"
        "\n📢 <b>Command Broadcast (Per Bot):</b>\n"
        "• <code>/set_broadcast [nama_bot]</code> - Simpan pesan broadcast (reply ke pesan)\n"
        "• <code>/set_broadcasttime &lt;nama_bot&gt; &lt;HH:MM|off&gt;</code> - Jadwalkan broadcast harian per bot\n"
        "• <code>/test_broadcast [nama_bot]</code> - Uji broadcast per bot ke admin\n"
        "• <code>/broadcast_status [nama_bot]</code> - Cek status & jadwal broadcast tiap bot\n"
    )


def admin_main_menu_keyboard():
    return [
        [Button.text("🤖 Kelola Bot Payment", resize=True), Button.text("📦 Kelola Paket VIP")],
        [Button.text("📢 Kelola Broadcast"), Button.text("💰 Antrean Penarikan")],
        [Button.text("📊 Status Sistem"), Button.text("⚙️ Pengaturan")],
    ]


def admin_bot_menu_keyboard():
    return [
        [Button.text("➕ Tambah Bot Baru", resize=True), Button.text("📋 Daftar Semua Bot")],
        [Button.text("⏹️ Hentikan Bot"), Button.text("▶️ Hidupkan Bot")],
        [Button.text("🗑️ Hapus Bot"), Button.text("🔙 Menu Utama")],
    ]


def admin_package_menu_keyboard():
    return [
        [Button.text("➕ Tambah Paket VIP", resize=True), Button.text("✏️ Edit Paket VIP")],
        [Button.text("📑 Daftar Paket VIP"), Button.text("🗑️ Hapus Paket VIP")],
        [Button.text("🔙 Menu Utama")],
    ]


def admin_broadcast_menu_keyboard():
    return [
        [Button.text("📝 Set Pesan Broadcast", resize=True), Button.text("⏰ Set Jadwal Broadcast")],
        [Button.text("🧪 Test Broadcast"), Button.text("📊 Status Broadcast")],
        [Button.text("🔙 Menu Utama")],
    ]


def cancel_keyboard():
    return [
        [Button.text("❌ Batal", resize=True)]
    ]
