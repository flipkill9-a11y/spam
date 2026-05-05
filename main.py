"""
Telegram Guruh Spam Filter Bot — Kengaytirilgan versiya v2
==========================================================
Yangi xususiyatlar (v2):
  ✅ Reply orqali moderatsiya (/warn /mute /ban /kick)
  ✅ Spam kalit so'zlarni panel orqali boshqarish
  ✅ Bloklangan domenlarni panel orqali boshqarish
  ✅ Spam log kanali (har spam → log kanalga yuboriladi)
  ✅ Guruhlar ro'yxati paneldan ko'rish
  ✅ Sozlamalar (ban chegarasi, mute vaqti) paneldan o'zgartirish
  ✅ Guruhni paneldan qo'shish (CHANNEL_IDS ni qo'lda kiritmay)
  ✅ Spam statistikasi har guruh uchun alohida
  ✅ Barcha avvalgi funksiyalar saqlanib qoldi

Ishlatish:
  1. pip install -r requirements.txt
  2. .env faylni to'ldiring
  3. python spam_filter_bot.py
"""

import re, logging, os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import (
    Update, ChatPermissions,
    InlineKeyboardButton, InlineKeyboardMarkup, Bot,
)
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# SOZLAMALAR  (.env dan o'qiladi)
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN             = os.getenv("BOT_TOKEN", "")
SUPER_ADMIN_ID        = int(os.getenv("SUPER_ADMIN_ID", 0))
LOG_CHANNEL_ID_RAW    = os.getenv("LOG_CHANNEL_ID", "")   # Spam log kanali (ixtiyoriy)
CHANNEL_IDS_RAW       = os.getenv("CHANNEL_IDS", "")
SUB_CHANNELS_RAW      = os.getenv("SUB_CHANNELS", "")

# ═══════════════════════════════════════════════════════════════
# SPAM FILTRI — dinamik (panel orqali o'zgartiriladi)
# ═══════════════════════════════════════════════════════════════
settings: dict = {
    "ban_after_warnings":    int(os.getenv("BAN_AFTER_WARNINGS", 3)),
    "mute_duration_minutes": int(os.getenv("MUTE_DURATION_MINUTES", 60)),
    "max_links":             int(os.getenv("MAX_LINKS_ALLOWED", 2)),
    "max_mentions":          int(os.getenv("MAX_MENTIONS", 3)),
    "caps_threshold":        float(os.getenv("CAPS_THRESHOLD", 0.70)),
    "min_caps_length":       int(os.getenv("MIN_CAPS_LENGTH", 20)),
}

BLOCKED_DOMAINS: list[str] = [
    "alijahon.uz", "bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "shorturl.at"
]
ALLOWED_DOMAINS: list[str] = []

SPAM_KEYWORDS: list[str] = [
    "profilimda", "profilida", "mening kanalim", "mening profilim",
    "profilga kiring", "profilimga kiring", "profile da",
    "buyurtma bering", "buyurtma berish", "narxi:", "narx :",
    "chegirma", "skidka", "discount", "aksiya", "promo kodi",
    "sotib oling", "xarid qiling",
    "zarabot", "earn money", "passive income",
    "crypto signal", "forex signal", "invest now",
    "subscribe", "follow me", "click here", "click link",
    "besplatno", "tekin", "free gift",
    "bot orqali", "botga yozing", "referral", "referal",
    "100% kafolat", "guarantee",
]

# ═══════════════════════════════════════════════════════════════
# XOTIRA (RAM)
# ═══════════════════════════════════════════════════════════════
user_warnings:  dict[int, int]  = {}
group_stats:    dict[int, dict] = {}   # {chat_id: {"title": ..., "spam": N}}
welcome_msgs:   dict[int, str]  = {}
bot_admins:     set[int]        = set()
bc_sessions:    dict[int, dict] = {}
mod_sessions:   dict[int, dict] = {}
known_groups:   dict[int, str]  = {}   # {chat_id: title} — bot admin bo'lgan guruhlar

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# YORDAMCHI
# ═══════════════════════════════════════════════════════════════

def mention(user) -> str:
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{user.full_name}</a>'


def is_bot_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID or user_id in bot_admins


async def get_tg_admin(chat_id: int, user_id: int, bot: Bot) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


def parse_channel_ids() -> list[str]:
    return [c.strip() for c in CHANNEL_IDS_RAW.split(",") if c.strip()]


def parse_sub_channels() -> list[str]:
    return [c.strip() for c in SUB_CHANNELS_RAW.split(",") if c.strip()]


async def get_admin_channels(bot: Bot) -> list[dict]:
    channels = []
    me = await bot.get_me()
    for cid in parse_channel_ids():
        try:
            chat = await bot.get_chat(cid)
            mem  = await bot.get_chat_member(cid, me.id)
            if mem.status in ("administrator", "creator"):
                channels.append({
                    "id":    chat.id,
                    "title": chat.title or cid,
                    "username": f"@{chat.username}" if chat.username else "",
                })
        except Exception as e:
            logger.warning(f"Kanal xato ({cid}): {e}")
    return channels


async def check_subscriptions(user_id: int, bot: Bot) -> list[str]:
    not_subscribed = []
    for ch in parse_sub_channels():
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked"):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


def is_spam(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    lower = text.lower()
    for kw in SPAM_KEYWORDS:
        if kw in lower:
            return True, f"spam kalit so'z: «{kw}»"
    urls  = re.findall(r'https?://([^\s/]+)', lower)
    plain = re.findall(r'(?<!\w)([\w-]+\.(?:uz|ru|com|net|org|io|me))', lower)
    for domain in urls + plain:
        d = domain.lstrip("www.")
        if any(a in d for a in ALLOWED_DOMAINS):
            continue
        if any(b in d for b in BLOCKED_DOMAINS):
            return True, f"bloklangan domen: «{d}»"
    link_count = len(re.findall(r'https?://', text))
    if link_count > settings["max_links"]:
        return True, f"{link_count} ta link"
    mention_count = len(re.findall(r'@\w+', text))
    if mention_count >= settings["max_mentions"]:
        return True, f"{mention_count} ta @mention"
    if len(text) >= settings["min_caps_length"]:
        letters = [c for c in text if c.isalpha()]
        if letters:
            ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if ratio >= settings["caps_threshold"]:
                return True, f"CAPS LOCK ({int(ratio*100)}%)"
    return False, ""


async def send_spam_log(bot: Bot, chat_title: str, chat_id: int,
                        user, reason: str, text: str, warnings: int):
    """Spam xabarni log kanaliga yuborish"""
    if not LOG_CHANNEL_ID_RAW:
        return
    try:
        log_text = (
            f"🚨 <b>SPAM aniqlandi</b>\n\n"
            f"👤 Foydalanuvchi: {mention(user)} (<code>{user.id}</code>)\n"
            f"💬 Guruh: <b>{chat_title}</b> (<code>{chat_id}</code>)\n"
            f"📌 Sabab: {reason}\n"
            f"🔢 Ogohlantirish: {warnings}/{settings['ban_after_warnings']}\n"
            f"⏰ Vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"📝 Xabar:\n<code>{text[:300]}</code>"
        )
        await bot.send_message(LOG_CHANNEL_ID_RAW, log_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Log yuborishda xato: {e}")

# ═══════════════════════════════════════════════════════════════
# KLAVIATURALAR
# ═══════════════════════════════════════════════════════════════

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Anonim xabar yuborish", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🛡 Moderatsiya",           callback_data="menu_mod"),
         InlineKeyboardButton("📊 Statistika",            callback_data="menu_stats")],
        [InlineKeyboardButton("👤 Adminlar",              callback_data="menu_admins"),
         InlineKeyboardButton("👋 Salom xabar",           callback_data="menu_welcome")],
        [InlineKeyboardButton("⚙️ Spam filtri",           callback_data="menu_filter"),
         InlineKeyboardButton("💬 Guruhlar",              callback_data="menu_groups")],
    ])


def mod_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Ogohlantir",   callback_data="mod_warn"),
         InlineKeyboardButton("✅ Ogohl. olish", callback_data="mod_unwarn")],
        [InlineKeyboardButton("🔇 Mute",         callback_data="mod_mute"),
         InlineKeyboardButton("🔊 Unmute",        callback_data="mod_unmute")],
        [InlineKeyboardButton("🚫 Ban",           callback_data="mod_ban"),
         InlineKeyboardButton("✅ Unban",          callback_data="mod_unban")],
        [InlineKeyboardButton("👢 Chiqarib yuborish", callback_data="mod_kick")],
        [InlineKeyboardButton("🔙 Orqaga",        callback_data="menu_back")],
    ])


def filter_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Kalit so'zlar",    callback_data="filter_keywords")],
        [InlineKeyboardButton("🌐 Domenlar",          callback_data="filter_domains")],
        [InlineKeyboardButton("⚙️ Sozlamalar",        callback_data="filter_settings")],
        [InlineKeyboardButton("🔙 Orqaga",            callback_data="menu_back")],
    ])


def keywords_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Kalit so'z qo'shish",   callback_data="kw_add")],
        [InlineKeyboardButton("📋 Ro'yxatni ko'rish",     callback_data="kw_list")],
        [InlineKeyboardButton("➖ Kalit so'z o'chirish",  callback_data="kw_remove")],
        [InlineKeyboardButton("🔙 Orqaga",                callback_data="menu_filter")],
    ])


def domains_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Bloklangan domenlar", callback_data="dom_blocked")],
        [InlineKeyboardButton("✅ Ruxsat etilgan domenlar", callback_data="dom_allowed")],
        [InlineKeyboardButton("🔙 Orqaga",              callback_data="menu_filter")],
    ])


def back_kb(target: str = "menu_back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Orqaga", callback_data=target)]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")]
    ])


def channel_select_kb(channels: list[dict], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"📢 {ch['title']} {ch['username']}", callback_data=f"{prefix}{ch['id']}")]
        for ch in channels
    ]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def broadcast_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Matnni tahrirlash", callback_data="bc_edit")],
        [InlineKeyboardButton("▶️ Kanalga yuborish",  callback_data="bc_choose_channel")],
        [InlineKeyboardButton("❌ Bekor qilish",       callback_data="cancel")],
    ])

# ═══════════════════════════════════════════════════════════════
# /start — Asosiy menyu
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.effective_message
    if not user or not msg:
        return

    not_sub = await check_subscriptions(user.id, context.bot)
    if not_sub:
        buttons = [[InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
                   for ch in not_sub]
        buttons.append([InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_sub")])
        await msg.reply_text(
            "⚠️ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:</b>",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        return

    if not is_bot_admin(user.id):
        await msg.reply_text(
            "👋 Salom! Bu bot guruh uchun spam filtri vazifasini bajaradi.\n"
            "Siz foydalanuvchi sifatida guruhda spam'dan himoyalanasiz.",
        )
        return

    await msg.reply_text(
        f"👋 Xush kelibsiz, <b>{user.full_name}</b>!\n\n"
        f"🤖 <b>Guruh Boshqaruv Paneli v2</b>\n"
        f"Quyidagi tugmalardan birini tanlang:",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )

# ═══════════════════════════════════════════════════════════════
# REPLY ORQALI GURUH MODERATSIYASI
# ═══════════════════════════════════════════════════════════════

async def cmd_mod_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guruhda xabarga reply qilib /warn /mute /ban /kick"""
    msg  = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return
    if chat.type == "private":
        return
    if not await get_tg_admin(chat.id, user.id, context.bot) and not is_bot_admin(user.id):
        return
    if not msg.reply_to_message:
        await msg.reply_text("⚠️ Bu buyruqni foydalanuvchi xabariga <b>reply</b> qilib yuboring.", parse_mode=ParseMode.HTML)
        return

    target = msg.reply_to_message.from_user
    if not target:
        return
    if target.is_bot:
        await msg.reply_text("❌ Botga bu amalni bajarish mumkin emas.")
        return

    command = msg.text.split()[0].lower().lstrip("/").split("@")[0]

    action_map = {
        "warn":   "mod_warn",
        "unwarn": "mod_unwarn",
        "mute":   "mod_mute",
        "unmute": "mod_unmute",
        "ban":    "mod_ban",
        "unban":  "mod_unban",
        "kick":   "mod_kick",
    }

    action = action_map.get(command)
    if not action:
        return

    result = await do_mod_action(action, chat.id, target.id, context.bot)

    try:
        await msg.delete()
    except Exception:
        pass

    reply = await chat.send_message(result, parse_mode=ParseMode.HTML)
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat.id, reply.message_id), when=20
    )

# ═══════════════════════════════════════════════════════════════
# MAJBURIY OBUNA TEKSHIRISH
# ═══════════════════════════════════════════════════════════════

async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    if not query or not user:
        return
    await query.answer()
    not_sub = await check_subscriptions(user.id, context.bot)
    if not_sub:
        buttons = [[InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
                   for ch in not_sub]
        buttons.append([InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_sub")])
        await query.edit_message_text(
            "❌ Hali ham obuna bo'lmagan kanallar bor:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await query.edit_message_text("✅ Rahmat! Endi /start bering.")

# ═══════════════════════════════════════════════════════════════
# MENYU CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: C901
    query = update.callback_query
    user  = update.effective_user
    if not query or not user:
        return
    await query.answer()
    data = query.data

    if not is_bot_admin(user.id):
        await query.answer("⛔ Siz admin emassiz!", show_alert=True)
        return

    # ── Bosh menyu ──
    if data == "menu_back":
        await query.edit_message_text(
            "🤖 <b>Boshqaruv Paneli</b>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Guruhlar ro'yxati ──
    elif data == "menu_groups":
        if not known_groups:
            text = "💬 <b>Guruhlar</b>\n\nHozircha hech qanday guruh yo'q.\nBot guruhga qo'shilganda avtomatik ro'yxatga kiradi."
        else:
            lines = []
            for cid, title in known_groups.items():
                spam_cnt = group_stats.get(cid, {}).get("spam", 0)
                lines.append(f"  • <b>{title}</b>\n    ID: <code>{cid}</code> | Spam: {spam_cnt}")
            text = "💬 <b>Guruhlar ro'yxati</b>\n\n" + "\n\n".join(lines)
        await query.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)

    # ── Moderatsiya ──
    elif data == "menu_mod":
        await query.edit_message_text(
            "🛡 <b>Moderatsiya</b>\n\n"
            "Tugma orqali yoki guruhda xabarga <b>reply</b> qilib:\n"
            "<code>/warn</code> <code>/mute</code> <code>/ban</code> <code>/kick</code>",
            reply_markup=mod_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data in ("mod_warn", "mod_unwarn", "mod_mute", "mod_unmute", "mod_ban", "mod_unban", "mod_kick"):
        action_names = {
            "mod_warn":   "⚠️ Ogohlantirish",
            "mod_unwarn": "✅ Ogohlantirishni olish",
            "mod_mute":   "🔇 Mute qilish",
            "mod_unmute": "🔊 Mute ochish",
            "mod_ban":    "🚫 Ban qilish",
            "mod_unban":  "✅ Bandan chiqarish",
            "mod_kick":   "👢 Chiqarib yuborish",
        }
        mod_sessions[user.id] = {"action": data, "chat_id": None}

        # Guruhlar ro'yxatini ko'rsatish
        if known_groups:
            buttons = [
                [InlineKeyboardButton(f"💬 {title}", callback_data=f"mod_select_group:{cid}:{data}")]
                for cid, title in known_groups.items()
            ]
            buttons.append([InlineKeyboardButton("✏️ ID qo'lda kiritish", callback_data=f"mod_manual_group:{data}")])
            buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")])
            await query.edit_message_text(
                f"{action_names[data]}\n\nQaysi guruhda?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"{action_names[data]}\n\n"
                f"Guruh ID sini yuboring:",
                reply_markup=cancel_kb(),
                parse_mode=ParseMode.HTML,
            )

    elif data.startswith("mod_select_group:"):
        _, cid_str, action = data.split(":", 2)
        cid = int(cid_str)
        mod_sessions[user.id] = {"action": action, "chat_id": cid}
        await query.edit_message_text(
            f"✅ Guruh: <b>{known_groups.get(cid, cid)}</b>\n\n"
            f"Foydalanuvchi <b>@username</b> yoki <b>user ID</b> ni yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("mod_manual_group:"):
        action = data.split(":", 1)[1]
        mod_sessions[user.id] = {"action": action, "chat_id": None}
        await query.edit_message_text(
            "Guruh ID sini yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Statistika ──
    elif data == "menu_stats":
        total_spam = sum(v.get("spam", 0) for v in group_stats.values())
        lines = []
        for cid, info in group_stats.items():
            title = info.get("title", str(cid))
            cnt   = info.get("spam", 0)
            lines.append(f"  • <b>{title}</b>: {cnt} ta spam")
        body = "\n".join(lines) if lines else "  Hozircha yo'q"
        warned = sum(1 for w in user_warnings.values() if w > 0)
        await query.edit_message_text(
            f"📊 <b>Spam Statistikasi</b>\n\n"
            f"Jami spam: <b>{total_spam}</b>\n"
            f"Ogohlantirish olgan foydalanuvchilar: <b>{warned}</b>\n\n"
            f"{body}",
            reply_markup=back_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Adminlar ──
    elif data == "menu_admins":
        admins_list = "\n".join(f"  • <code>{a}</code>" for a in bot_admins) or "  Hozircha yo'q"
        await query.edit_message_text(
            f"👤 <b>Bot Adminlari</b>\n\n"
            f"Super admin: <code>{SUPER_ADMIN_ID}</code>\n\n"
            f"Qo'shimcha adminlar:\n{admins_list}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_add"),
                 InlineKeyboardButton("➖ Admin o'chirish", callback_data="admin_remove")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")],
            ]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_add":
        mod_sessions[user.id] = {"action": "admin_add"}
        await query.edit_message_text(
            "➕ <b>Admin qo'shish</b>\n\nYangi admin <b>user ID</b> sini yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_remove":
        if not bot_admins:
            await query.edit_message_text("Adminlar ro'yxati bo'sh.", reply_markup=back_kb())
            return
        buttons = [
            [InlineKeyboardButton(f"❌ {aid}", callback_data=f"rm_admin:{aid}")]
            for aid in bot_admins
        ]
        buttons.append([InlineKeyboardButton("🔙 Orqaga", callback_data="menu_admins")])
        await query.edit_message_text(
            "➖ <b>Adminni o'chirish</b>:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("rm_admin:"):
        aid = int(data.split(":")[1])
        bot_admins.discard(aid)
        await query.edit_message_text(
            f"✅ Admin <code>{aid}</code> o'chirildi.",
            reply_markup=back_kb("menu_admins"),
            parse_mode=ParseMode.HTML,
        )

    # ── Salom xabar ──
    elif data == "menu_welcome":
        # Guruhlar ro'yxatidan tanlash
        if known_groups:
            buttons = [
                [InlineKeyboardButton(f"💬 {title}", callback_data=f"welcome_group:{cid}")]
                for cid, title in known_groups.items()
            ]
            buttons.append([InlineKeyboardButton("✏️ ID qo'lda kiritish", callback_data="welcome_manual")])
            buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")])
            await query.edit_message_text(
                "👋 <b>Salom xabar</b>\n\nQaysi guruhga o'rnatmoqchisiz?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        else:
            mod_sessions[user.id] = {"action": "set_welcome_chatid"}
            await query.edit_message_text(
                "👋 <b>Salom xabar</b>\n\nGuruh ID sini yuboring:",
                reply_markup=cancel_kb(),
                parse_mode=ParseMode.HTML,
            )

    elif data.startswith("welcome_group:"):
        cid = int(data.split(":")[1])
        mod_sessions[user.id] = {"action": "set_welcome_text", "chat_id": cid}
        await query.edit_message_text(
            f"✅ Guruh: <b>{known_groups.get(cid, cid)}</b>\n\n"
            "Salom xabarini yuboring.\n"
            "<code>{name}</code> — a'zo ismi o'rniga qo'yiladi.",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "welcome_manual":
        mod_sessions[user.id] = {"action": "set_welcome_chatid"}
        await query.edit_message_text(
            "Guruh ID sini yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Spam filtri menyusi ──
    elif data == "menu_filter":
        await query.edit_message_text(
            f"⚙️ <b>Spam Filtri</b>\n\n"
            f"🔢 Ban chegarasi: <b>{settings['ban_after_warnings']}</b>\n"
            f"⏱ Mute vaqti: <b>{settings['mute_duration_minutes']}</b> daqiqa\n"
            f"🔗 Max link: <b>{settings['max_links']}</b>\n"
            f"👤 Max mention: <b>{settings['max_mentions']}</b>",
            reply_markup=filter_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Kalit so'zlar ──
    elif data == "filter_keywords":
        await query.edit_message_text(
            f"🔤 <b>Kalit so'zlar</b>\n\nJami: {len(SPAM_KEYWORDS)} ta",
            reply_markup=keywords_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "kw_add":
        mod_sessions[user.id] = {"action": "kw_add"}
        await query.edit_message_text(
            "➕ Yangi kalit so'z yuboring (kichik harf, o'zbekcha yoki inglizcha):",
            reply_markup=cancel_kb(),
        )

    elif data == "kw_list":
        chunks = [SPAM_KEYWORDS[i:i+15] for i in range(0, len(SPAM_KEYWORDS), 15)]
        text = "📋 <b>Kalit so'zlar ro'yxati:</b>\n\n"
        for i, kw in enumerate(SPAM_KEYWORDS, 1):
            text += f"  {i}. <code>{kw}</code>\n"
        if len(text) > 3500:
            text = text[:3500] + "\n..."
        await query.edit_message_text(text, reply_markup=back_kb("filter_keywords"), parse_mode=ParseMode.HTML)

    elif data == "kw_remove":
        if not SPAM_KEYWORDS:
            await query.edit_message_text("Ro'yxat bo'sh.", reply_markup=back_kb("filter_keywords"))
            return
        buttons = []
        for i, kw in enumerate(SPAM_KEYWORDS):
            buttons.append([InlineKeyboardButton(f"❌ {kw[:40]}", callback_data=f"kw_del:{i}")])
        buttons.append([InlineKeyboardButton("🔙 Orqaga", callback_data="filter_keywords")])
        await query.edit_message_text(
            "O'chirish uchun kalit so'zni tanlang:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("kw_del:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(SPAM_KEYWORDS):
            removed = SPAM_KEYWORDS.pop(idx)
            await query.edit_message_text(
                f"✅ «{removed}» o'chirildi.",
                reply_markup=back_kb("filter_keywords"),
            )

    # ── Domenlar ──
    elif data == "filter_domains":
        await query.edit_message_text(
            f"🌐 <b>Domenlar</b>\n\n"
            f"Bloklangan: {len(BLOCKED_DOMAINS)} ta\n"
            f"Ruxsat etilgan: {len(ALLOWED_DOMAINS)} ta",
            reply_markup=domains_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "dom_blocked":
        text = "🚫 <b>Bloklangan domenlar:</b>\n\n"
        text += "\n".join(f"  • <code>{d}</code>" for d in BLOCKED_DOMAINS) or "  Bo'sh"
        buttons = [
            [InlineKeyboardButton("➕ Qo'shish", callback_data="dom_block_add"),
             InlineKeyboardButton("➖ O'chirish", callback_data="dom_block_remove")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="filter_domains")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

    elif data == "dom_allowed":
        text = "✅ <b>Ruxsat etilgan domenlar:</b>\n\n"
        text += "\n".join(f"  • <code>{d}</code>" for d in ALLOWED_DOMAINS) or "  Bo'sh"
        buttons = [
            [InlineKeyboardButton("➕ Qo'shish", callback_data="dom_allow_add"),
             InlineKeyboardButton("➖ O'chirish", callback_data="dom_allow_remove")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="filter_domains")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

    elif data in ("dom_block_add", "dom_allow_add", "dom_block_remove", "dom_allow_remove"):
        mod_sessions[user.id] = {"action": data}
        prompts = {
            "dom_block_add":    "Bloklash uchun domen kiriting (misol: spam.uz):",
            "dom_allow_add":    "Ruxsat berish uchun domen kiriting (misol: google.com):",
            "dom_block_remove": "O'chirish uchun bloklangan domen kiriting:",
            "dom_allow_remove": "O'chirish uchun ruxsat etilgan domen kiriting:",
        }
        await query.edit_message_text(prompts[data], reply_markup=cancel_kb())

    # ── Filtri sozlamalari ──
    elif data == "filter_settings":
        mod_sessions[user.id] = {"action": "filter_settings_choose"}
        await query.edit_message_text(
            f"⚙️ <b>Sozlamalar</b>\n\n"
            f"O'zgartirmoqchi bo'lgan sozlamani tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔢 Ban chegarasi ({settings['ban_after_warnings']})", callback_data="fs_ban")],
                [InlineKeyboardButton(f"⏱ Mute vaqti ({settings['mute_duration_minutes']} daq.)", callback_data="fs_mute")],
                [InlineKeyboardButton(f"🔗 Max link ({settings['max_links']})", callback_data="fs_links")],
                [InlineKeyboardButton(f"👤 Max mention ({settings['max_mentions']})", callback_data="fs_mentions")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_filter")],
            ]),
            parse_mode=ParseMode.HTML,
        )

    elif data in ("fs_ban", "fs_mute", "fs_links", "fs_mentions"):
        labels = {
            "fs_ban":      ("ban_after_warnings",    "Ban chegarasi (ogohlantirish soni)"),
            "fs_mute":     ("mute_duration_minutes", "Mute vaqti (daqiqada, 0 = ban)"),
            "fs_links":    ("max_links",             "Maksimal link soni"),
            "fs_mentions": ("max_mentions",          "Maksimal @mention soni"),
        }
        key, label = labels[data]
        mod_sessions[user.id] = {"action": "fs_update", "key": key}
        await query.edit_message_text(
            f"✏️ <b>{label}</b>\n\nHozirgi qiymat: <code>{settings[key]}</code>\n\nYangi qiymat (raqam) yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Broadcast ──
    elif data == "menu_broadcast":
        bc_sessions[user.id] = {"step": "waiting_message"}
        await query.edit_message_text(
            "📢 <b>Anonim Xabar Yuborish</b>\n\n"
            "Xabarni <b>forward qiling</b> yoki to'g'ridan yuboring.",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "bc_edit":
        session = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q.", reply_markup=back_kb())
            return
        session["step"] = "editing_text"
        await query.edit_message_text(
            "✏️ Yangi matnni yuboring:",
            reply_markup=cancel_kb(),
        )

    elif data == "bc_choose_channel":
        session = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q.", reply_markup=back_kb())
            return
        channels = await get_admin_channels(context.bot)
        if not channels:
            await query.edit_message_text(
                "❌ Bot hech qanday kanalda admin emas.\n.env da CHANNEL_IDS ni to'ldiring.",
                reply_markup=back_kb(),
            )
            return
        session["step"] = "choose_channel"
        await query.edit_message_text(
            "📢 Qaysi kanalga?",
            reply_markup=channel_select_kb(channels, "bc_send:"),
        )

    elif data.startswith("bc_send:"):
        target_id = int(data.split(":")[1])
        session   = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q.", reply_markup=back_kb())
            return
        try:
            edited_text = session.get("edited_text")
            if edited_text:
                if session.get("has_media"):
                    await context.bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=session["from_chat_id"],
                        message_id=session["message_id"],
                        caption=edited_text,
                    )
                else:
                    await context.bot.send_message(target_id, edited_text, parse_mode=ParseMode.HTML)
            else:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=session["from_chat_id"],
                    message_id=session["message_id"],
                )
            channels  = await get_admin_channels(context.bot)
            ch_title  = next((c["title"] for c in channels if c["id"] == target_id), str(target_id))
            bc_sessions.pop(user.id, None)
            await query.edit_message_text(
                f"✅ Xabar <b>{ch_title}</b> kanaliga yuborildi (anonim)!",
                reply_markup=back_kb(),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Xatolik: {e}", reply_markup=back_kb())

    # ── Bekor qilish ──
    elif data == "cancel":
        bc_sessions.pop(user.id, None)
        mod_sessions.pop(user.id, None)
        await query.edit_message_text(
            "❌ Bekor qilindi.\n\n🤖 <b>Boshqaruv Paneli</b>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

# ═══════════════════════════════════════════════════════════════
# SHAXSIY CHAT — INPUT HANDLER
# ═══════════════════════════════════════════════════════════════

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: C901
    user = update.effective_user
    msg  = update.effective_message
    if not user or not msg:
        return

    # ── Moderatsiya sessiyasi ──
    if user.id in mod_sessions:
        session = mod_sessions[user.id]
        action  = session.get("action", "")
        text    = (msg.text or "").strip()

        # Admin qo'shish
        if action == "admin_add":
            try:
                new_id = int(text)
                bot_admins.add(new_id)
                mod_sessions.pop(user.id)
                await msg.reply_text(
                    f"✅ <code>{new_id}</code> admin qilib qo'shildi!",
                    reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await msg.reply_text("❌ Noto'g'ri ID.", reply_markup=cancel_kb())
            return

        # Salom xabar — guruh ID
        if action == "set_welcome_chatid":
            try:
                cid = int(text)
                session["chat_id"] = cid
                session["action"]  = "set_welcome_text"
                await msg.reply_text(
                    "✅ Guruh ID qabul qilindi.\n\nSalom xabarini yuboring.\n"
                    "<code>{name}</code> — a'zo ismi o'rniga qo'yiladi.",
                    reply_markup=cancel_kb(), parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await msg.reply_text("❌ Noto'g'ri ID.", reply_markup=cancel_kb())
            return

        if action == "set_welcome_text":
            cid = session.get("chat_id")
            welcome_msgs[cid] = text
            mod_sessions.pop(user.id)
            await msg.reply_text(
                f"✅ Salom xabar o'rnatildi:\n\n{text}",
                reply_markup=main_menu_kb(),
            )
            return

        # Kalit so'z qo'shish
        if action == "kw_add":
            kw = text.lower()
            if kw in SPAM_KEYWORDS:
                await msg.reply_text("⚠️ Bu kalit so'z allaqachon bor.", reply_markup=cancel_kb())
            else:
                SPAM_KEYWORDS.append(kw)
                mod_sessions.pop(user.id)
                await msg.reply_text(
                    f"✅ «{kw}» kalit so'z qo'shildi.\nJami: {len(SPAM_KEYWORDS)} ta",
                    reply_markup=main_menu_kb(),
                )
            return

        # Domen boshqaruvi
        if action in ("dom_block_add", "dom_allow_add", "dom_block_remove", "dom_allow_remove"):
            domain = text.lower().lstrip("www.").lstrip("https://").lstrip("http://").split("/")[0]
            if action == "dom_block_add":
                if domain not in BLOCKED_DOMAINS:
                    BLOCKED_DOMAINS.append(domain)
                msg_text = f"✅ «{domain}» bloklangan domenlar ro'yxatiga qo'shildi."
            elif action == "dom_allow_add":
                if domain not in ALLOWED_DOMAINS:
                    ALLOWED_DOMAINS.append(domain)
                msg_text = f"✅ «{domain}» ruxsat etilgan domenlar ro'yxatiga qo'shildi."
            elif action == "dom_block_remove":
                BLOCKED_DOMAINS.discard(domain) if hasattr(BLOCKED_DOMAINS, 'discard') else None
                if domain in BLOCKED_DOMAINS:
                    BLOCKED_DOMAINS.remove(domain)
                msg_text = f"✅ «{domain}» bloklangan ro'yxatdan o'chirildi."
            elif action == "dom_allow_remove":
                if domain in ALLOWED_DOMAINS:
                    ALLOWED_DOMAINS.remove(domain)
                msg_text = f"✅ «{domain}» ruxsat ro'yxatidan o'chirildi."
            else:
                msg_text = "✅ Bajarildi."
            mod_sessions.pop(user.id)
            await msg.reply_text(msg_text, reply_markup=main_menu_kb())
            return

        # Filtri sozlamalari
        if action == "fs_update":
            key = session.get("key")
            try:
                val = float(text) if "." in text else int(text)
                settings[key] = val
                mod_sessions.pop(user.id)
                await msg.reply_text(
                    f"✅ Sozlama yangilandi: <code>{key}</code> = <b>{val}</b>",
                    reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await msg.reply_text("❌ Faqat raqam kiriting.", reply_markup=cancel_kb())
            return

        # Moderatsiya — guruh ID
        if action in ("mod_warn", "mod_unwarn", "mod_mute", "mod_unmute", "mod_ban", "mod_unban", "mod_kick"):
            if session.get("chat_id") is None:
                try:
                    cid = int(text)
                    session["chat_id"] = cid
                    await msg.reply_text(
                        "✅ Guruh ID qabul qilindi.\n\nFoydalanuvchi @username yoki ID:",
                        reply_markup=cancel_kb(), parse_mode=ParseMode.HTML,
                    )
                except ValueError:
                    await msg.reply_text("❌ Noto'g'ri ID.", reply_markup=cancel_kb())
                return
            else:
                target_input = text
                chat_id = session["chat_id"]
                try:
                    if target_input.startswith("@"):
                        target_user = await context.bot.get_chat(target_input)
                        target_id   = target_user.id
                    else:
                        target_id = int(target_input)
                    result_text = await do_mod_action(action, chat_id, target_id, context.bot)
                    mod_sessions.pop(user.id)
                    await msg.reply_text(result_text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
                except Exception as e:
                    await msg.reply_text(f"❌ Xato: {e}", reply_markup=cancel_kb())
                return

    # ── Broadcast sessiyasi ──
    if user.id in bc_sessions:
        session = bc_sessions[user.id]
        step    = session.get("step")

        if step == "waiting_message":
            has_media = bool(msg.photo or msg.video or msg.document or msg.audio)
            session.update({
                "from_chat_id": msg.chat_id,
                "message_id":   msg.message_id,
                "has_media":    has_media,
                "step":         "preview",
            })
            await msg.reply_text(
                "✅ Xabar qabul qilindi! Nima qilmoqchisiz?",
                reply_markup=broadcast_edit_kb(),
            )
            return

        if step == "editing_text":
            session["edited_text"] = msg.text or msg.caption or ""
            session["step"] = "preview"
            await msg.reply_text(
                "✅ Matn yangilandi! Kanalga yuborish uchun:",
                reply_markup=broadcast_edit_kb(),
            )
            return

    if is_bot_admin(user.id):
        await msg.reply_text(
            "🤖 <b>Boshqaruv Paneli</b>",
            reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML,
        )

# ═══════════════════════════════════════════════════════════════
# MODERATSIYA AMALLARI
# ═══════════════════════════════════════════════════════════════

async def do_mod_action(action: str, chat_id: int, target_id: int, bot: Bot) -> str:
    if action == "mod_warn":
        user_warnings[target_id] = user_warnings.get(target_id, 0) + 1
        w = user_warnings[target_id]
        return f"⚠️ <code>{target_id}</code> ogohlantirish: {w}/{settings['ban_after_warnings']}"

    if action == "mod_unwarn":
        user_warnings[target_id] = max(0, user_warnings.get(target_id, 0) - 1)
        return f"✅ <code>{target_id}</code> ogohlantirish: {user_warnings[target_id]}/{settings['ban_after_warnings']}"

    if action == "mod_mute":
        until = datetime.now() + timedelta(minutes=settings["mute_duration_minutes"])
        await bot.restrict_chat_member(
            chat_id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return f"🔇 <code>{target_id}</code> {settings['mute_duration_minutes']} daqiqa mute."

    if action == "mod_unmute":
        await bot.restrict_chat_member(
            chat_id, target_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        return f"🔊 <code>{target_id}</code> mute'dan chiqarildi."

    if action == "mod_ban":
        await bot.ban_chat_member(chat_id, target_id)
        return f"🚫 <code>{target_id}</code> ban qilindi."

    if action == "mod_unban":
        await bot.unban_chat_member(chat_id, target_id)
        return f"✅ <code>{target_id}</code> ban'dan chiqarildi."

    if action == "mod_kick":
        await bot.ban_chat_member(chat_id, target_id)
        await bot.unban_chat_member(chat_id, target_id)
        return f"👢 <code>{target_id}</code> guruhdan chiqarib yuborildi."

    return "❓ Noma'lum amal."

# ═══════════════════════════════════════════════════════════════
# GURUH — SPAM HANDLER
# ═══════════════════════════════════════════════════════════════

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user    = update.effective_user
    chat    = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type == "private":
        return
    if is_bot_admin(user.id):
        return
    if await get_tg_admin(chat.id, user.id, context.bot):
        return

    # Guruhni ro'yxatga olish
    if chat.id not in known_groups:
        known_groups[chat.id] = chat.title or str(chat.id)

    text = message.text or message.caption or ""
    spam, reason = is_spam(text)
    if not spam:
        return

    user_warnings[user.id] = user_warnings.get(user.id, 0) + 1
    if chat.id not in group_stats:
        group_stats[chat.id] = {"title": chat.title or str(chat.id), "spam": 0}
    group_stats[chat.id]["spam"] += 1
    warnings = user_warnings[user.id]
    logger.info(f"SPAM | {user.id} | {reason} | warn:{warnings}")

    # Log kanaliga yuborish
    await send_spam_log(context.bot, chat.title or str(chat.id), chat.id, user, reason, text, warnings)

    try:
        await message.delete()
    except Exception:
        pass

    if warnings >= settings["ban_after_warnings"]:
        try:
            mute_min = settings["mute_duration_minutes"]
            if mute_min > 0:
                until = datetime.now() + timedelta(minutes=mute_min)
                await context.bot.restrict_chat_member(
                    chat.id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                txt = f"⛔ {mention(user)} {mute_min} daqiqa <b>MUTE</b> qilindi (spam)."
            else:
                await context.bot.ban_chat_member(chat.id, user.id)
                txt = f"🚫 {mention(user)} <b>BAN</b> qilindi (spam)."
            user_warnings[user.id] = 0
            await context.bot.send_message(chat.id, txt, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Ban/mute xato: {e}")
    else:
        remaining = settings["ban_after_warnings"] - warnings
        try:
            w = await context.bot.send_message(
                chat.id,
                f"⚠️ {mention(user)}, spam xabar o'chirildi!\n"
                f"📌 Sabab: {reason}\n"
                f"🔢 Ogohlantirish: {warnings}/{settings['ban_after_warnings']} "
                f"(yana {remaining} ta qoldi)",
                parse_mode=ParseMode.HTML,
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat.id, w.message_id), when=15
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════
# GURUH — BOT QO'SHILGANDA
# ═══════════════════════════════════════════════════════════════

async def handle_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    me = await context.bot.get_me()
    for member in (msg.new_chat_members or []):
        if member.id == me.id:
            known_groups[chat.id] = chat.title or str(chat.id)
            logger.info(f"Bot yangi guruhga qo'shildi: {chat.title} ({chat.id})")

# ═══════════════════════════════════════════════════════════════
# GURUH — YANGI A'ZO
# ═══════════════════════════════════════════════════════════════

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    me = await context.bot.get_me()
    for member in (msg.new_chat_members or []):
        if member.id == me.id:
            known_groups[chat.id] = chat.title or str(chat.id)
            continue

        not_sub = await check_subscriptions(member.id, context.bot)
        if not_sub:
            buttons = [[InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
                       for ch in not_sub]
            buttons.append([InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_sub")])
            await context.bot.send_message(
                chat.id,
                f"👋 Salom {mention(member)}!\n\n"
                f"⚠️ Guruhda yozish uchun quyidagi kanallarga obuna bo'ling:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        elif chat.id in welcome_msgs:
            text = welcome_msgs[chat.id].replace(
                "{name}", f'<a href="tg://user?id={member.id}">{member.full_name}</a>'
            )
            await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        print("Xato: .env faylda BOT_TOKEN ni to'ldiring!")
        return
    if not SUPER_ADMIN_ID:
        print("Xato: .env faylda SUPER_ADMIN_ID ni to'ldiring!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Guruhda reply orqali moderatsiya
    for cmd in ["warn", "unwarn", "mute", "unmute", "ban", "unban", "kick"]:
        app.add_handler(CommandHandler(cmd, cmd_mod_reply, filters=~filters.ChatType.PRIVATE))

    # Majburiy obuna tekshirish
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub$"))

    # Barcha inline tugmalar
    app.add_handler(CallbackQueryHandler(menu_callback))

    # Shaxsiy chat
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private,
    ))

    # Guruh — spam filtri
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.ChatType.PRIVATE,
        handle_group_message,
    ))

    # Guruh — yangi a'zo (bot qo'shilishi ham shu orqali)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member,
    ))

    print("=" * 60)
    print("  Spam Filter Bot v2 ishga tushdi!")
    print(f"  Super admin ID   : {SUPER_ADMIN_ID}")
    print(f"  Ban chegarasi    : {settings['ban_after_warnings']} ogohlantirish")
    print(f"  Mute vaqti       : {settings['mute_duration_minutes']} daqiqa")
    print(f"  Log kanali       : {LOG_CHANNEL_ID_RAW or 'Yoqilmagan'}")
    print(f"  Broadcast kanal  : {CHANNEL_IDS_RAW or 'Yoqilmagan'}")
    print(f"  Obuna kanallar   : {SUB_CHANNELS_RAW or 'Yoqilmagan'}")
    print(f"  Reply mod buyruq : /warn /mute /ban /kick /unban /unmute")
    print("=" * 60)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
