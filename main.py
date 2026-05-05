"""
Telegram Guruh Spam Filter Bot — To'liq versiya
=================================================
Xususiyatlar:
  ✅ Spam filtri (auto o'chirish, mute, ban)
  ✅ Barcha boshqaruv TUGMALAR orqali (/ buyruq emas)
  ✅ Admin qo'shish / o'chirish paneli
  ✅ Majburiy obuna (subscribe) tekshirish
  ✅ Anonim xabar: boshqa kanaldan forward → tahrirlash → kanal tanlash → yuborish
  ✅ Yangi a'zo uchun salom xabar
  ✅ Spam statistikasi

Ishlatish:
  1. pip install -r requirements.txt
  2. .env faylni to'ldiring
  3. Botni guruh va kanallarga ADMIN qiling
  4. python spam_filter_bot.py
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
BAN_AFTER_WARNINGS    = int(os.getenv("BAN_AFTER_WARNINGS", 3))
MUTE_DURATION_MINUTES = int(os.getenv("MUTE_DURATION_MINUTES", 60))
SUPER_ADMIN_ID        = int(os.getenv("SUPER_ADMIN_ID", 0))   # Asosiy admin (siz)

# Bot admin bo'lgan kanallar IDs (vergul bilan): -100xxx,-100yyy
CHANNEL_IDS_RAW = os.getenv("CHANNEL_IDS", "")
# Majburiy obuna kanallar (vergul bilan): @mychannel,@other
SUB_CHANNELS_RAW = os.getenv("SUB_CHANNELS", "")

# ═══════════════════════════════════════════════════════════════
# SPAM FILTRI
# ═══════════════════════════════════════════════════════════════
BLOCKED_DOMAINS = ["alijahon.uz","bit.ly","tinyurl.com","cutt.ly","is.gd","shorturl.at"]
ALLOWED_DOMAINS: list[str] = []

SPAM_KEYWORDS = [
    "profilimda","profilida","mening kanalim","mening profilim",
    "profilga kiring","profilimga kiring","profile da",
    "buyurtma bering","buyurtma berish","narxi:","narx :",
    "chegirma","skidka","discount","aksiya","promo kodi",
    "sotib oling","xarid qiling",
    "zarabot","earn money","passive income",
    "crypto signal","forex signal","invest now",
    "subscribe","follow me","click here","click link",
    "besplatno","tekin","free gift",
    "bot orqali","botga yozing","referral","referal",
    "100% kafolat","guarantee",
]
CAPS_LOCK_THRESHOLD = 0.70
MIN_CAPS_LENGTH     = 20
MAX_LINKS_ALLOWED   = 2
MAX_MENTIONS        = 3

# ═══════════════════════════════════════════════════════════════
# XOTIRA (RAM)
# ═══════════════════════════════════════════════════════════════
user_warnings:      dict[int, int]  = {}   # {user_id: count}
group_stats:        dict[int, int]  = {}   # {chat_id: spam_count}
welcome_msgs:       dict[int, str]  = {}   # {chat_id: text}
bot_admins:         set[int]        = set()  # qo'shimcha adminlar
# Broadcast sessiya: {user_id: {step, from_chat_id, message_id, edited_text, ...}}
bc_sessions:        dict[int, dict] = {}
# Moderatsiya sessiyasi (reply talab qilmaslik uchun): {user_id: {action, chat_id}}
mod_sessions:       dict[int, dict] = {}

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


async def is_admin_anywhere(chat_id: int, user_id: int, bot: Bot) -> bool:
    return is_bot_admin(user_id) or await get_tg_admin(chat_id, user_id, bot)


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
    """Foydalanuvchi obuna bo'lmagan kanallar ro'yxatini qaytaradi"""
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
        if any(a in d for a in ALLOWED_DOMAINS): continue
        if any(b in d for b in BLOCKED_DOMAINS):
            return True, f"bloklangan domen: «{d}»"
    if len(re.findall(r'https?://', text)) > MAX_LINKS_ALLOWED:
        return True, f"{len(re.findall(r'https?://', text))} ta link"
    if len(re.findall(r'@\w+', text)) >= MAX_MENTIONS:
        return True, f"{len(re.findall(r'@\w+', text))} ta @mention"
    if len(text) >= MIN_CAPS_LENGTH:
        letters = [c for c in text if c.isalpha()]
        if letters:
            ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if ratio >= CAPS_LOCK_THRESHOLD:
                return True, f"CAPS LOCK ({int(ratio*100)}%)"
    return False, ""

# ═══════════════════════════════════════════════════════════════
# KLAVIATURALAR (Tugmalar)
# ═══════════════════════════════════════════════════════════════

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Anonim xabar yuborish", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🛡 Moderatsiya",           callback_data="menu_mod"),
         InlineKeyboardButton("📊 Statistika",            callback_data="menu_stats")],
        [InlineKeyboardButton("👤 Adminlar",              callback_data="menu_admins"),
         InlineKeyboardButton("👋 Salom xabar",           callback_data="menu_welcome")],
        [InlineKeyboardButton("⚙️ Spam filtri",           callback_data="menu_filter")],
    ])


def mod_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Ogohlantir",   callback_data="mod_warn"),
         InlineKeyboardButton("✅ Ogohl. olish", callback_data="mod_unwarn")],
        [InlineKeyboardButton("🔇 Mute",         callback_data="mod_mute"),
         InlineKeyboardButton("🔊 Unmute",        callback_data="mod_unmute")],
        [InlineKeyboardButton("🚫 Ban",           callback_data="mod_ban"),
         InlineKeyboardButton("✅ Unban",          callback_data="mod_unban")],
        [InlineKeyboardButton("🔙 Orqaga",        callback_data="menu_back")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]
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

    # Majburiy obuna tekshirish
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
        f"🤖 <b>Guruh Boshqaruv Paneli</b>\n"
        f"Quyidagi tugmalardan birini tanlang:",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )

# ═══════════════════════════════════════════════════════════════
# MAJBURIY OBUNA TEKSHIRISH (tugma bosilganda)
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

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    if not query or not user:
        return
    await query.answer()
    data = query.data

    # Har qanday tugmadan oldin admin tekshirish
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

    # ── Moderatsiya menyusi ──
    elif data == "menu_mod":
        await query.edit_message_text(
            "🛡 <b>Moderatsiya</b>\n\n"
            "Kerakli amalni tanlang. Bot sizdan @username yoki user ID so'raydi:",
            reply_markup=mod_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

    elif data in ("mod_warn","mod_unwarn","mod_mute","mod_unmute","mod_ban","mod_unban"):
        action_names = {
            "mod_warn":   "⚠️ Ogohlantirish",
            "mod_unwarn": "✅ Ogohlantirishni olish",
            "mod_mute":   "🔇 Mute qilish",
            "mod_unmute": "🔊 Mute ochish",
            "mod_ban":    "🚫 Ban qilish",
            "mod_unban":  "✅ Bandan chiqarish",
        }
        mod_sessions[user.id] = {"action": data, "chat_id": None}
        await query.edit_message_text(
            f"{action_names[data]}\n\n"
            f"Guruh/kanal ID sini yuboring (misol: <code>-1001234567890</code>)\n"
            f"Keyin foydalanuvchi @username yoki ID so'raladi.",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Statistika ──
    elif data == "menu_stats":
        total_spam = sum(group_stats.values())
        lines = "\n".join(f"  • Chat {cid}: <b>{cnt}</b> ta" for cid, cnt in group_stats.items()) or "  Hozircha yo'q"
        await query.edit_message_text(
            f"📊 <b>Spam Statistikasi</b>\n\n"
            f"Jami o'chirilgan spam: <b>{total_spam}</b>\n\n"
            f"{lines}",
            reply_markup=back_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Adminlar menyusi ──
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
            "➕ <b>Admin qo'shish</b>\n\n"
            "Yangi admin <b>user ID</b> sini yuboring:\n"
            "(ID ni bilish uchun @userinfobot ga yozing)",
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
            "➖ <b>Adminni o'chirish</b>\nO'chirish uchun tanlang:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("rm_admin:"):
        aid = int(data.split(":")[1])
        bot_admins.discard(aid)
        await query.edit_message_text(f"✅ Admin <code>{aid}</code> o'chirildi.", reply_markup=back_kb(), parse_mode=ParseMode.HTML)

    # ── Salom xabar ──
    elif data == "menu_welcome":
        mod_sessions[user.id] = {"action": "set_welcome_chatid"}
        await query.edit_message_text(
            "👋 <b>Salom xabar o'rnatish</b>\n\n"
            "Avval guruh ID sini yuboring:",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Spam filtri ──
    elif data == "menu_filter":
        await query.edit_message_text(
            f"⚙️ <b>Spam Filtri Sozlamalari</b>\n\n"
            f"🔢 Ban chegarasi: <b>{BAN_AFTER_WARNINGS}</b> ogohlantirish\n"
            f"⏱ Mute vaqti: <b>{MUTE_DURATION_MINUTES}</b> daqiqa\n"
            f"🔗 Max link: <b>{MAX_LINKS_ALLOWED}</b>\n"
            f"👤 Max mention: <b>{MAX_MENTIONS}</b>\n\n"
            f"Sozlamalarni o'zgartirish uchun .env faylni tahrirlang.",
            reply_markup=back_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Broadcast menyu ──
    elif data == "menu_broadcast":
        bc_sessions[user.id] = {"step": "waiting_message"}
        await query.edit_message_text(
            "📢 <b>Anonim Xabar Yuborish</b>\n\n"
            "Boshqa kanaldan xabarni <b>forward qiling</b> yoki "
            "to'g'ridan matn/rasm/video yuboring.\n\n"
            "Keyin xabarni tahrirlash yoki to'g'ridan yuborish imkoniyati beriladi.",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Broadcast: tahrirlash ──
    elif data == "bc_edit":
        session = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q. Qayta boshlang.", reply_markup=back_kb())
            return
        session["step"] = "editing_text"
        await query.edit_message_text(
            "✏️ Yangi matnni yuboring (faqat matn tahrirlash mumkin):\n\n"
            "Rasm/video o'zgarmaydi, faqat <b>caption</b> yangilanadi.",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )

    # ── Broadcast: kanal tanlash ──
    elif data == "bc_choose_channel":
        session = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q.", reply_markup=back_kb())
            return
        channels = await get_admin_channels(context.bot)
        if not channels:
            await query.edit_message_text(
                "❌ Bot hech qanday kanalda admin emas.\n"
                ".env da CHANNEL_IDS ni to'ldiring.",
                reply_markup=back_kb(),
            )
            return
        session["step"] = "choose_channel"
        await query.edit_message_text(
            "📢 Qaysi kanalga yuborasiz?",
            reply_markup=channel_select_kb(channels, "bc_send:"),
        )

    # ── Broadcast: yuborish ──
    elif data.startswith("bc_send:"):
        target_id = int(data.split(":")[1])
        session   = bc_sessions.get(user.id, {})
        if not session:
            await query.edit_message_text("❌ Sessiya yo'q.", reply_markup=back_kb())
            return
        try:
            edited_text = session.get("edited_text")
            if edited_text:
                # Matn tahrirlangan, eski xabarni copy qilib yangi caption bilan yuborish
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
            channels = await get_admin_channels(context.bot)
            ch_title = next((c["title"] for c in channels if c["id"] == target_id), str(target_id))
            bc_sessions.pop(user.id, None)
            await query.edit_message_text(
                f"✅ Xabar <b>{ch_title}</b> kanaliga yuborildi!\n"
                f"👤 Kim yuborgani ko'rsatilmaydi (anonim).",
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
# SHAXSIY CHAT — BROADCAST XABARI QABUL QILISH
# ═══════════════════════════════════════════════════════════════

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.effective_message
    if not user or not msg:
        return

    # ── Moderatsiya sessiyasi ──
    if user.id in mod_sessions:
        session = mod_sessions[user.id]
        action  = session.get("action", "")
        text    = msg.text or ""

        # Admin qo'shish
        if action == "admin_add":
            try:
                new_id = int(text.strip())
                bot_admins.add(new_id)
                mod_sessions.pop(user.id)
                await msg.reply_text(
                    f"✅ <code>{new_id}</code> admin qilib qo'shildi!",
                    reply_markup=main_menu_kb(),
                    parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await msg.reply_text("❌ Noto'g'ri ID. Faqat raqam kiriting.", reply_markup=cancel_kb())
            return

        # Salom xabar — guruh ID
        if action == "set_welcome_chatid":
            try:
                cid = int(text.strip())
                session["chat_id"] = cid
                session["action"]  = "set_welcome_text"
                await msg.reply_text(
                    "✅ Guruh ID qabul qilindi.\n\n"
                    "Endi salom xabarini yuboring.\n"
                    "<code>{name}</code> — a'zo ismi o'rniga qo'yiladi.",
                    reply_markup=cancel_kb(),
                    parse_mode=ParseMode.HTML,
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

        # Moderatsiya — guruh ID kutilmoqda
        if action in ("mod_warn","mod_unwarn","mod_mute","mod_unmute","mod_ban","mod_unban"):
            if session.get("chat_id") is None:
                try:
                    cid = int(text.strip())
                    session["chat_id"] = cid
                    await msg.reply_text(
                        "✅ Guruh ID qabul qilindi.\n\n"
                        "Endi foydalanuvchi <b>@username</b> yoki <b>user ID</b> ni yuboring:",
                        reply_markup=cancel_kb(),
                        parse_mode=ParseMode.HTML,
                    )
                except ValueError:
                    await msg.reply_text("❌ Noto'g'ri ID.", reply_markup=cancel_kb())
                return
            else:
                # User ID yoki username keldi
                target_input = text.strip()
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
                "✅ Xabar qabul qilindi!\n\n"
                "Nima qilmoqchisiz?",
                reply_markup=broadcast_edit_kb(),
            )
            return

        if step == "editing_text":
            session["edited_text"] = msg.text or msg.caption or ""
            session["step"] = "preview"
            await msg.reply_text(
                "✅ Matn yangilandi!\n\n"
                "Endi kanalga yuborish uchun tugmani bosing:",
                reply_markup=broadcast_edit_kb(),
            )
            return

    # Boshqa xabarlar
    if is_bot_admin(user.id):
        await msg.reply_text(
            "🤖 <b>Boshqaruv Paneli</b>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )

# ═══════════════════════════════════════════════════════════════
# MODERATSIYA AMALLARI
# ═══════════════════════════════════════════════════════════════

async def do_mod_action(action: str, chat_id: int, target_id: int, bot: Bot) -> str:
    if action == "mod_warn":
        user_warnings[target_id] = user_warnings.get(target_id, 0) + 1
        w = user_warnings[target_id]
        return f"⚠️ <code>{target_id}</code> ogohlantirish: {w}/{BAN_AFTER_WARNINGS}"

    if action == "mod_unwarn":
        user_warnings[target_id] = max(0, user_warnings.get(target_id, 0) - 1)
        return f"✅ <code>{target_id}</code> ogohlantirish: {user_warnings[target_id]}/{BAN_AFTER_WARNINGS}"

    if action == "mod_mute":
        until = datetime.now() + timedelta(minutes=MUTE_DURATION_MINUTES)
        await bot.restrict_chat_member(
            chat_id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return f"🔇 <code>{target_id}</code> {MUTE_DURATION_MINUTES} daqiqa mute qilindi."

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

    text = message.text or message.caption or ""
    spam, reason = is_spam(text)
    if not spam:
        return

    user_warnings[user.id] = user_warnings.get(user.id, 0) + 1
    group_stats[chat.id]   = group_stats.get(chat.id, 0) + 1
    warnings = user_warnings[user.id]
    logger.info(f"SPAM | {user.id} | {reason} | warn:{warnings}")

    try:
        await message.delete()
    except Exception:
        pass

    if warnings >= BAN_AFTER_WARNINGS:
        try:
            if MUTE_DURATION_MINUTES > 0:
                until = datetime.now() + timedelta(minutes=MUTE_DURATION_MINUTES)
                await context.bot.restrict_chat_member(
                    chat.id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                txt = (f"⛔ {mention(user)} {MUTE_DURATION_MINUTES} daqiqa "
                       f"<b>MUTE</b> qilindi (spam).")
            else:
                await context.bot.ban_chat_member(chat.id, user.id)
                txt = f"🚫 {mention(user)} <b>BAN</b> qilindi (spam)."
            user_warnings[user.id] = 0
            await context.bot.send_message(chat.id, txt, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Ban/mute xato: {e}")
    else:
        remaining = BAN_AFTER_WARNINGS - warnings
        try:
            w = await context.bot.send_message(
                chat.id,
                f"⚠️ {mention(user)}, spam xabar o'chirildi!\n"
                f"📌 Sabab: {reason}\n"
                f"🔢 Ogohlantirish: {warnings}/{BAN_AFTER_WARNINGS} (yana {remaining} ta)",
                parse_mode=ParseMode.HTML,
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat.id, w.message_id), when=15
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════
# GURUH — YANGI A'ZO
# ═══════════════════════════════════════════════════════════════

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    # Majburiy obuna tekshirish
    for member in (msg.new_chat_members or []):
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
    if not BOT_TOKEN or BOT_TOKEN == "SIZNING_BOT_TOKEN_INGIZ":
        print("Xato: .env faylda BOT_TOKEN ni to'ldiring!")
        return
    if not SUPER_ADMIN_ID:
        print("Xato: .env faylda SUPER_ADMIN_ID ni to'ldiring!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Majburiy obuna tekshirish tugmasi
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub$"))

    # Barcha inline tugmalar
    app.add_handler(CallbackQueryHandler(menu_callback))

    # Shaxsiy chat — broadcast va moderatsiya input
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private,
    ))

    # Guruh — spam filtri
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.ChatType.PRIVATE,
        handle_group_message,
    ))

    # Guruh — yangi a'zo
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member,
    ))

    print("=" * 55)
    print("  Spam Filter Bot ishga tushdi!")
    print(f"  Super admin ID : {SUPER_ADMIN_ID}")
    print(f"  Ban chegarasi  : {BAN_AFTER_WARNINGS} ogohlantirish")
    print(f"  Mute vaqti     : {MUTE_DURATION_MINUTES} daqiqa")
    print(f"  Kanallar       : {CHANNEL_IDS_RAW or 'Yoqilmagan'}")
    print(f"  Obuna kanallar : {SUB_CHANNELS_RAW or 'Yoqilmagan'}")
    print("=" * 55)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
