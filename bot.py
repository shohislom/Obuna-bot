import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import (
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ------------------------------------------------------------------ sozlamalar
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# Bir yoki bir nechta guruh: GROUP_ID=-100111,-100222 (vergul bilan)
GROUP_IDS = [
    int(x) for x in os.getenv("GROUP_ID", "0").replace(" ", "").split(",") if x and x != "0"
]
YT_LINK = os.getenv("YT_LINK", "https://youtube.com/@KANALINGIZ")
# Necha kunda bir marta obunani qayta tasdiqlatish (0 = o'chirilgan)
RECHECK_DAYS = int(os.getenv("RECHECK_DAYS", "15"))
# Telegram kanal (bot avtomatik tekshiradi). Bo'sh qoldirsangiz, tekshiruv o'chadi.
TG_CHANNEL = os.getenv("TG_CHANNEL", "").strip()
TG_LINK = f"https://t.me/{TG_CHANNEL.lstrip('@')}" if TG_CHANNEL else ""
# Guruhda yozgan odamning Telegram obunasi necha daqiqada bir qayta tekshiriladi
TG_CHECK_MINUTES = int(os.getenv("TG_CHECK_MINUTES", "10"))

# Guruhda yozish uchun nechta odam qo'shish kerak (0 = bu shart o'chirilgan)
INVITE_REQUIRED = int(os.getenv("INVITE_REQUIRED", "5"))
# YouTube + Telegram obuna talabi (0 = faqat odam qo'shish sharti qoladi)
SUB_REQUIRED = os.getenv("SUB_REQUIRED", "1") == "1"

DB_FILE = "bot.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# MUHIM: restrict_chat_member da ko'rsatilmagan huquq avtomatik TAQIQLANGAN bo'ladi,
# shuning uchun kerakli har bir huquq aniq yozilishi shart.
#
# NO_WRITE  — shartlarni bajarmaganlar: yozolmaydi, lekin odam QO'SHA oladi
#             (5 ta odam shartini bajarishi uchun shart!).
# CAN_WRITE — tasdiqlanganlar: matn, rasm, video, videoxabar, reaksiya,
#             odam qo'shish va o'z tegini tahrirlash.
NO_WRITE = ChatPermissions(
    can_send_messages=False,
    can_invite_users=True,
)
CAN_WRITE = ChatPermissions(
    can_send_messages=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_react_to_messages=True,
    can_invite_users=True,
    can_edit_tag=True,
)

WARNING_TEXT = (
    "⚠️ <b>Diqqat! Agar keyinchalik obunani bekor qilsangiz, "
    "guruhdan chiqarib yuboriladi (ban beriladi).</b>"
)

if TG_CHANNEL:
    WELCOME_TEXT = (
        "👋 Salom! Guruhda yozish uchun:\n\n"
        "1️⃣ Telegram kanalimizga obuna bo'ling\n"
        "2️⃣ YouTube kanalimizga obuna bo'ling\n"
        "3️⃣ Pastdagi <b>«✅ Telegram obunasini tekshirish»</b> tugmasini bosing, "
        "so'ng YouTube obunangiz <b>skrinshotini shu yerga yuklang</b>.\n\n"
        + WARNING_TEXT
    )
else:
    WELCOME_TEXT = (
        "👋 Salom! Guruhda yozish uchun avval YouTube kanalimizga obuna bo'ling.\n\n"
        "📸 Obuna bo'lgach, obuna bo'lganingiz ko'rinib turgan "
        "<b>skrinshotni shu yerga yuklang</b>.\n\n"
        + WARNING_TEXT
    )


# ------------------------------------------------------------------ baza
def db(query: str, args: tuple = (), fetch: bool = False):
    with closing(sqlite3.connect(DB_FILE)) as con:
        cur = con.execute(query, args)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows


def init_db():
    db(
        """CREATE TABLE IF NOT EXISTS users(
            user_id     INTEGER PRIMARY KEY,
            name        TEXT,
            tg_username TEXT,
            yt_handle   TEXT,
            status      TEXT DEFAULT 'new',
            verified_at INTEGER,
            remind_at   INTEGER
        )"""
    )
    db(
        """CREATE TABLE IF NOT EXISTS invites(
            invited_id INTEGER PRIMARY KEY,
            inviter_id INTEGER,
            chat_id    INTEGER,
            active     INTEGER DEFAULT 1,
            ts         INTEGER
        )"""
    )
    db("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    try:
        db("ALTER TABLE users ADD COLUMN invites_ok INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # ustun allaqachon bor


def touch_user(user):
    db(
        "INSERT OR IGNORE INTO users(user_id, name, tg_username) VALUES(?,?,?)",
        (user.id, user.full_name, user.username),
    )
    db(
        "UPDATE users SET name=?, tg_username=? WHERE user_id=?",
        (user.full_name, user.username, user.id),
    )


def enforce_on() -> bool:
    """Tekshiruv yoqilganmi? (/pauza va /davom buyruqlari boshqaradi)"""
    rows = db("SELECT value FROM settings WHERE key='enforce'", fetch=True)
    return not rows or rows[0][0] != "0"


def set_enforce(on: bool):
    db("INSERT OR REPLACE INTO settings(key, value) VALUES('enforce', ?)", ("1" if on else "0",))


def get_status(user_id: int) -> str:
    rows = db("SELECT status FROM users WHERE user_id=?", (user_id,), True)
    return rows[0][0] if rows else "new"


# ------------------------------------------------------------------ yordamchi
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def ban_user(uid: int, reason: str = ""):
    for gid in GROUP_IDS:
        try:
            await bot.ban_chat_member(gid, uid)
        except Exception as e:
            logging.warning("Ban xatosi %s (guruh %s): %s", uid, gid, e)
    db("UPDATE users SET status='banned' WHERE user_id=?", (uid,))
    try:
        await bot.send_message(uid, f"🚫 Siz guruhdan chiqarildingiz. {reason}".strip())
    except Exception:
        pass


async def is_tg_subscribed(uid: int):
    """True / False — Telegram kanalga obuna bo'lgan yoki yo'q; None — tekshirib bo'lmadi."""
    if not TG_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(TG_CHANNEL, uid)
    except Exception as e:
        logging.warning("Telegram kanalni tekshirib bo'lmadi (bot kanalda adminmi?): %s", e)
        return None
    if member.status in ("creator", "administrator", "member"):
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False  # left / kicked


tg_error_notified = False


async def alert_admin_tg_error():
    global tg_error_notified
    if tg_error_notified:
        return
    tg_error_notified = True
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Telegram kanal ({html.escape(TG_CHANNEL)}) obunasini tekshirib bo'lmadi. "
            f"Bot shu kanalda admin ekanini va TG_CHANNEL to'g'ri yozilganini tekshiring.",
        )
    except Exception:
        pass


def invites_ok(uid: int) -> bool:
    """Odam qo'shish sharti bajarilganmi (yoqilmagan bo'lsa — har doim True)."""
    if INVITE_REQUIRED <= 0:
        return True
    rows = db("SELECT invites_ok FROM users WHERE user_id=?", (uid,), True)
    return bool(rows and rows[0][0])


def invite_count(uid: int) -> int:
    return db(
        "SELECT COUNT(*) FROM invites WHERE inviter_id=? AND active=1", (uid,), True
    )[0][0]


async def subscription_ok(uid: int) -> bool:
    """YouTube tasdig'i amalda va Telegram kanalga obuna bo'lganmi."""
    if not SUB_REQUIRED:
        return True
    if get_status(uid) not in ("approved", "recheck"):
        return False
    rows = db("SELECT verified_at FROM users WHERE user_id=?", (uid,), True)
    ts = rows[0][0] if rows else None
    if RECHECK_DAYS > 0 and ts and time.time() - ts > RECHECK_DAYS * 86400:
        return False
    return (await is_tg_subscribed(uid)) is True


async def unlock_everywhere(uid: int):
    for gid in GROUP_IDS:
        try:
            await bot.restrict_chat_member(gid, uid, permissions=CAN_WRITE, use_independent_chat_permissions=True)
        except Exception:
            pass  # foydalanuvchi bu guruhda bo'lmasligi mumkin


async def group_kb(sub: bool, inv: bool) -> InlineKeyboardMarkup:
    me = await bot.get_me()
    rows = []
    if sub:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📺 Obunani tasdiqlash",
                    url=f"https://t.me/{me.username}?start=verify",
                )
            ]
        )
    if inv:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {INVITE_REQUIRED} ta odam qo'shdim", callback_data="invcheck"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invite_phrase() -> str:
    return f"guruhga {INVITE_REQUIRED} ta odam qo'shing va «✅ Odam qo'shdim» tugmasini bosing"


def unlock_text(uid: int, base: str) -> str:
    if invites_ok(uid):
        return base + " Guruhda yana yozishingiz mumkin."
    return base + f" Endi {invite_phrase()}."


async def restore_user(uid: int):
    db("UPDATE users SET status='approved' WHERE user_id=?", (uid,))
    if invites_ok(uid):
        await unlock_everywhere(uid)


class Form(StatesGroup):
    photo = State()
    handle = State()


# ------------------------------------------------------------------ guruh
@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Chat ID: <code>{m.chat.id}</code>")


@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated):
    if event.chat.id not in GROUP_IDS:
        return
    gid = event.chat.id
    user = event.new_chat_member.user
    logging.info("Yangi a'zo kirdi: %s (%s)", user.full_name, user.id)
    if user.is_bot:
        return
    touch_user(user)

    # Kim qo'shdi? (havola orqali o'zi kirgan bo'lsa, from_user == user — sanalmaydi)
    inviter = event.from_user
    if INVITE_REQUIRED > 0 and inviter and inviter.id != user.id and not inviter.is_bot:
        db(
            "INSERT OR IGNORE INTO invites(invited_id, inviter_id, chat_id, active, ts) "
            "VALUES(?,?,?,1,?)",
            (user.id, inviter.id, gid, int(time.time())),
        )
        # Chiqib, qayta qo'shilgan bo'lsa — asl taklif qiluvchiga qaytadan sanaladi (bir odam 1 marta)
        db("UPDATE invites SET active=1 WHERE invited_id=? AND chat_id=?", (user.id, gid))

        # Oxirgi (5-) odam qo'shilishi bilan shart avtomatik bajariladi — tugma bosish shart emas
        if invite_count(inviter.id) >= INVITE_REQUIRED and not invites_ok(inviter.id):
            touch_user(inviter)
            db("UPDATE users SET invites_ok=1 WHERE user_id=?", (inviter.id,))
            who = f'<a href="tg://user?id={inviter.id}">{html.escape(inviter.full_name)}</a>'
            if await subscription_ok(inviter.id):
                await unlock_everywhere(inviter.id)
                note = f"🎉 {who} {INVITE_REQUIRED} ta odam qo'shdi, endi guruhda yoza oladi!"
            else:
                note = (
                    f"✅ {who}, {INVITE_REQUIRED} ta odam qo'shish sharti bajarildi. "
                    f"Endi botda obunangizni tasdiqlang."
                )
            try:
                sent = await bot.send_message(gid, note)
                asyncio.create_task(delete_later(gid, sent.message_id, 30))
            except Exception:
                pass

    if not enforce_on():
        return  # pauza: yangi a'zolar cheklanmaydi (odam qo'shish sanalishda davom etadi)

    sub_done = (not SUB_REQUIRED) or get_status(user.id) in ("approved", "recheck")
    inv_done = invites_ok(user.id)

    # Hamma shart bajargan foydalanuvchi qaytib kirsa — yozish huquqini qaytaramiz
    if sub_done and inv_done:
        try:
            await bot.restrict_chat_member(gid, user.id, permissions=CAN_WRITE, use_independent_chat_permissions=True)
        except Exception as e:
            logging.warning("Huquq qaytarishda xato: %s", e)
        return

    try:
        await bot.restrict_chat_member(gid, user.id, permissions=NO_WRITE, use_independent_chat_permissions=True)
    except Exception as e:
        logging.warning("Cheklashda xato: %s", e)
        return

    parts = []
    if not sub_done:
        parts.append(f"{await channels_phrase(user.id)} botda tasdiqlang")
    if not inv_done:
        parts.append(invite_phrase())
    await bot.send_message(
        gid,
        f"👋 {html.escape(user.full_name)}, guruhda yozish uchun: " + "; ".join(parts) + ".",
        reply_markup=await group_kb(sub=not sub_done, inv=not inv_done),
    )


@dp.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_leave(event: ChatMemberUpdated):
    if event.chat.id not in GROUP_IDS:
        return
    # Qo'shilgan odam guruhdan chiqib ketsa, taklif qiluvchining hisobidan ayiriladi
    db(
        "UPDATE invites SET active=0 WHERE invited_id=? AND chat_id=?",
        (event.new_chat_member.user.id, event.chat.id),
    )


# ------------------------------------------------------------------ admin buyruqlari
@dp.message(Command("ban"), F.from_user.id == ADMIN_ID)
async def cmd_ban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Ishlatish: <code>/ban 123456789</code>")
    uid = int(command.args.strip())
    await ban_user(uid, "Obuna bekor qilingani sababli.")
    await m.answer(f"🚫 {uid} ban qilindi.")


@dp.message(Command("unban"), F.from_user.id == ADMIN_ID)
async def cmd_unban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Ishlatish: <code>/unban 123456789</code>")
    uid = int(command.args.strip())
    errors = []
    for gid in GROUP_IDS:
        try:
            await bot.unban_chat_member(gid, uid, only_if_banned=True)
        except Exception as e:
            errors.append(str(e))
    if GROUP_IDS and len(errors) == len(GROUP_IDS):
        return await m.answer(f"Xato: {html.escape(errors[0])}")
    db("UPDATE users SET status='new' WHERE user_id=?", (uid,))
    await m.answer(f"✅ {uid} blokdan chiqarildi. U qayta kirib, qaytadan tasdiqlashi kerak.")


@dp.message(Command("list"), F.from_user.id == ADMIN_ID)
async def cmd_list(m: Message):
    rows = db(
        "SELECT user_id, name, yt_handle, status, verified_at FROM users "
        "WHERE status IN ('approved','recheck','expired','tg_left') ORDER BY verified_at",
        fetch=True,
    )
    if not rows:
        return await m.answer("Tasdiqlanganlar hali yo'q.")
    marks = {"expired": " ⏳ muddati tugagan", "tg_left": " 📣 Telegramdan chiqqan"}
    lines = []
    for i, (uid, name, yt, st, ts) in enumerate(rows, 1):
        date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"
        lines.append(
            f"{i}. {html.escape(name or '-')} | {html.escape(yt or '-')} | "
            f"<code>{uid}</code> | {date}{marks.get(st, '')}"
        )
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await m.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await m.answer(chunk)


@dp.message(Command("ruxsat"), F.from_user.id == ADMIN_ID)
async def cmd_allow(m: Message, command: CommandObject):
    """Odam qo'shish shartidan ozod qilish: /ruxsat 123456789"""
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Ishlatish: <code>/ruxsat 123456789</code>")
    uid = int(command.args.strip())
    db("INSERT OR IGNORE INTO users(user_id, name) VALUES(?, '-')", (uid,))
    db("UPDATE users SET invites_ok=1 WHERE user_id=?", (uid,))
    if await subscription_ok(uid):
        await unlock_everywhere(uid)
        return await m.answer(f"✅ {uid} odam qo'shish shartidan ozod qilindi va yozish huquqi berildi.")
    await m.answer(
        f"✅ {uid} odam qo'shish shartidan ozod qilindi. Obuna tasdig'i hali yetarli emas, "
        f"u bajarilgach yoza oladi."
    )


@dp.message(Command("yangila"), F.from_user.id == ADMIN_ID)
async def cmd_refresh(m: Message):
    """Allaqachon cheklangan a'zolarning huquqlarini to'g'rilaydi (a'zo qo'shish huquqi qaytadi)."""
    rows = db("SELECT user_id FROM users WHERE status != 'banned'", fetch=True)
    fixed = 0
    for (uid,) in rows:
        if uid == ADMIN_ID:
            continue
        eligible = invites_ok(uid) and await subscription_ok(uid)
        perms = CAN_WRITE if eligible else NO_WRITE
        done = False
        for gid in GROUP_IDS:
            try:
                member = await bot.get_chat_member(gid, uid)
                if member.status != "restricted":
                    continue  # cheklanmagan (yoki admin / guruhda emas) — tegmaymiz
                await bot.restrict_chat_member(gid, uid, permissions=perms, use_independent_chat_permissions=True)
                done = True
            except Exception:
                pass
        fixed += done
        await asyncio.sleep(0.1)
    await m.answer(f"✅ {fixed} ta cheklangan a'zoning huquqlari yangilandi.")


@dp.message(Command("pauza"), F.from_user.id == ADMIN_ID)
async def cmd_pause(m: Message):
    set_enforce(False)
    await m.answer(
        "⏸ Tekshiruv to'xtatildi: bot hech kimni cheklamaydi va xabarlarini o'chirmaydi "
        "(faqat havola/@username taqiqi ishlaydi). Qayta yoqish: /davom"
    )


@dp.message(Command("davom"), F.from_user.id == ADMIN_ID)
async def cmd_resume(m: Message):
    set_enforce(True)
    await m.answer("▶️ Tekshiruv yoqildi.")


@dp.message(Command("ochish"), F.from_user.id == ADMIN_ID)
async def cmd_unlock_all(m: Message):
    """Botga ma'lum hamma foydalanuvchining cheklovini olib tashlaydi va tekshiruvni to'xtatadi."""
    set_enforce(False)
    users = db("SELECT user_id FROM users WHERE status != 'banned'", fetch=True)
    await m.answer(f"⏳ {len(users)} ta foydalanuvchining cheklovi olib tashlanmoqda...")
    done = 0
    for gid in GROUP_IDS:
        try:
            chat = await bot.get_chat(gid)
            perms = (chat.permissions or CAN_WRITE).model_copy(update={"can_invite_users": True})
        except Exception:
            perms = CAN_WRITE
        for (uid,) in users:
            if uid == ADMIN_ID:
                continue
            for _ in range(3):
                try:
                    member = await bot.get_chat_member(gid, uid)
                    if member.status == "restricted":
                        await bot.restrict_chat_member(gid, uid, permissions=perms, use_independent_chat_permissions=True)
                        done += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    break  # guruhda yo'q yoki admin
            await asyncio.sleep(0.05)
    await m.answer(
        f"✅ Tayyor: {done} ta cheklov olib tashlandi. Tekshiruv hozir <b>to'xtatilgan</b>. "
        f"Tayyor bo'lganingizda /post yozib, keyin /davom bilan yoqing."
    )


@dp.message(Command("post"), F.from_user.id == ADMIN_ID)
async def cmd_post(m: Message):
    """Guruhga umumiy xabar tashlaydi (mavjud a'zolar uchun)."""
    if not GROUP_IDS:
        return await m.answer("GROUP_ID hali sozlanmagan (.env).")
    parts = []
    if SUB_REQUIRED:
        parts.append("Telegram va YouTube kanallarimizga obuna bo'lib, botda tasdiqlang")
    if INVITE_REQUIRED > 0:
        parts.append(invite_phrase())
    kb = await group_kb(sub=SUB_REQUIRED, inv=INVITE_REQUIRED > 0)
    for gid in GROUP_IDS:
        try:
            sent = await bot.send_message(
                gid,
                "📢 Guruhda yozish uchun: " + "; ".join(parts) + ".",
                reply_markup=kb,
            )
            try:
                await bot.pin_chat_message(gid, sent.message_id)
            except Exception:
                pass
        except Exception as e:
            await m.answer(f"Guruh {gid} ga yuborib bo'lmadi: {html.escape(str(e))}")
    await m.answer("✅ Guruh(lar)ga xabar tashlandi.")


# ------------------------------------------------------------------ foydalanuvchi: botda
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message, state: FSMContext):
    touch_user(m.from_user)
    uid = m.from_user.id
    status = get_status(uid)
    if status == "banned":
        return await m.answer("🚫 Siz guruhdan chiqarilgansiz. Admin bilan bog'laning.")

    tg_res = await is_tg_subscribed(uid) if TG_CHANNEL else True
    if tg_res is None:
        await alert_admin_tg_error()

    # Telegram kanalga allaqachon obuna bo'lgan — faqat YouTube haqida gapiramiz
    if TG_CHANNEL and tg_res is True:
        if status == "tg_left":
            await restore_user(uid)
            return await m.answer(unlock_text(uid, "✅ Telegram obunangiz tiklandi."))
        if status == "approved":
            rows = db("SELECT verified_at FROM users WHERE user_id=?", (uid,), True)
            ts = rows[0][0] if rows else None
            extra = ""
            if RECHECK_DAYS > 0 and ts:
                nxt = time.strftime("%Y-%m-%d", time.localtime(ts + RECHECK_DAYS * 86400))
                extra = f" Keyingi tasdiqlash: {nxt}."
            return await m.answer(f"✅ Siz tasdiqlangansiz.{extra}")
        await state.set_state(Form.photo)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📺 YouTube kanal", url=YT_LINK)]]
        )
        return await m.answer(
            "👋 Salom! Telegram kanalimizga obunangiz tasdiqlandi ✅\n\n"
            "Endi YouTube kanalimizga obuna bo'ling va obuna bo'lganingiz ko'rinib turgan "
            "<b>skrinshotni shu yerga yuklang</b>.\n\n" + WARNING_TEXT,
            reply_markup=kb,
        )

    links = []
    if TG_CHANNEL:
        links.append(InlineKeyboardButton(text="📣 Telegram kanal", url=TG_LINK))
    links.append(InlineKeyboardButton(text="📺 YouTube kanal", url=YT_LINK))
    rows_kb = [links]
    if TG_CHANNEL:
        rows_kb.append(
            [InlineKeyboardButton(text="✅ Telegram obunasini tekshirish", callback_data="tgcheck")]
        )
    else:
        await state.set_state(Form.photo)
    await m.answer(WELCOME_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))


@dp.callback_query(F.data == "tgcheck")
async def tgcheck(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    touch_user(c.from_user)
    status = get_status(uid)
    if status == "banned":
        return await c.answer("Siz guruhdan chiqarilgansiz. Admin bilan bog'laning.", show_alert=True)

    res = await is_tg_subscribed(uid)
    if res is None:
        await alert_admin_tg_error()
        return await c.answer("Tekshirib bo'lmadi. Birozdan keyin qayta urinib ko'ring.", show_alert=True)
    if res is False:
        return await c.answer(
            "❌ Siz Telegram kanalga hali obuna bo'lmagansiz. Obuna bo'lib, qayta bosing.",
            show_alert=True,
        )

    await c.answer("✅ Telegram obunasi tasdiqlandi!")
    if status == "tg_left":
        await restore_user(uid)
        return await c.message.answer(unlock_text(uid, "✅ Telegram obunangiz tiklandi."))
    if status == "approved":
        return await c.message.answer("✅ Siz allaqachon tasdiqlangansiz.")
    await state.set_state(Form.photo)
    await c.message.answer(
        "✅ Telegram obunangiz tasdiqlandi.\n\n"
        "📸 Endi <b>YouTube obunangiz skrinshotini</b> shu yerga yuklang."
    )


@dp.message(Form.photo, F.photo, F.chat.type == "private")
async def got_photo(m: Message, state: FSMContext):
    await state.update_data(file_id=m.photo[-1].file_id)
    await state.set_state(Form.handle)
    await m.answer(
        "✍️ Endi YouTube akkauntingiz nomini (<b>@handle</b>) yozib yuboring.\n"
        "Masalan: <code>@ismingiz</code>"
    )


@dp.message(Form.photo, F.chat.type == "private")
async def photo_expected(m: Message):
    await m.answer("Iltimos, skrinshotni <b>rasm</b> sifatida yuboring.")


@dp.message(Form.handle, F.text, F.chat.type == "private")
async def got_handle(m: Message, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("file_id")
    if not file_id:
        await state.set_state(Form.photo)
        return await m.answer("Avval skrinshotni yuboring.")

    if TG_CHANNEL:
        res = await is_tg_subscribed(m.from_user.id)
        if res is False:
            await state.clear()
            return await m.answer(
                "❌ Telegram kanalga obuna bo'lmagansiz. Obuna bo'lib, /start orqali qaytadan boshlang."
            )
        if res is None:
            await alert_admin_tg_error()

    handle = m.text.strip()[:100]
    uid = m.from_user.id
    db("UPDATE users SET yt_handle=? WHERE user_id=?", (handle, uid))
    # Oddiy holatlarda "pending" qilamiz; qayta tekshiruvdagilar statusini saqlaymiz
    db(
        "UPDATE users SET status='pending' WHERE user_id=? AND status IN ('new','rejected','pending')",
        (uid,),
    )
    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{uid}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"no:{uid}"),
            ]
        ]
    )
    tg_user = f"@{m.from_user.username}" if m.from_user.username else "username yo'q"
    await bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"👤 {html.escape(m.from_user.full_name)}\n"
            f"Telegram: {html.escape(tg_user)}\n"
            + ("Telegram kanal: ✅ obuna (bot tekshirdi)\n" if TG_CHANNEL else "")
            + f"ID: <code>{uid}</code>\n"
            f"YouTube: {html.escape(handle)}"
        ),
        reply_markup=kb,
    )
    await m.answer("⏳ Ma'lumotlar adminga yuborildi. Javobni kuting.")


@dp.message(F.chat.type == "private", ~F.text.startswith("/"))
async def fallback(m: Message):
    if is_admin(m.from_user.id):
        return
    await m.answer("Boshlash uchun /start ni bosing.")


# ------------------------------------------------------------------ admin qarori
@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decision(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)

    action, uid_s = c.data.split(":")
    uid = int(uid_s)

    if action == "ok":
        if invites_ok(uid):
            ok_count, last_err = 0, None
            for gid in GROUP_IDS:
                try:
                    await bot.restrict_chat_member(gid, uid, permissions=CAN_WRITE, use_independent_chat_permissions=True)
                    ok_count += 1
                except Exception as e:  # foydalanuvchi bu guruhda bo'lmasligi mumkin
                    last_err = e
            if GROUP_IDS and ok_count == 0:
                return await c.answer(f"Xato: {last_err}", show_alert=True)
            note = "✅ Tasdiqlandi! Endi guruhda yozishingiz mumkin."
        else:
            note = f"✅ YouTube obunangiz tasdiqlandi. Endi {invite_phrase()}."
        db(
            "UPDATE users SET status='approved', verified_at=?, remind_at=NULL WHERE user_id=?",
            (int(time.time()), uid),
        )
        try:
            await bot.send_message(uid, note)
        except Exception:
            pass
        status = "✅ Tasdiqlandi"
    else:
        db(
            "UPDATE users SET status='rejected' WHERE user_id=? AND status IN ('new','pending','rejected')",
            (uid,),
        )
        try:
            await bot.send_message(
                uid,
                "❌ Skrinshot qabul qilinmadi. Obuna bo'lib, /start orqali qaytadan yuboring.",
            )
        except Exception:
            pass
        status = "❌ Rad etildi"

    await c.message.edit_caption(caption=f"{c.message.html_text}\n\n{status}")
    await c.answer()


@dp.callback_query(F.data == "invcheck", F.message.chat.id.in_(GROUP_IDS))
async def inv_check(c: CallbackQuery):
    """Guruhning o'zidagi «Odam qo'shdim» tugmasi."""
    uid = c.from_user.id
    touch_user(c.from_user)
    if INVITE_REQUIRED <= 0:
        return await c.answer("Bu shart yoqilmagan.", show_alert=True)
    if invites_ok(uid):
        return await c.answer("✅ Odam qo'shish sharti allaqachon bajarilgan.", show_alert=True)

    n = invite_count(uid)
    if n < INVITE_REQUIRED:
        return await c.answer(
            f"Siz hozircha {n} ta odam qo'shdingiz. Yana {INVITE_REQUIRED - n} ta odam "
            f"qo'shing va qayta bosing.",
            show_alert=True,
        )

    db("UPDATE users SET invites_ok=1 WHERE user_id=?", (uid,))
    if await subscription_ok(uid):
        await unlock_everywhere(uid)
        return await c.answer("🎉 Tabriklaymiz! Endi guruhda yoza olasiz.", show_alert=True)
    await c.answer(
        "✅ Odam qo'shish sharti bajarildi! Endi «📺 Obunani tasdiqlash» tugmasi orqali "
        "obunangizni tasdiqlang.",
        show_alert=True,
    )


# ------------------------------------------------------------------ guruh qo'riqchisi
# Tasdiqlanmagan a'zo guruhga yozsa: xabarini o'chiradi, yozishni cheklaydi
# va tasdiqlash tugmali ogohlantirish yuboradi.
warned: dict[int, float] = {}


async def delete_later(chat_id: int, message_id: int, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ------------------------------------------------------------------ havola filtri
# Admin bo'lmaganlar havola (https://..., t.me/...) va @username yubora olmaydi.
# Ruxsat etilganlar: o'z kanallaringiz, botning o'zi va ALLOWED_LINKS (vergul bilan).
LINK_RE = re.compile(
    r"(?i)(?:https?://|www\.|t\.me/|telegram\.(?:me|dog)/)\S+|(?<![\w.])@\w{3,}"
)
link_warned: dict[int, float] = {}
_allowed_cache = None


async def allowed_tokens() -> set:
    global _allowed_cache
    if _allowed_cache is None:
        toks = {t.strip().lower() for t in os.getenv("ALLOWED_LINKS", "").split(",") if t.strip()}
        if TG_CHANNEL:
            toks.add(TG_CHANNEL.lstrip("@").lower())
        yt = YT_LINK.rstrip("/").split("/")[-1].lstrip("@").lower()
        if yt and yt != "kanalingiz":
            toks.add(yt)
        try:
            toks.add((await bot.get_me()).username.lower())
        except Exception:
            pass
        _allowed_cache = toks
    return _allowed_cache


async def has_forbidden_link(m: Message) -> bool:
    text = m.text or m.caption or ""
    entities = (m.entities if m.text else m.caption_entities) or []
    found = []
    for e in entities:
        if e.type in ("url", "mention"):
            found.append(e.extract_from(text))
        elif e.type == "text_link" and e.url:
            found.append(e.url)
    found += [x.group(0) for x in LINK_RE.finditer(text)]
    if not found:
        return False
    allowed = await allowed_tokens()
    return any(not any(tok in f.lower() for tok in allowed) for f in found)


async def punish_link(m: Message) -> bool:
    """Taqiqlangan havola bo'lsa (va yuboruvchi admin bo'lmasa) xabarni o'chiradi."""
    if m.sender_chat or not m.from_user or m.from_user.is_bot:
        return False
    if not await has_forbidden_link(m):
        return False
    try:
        member = await bot.get_chat_member(m.chat.id, m.from_user.id)
        if member.status in ("creator", "administrator"):
            return False
    except Exception:
        pass
    try:
        await bot.delete_message(m.chat.id, m.message_id)
    except Exception as e:
        logging.warning("Havolali xabarni o'chirib bo'lmadi (Delete messages huquqi bormi?): %s", e)
    now = time.time()
    if now - link_warned.get(m.from_user.id, 0) >= 300:
        link_warned[m.from_user.id] = now
        try:
            sent = await bot.send_message(
                m.chat.id,
                f'🚫 <a href="tg://user?id={m.from_user.id}">{html.escape(m.from_user.full_name)}</a>, '
                f"guruhda havola va @username yuborish taqiqlangan.",
            )
            asyncio.create_task(delete_later(m.chat.id, sent.message_id, 20))
        except Exception:
            pass
    return True


tg_ok_cache: dict[int, float] = {}  # uid -> Telegram obunasi oxirgi marta tasdiqlangan vaqt


async def tg_status_cached(uid: int):
    """Telegram obunasi: True / False / None (tekshirib bo'lmadi).
    Ijobiy natija TG_CHECK_MINUTES daqiqaga eslab qolinadi (Telegramga ortiqcha so'rov bormasin)."""
    if not TG_CHANNEL:
        return True
    now = time.time()
    if now - tg_ok_cache.get(uid, 0) < TG_CHECK_MINUTES * 60:
        return True
    res = await is_tg_subscribed(uid)
    if res is True:
        tg_ok_cache[uid] = now
    return res


async def channels_phrase(uid: int) -> str:
    """Telegramga allaqachon obuna bo'lgan odamga faqat YouTube haqida yozamiz."""
    if TG_CHANNEL and (await tg_status_cached(uid)) is False:
        return "Telegram va YouTube kanallarimizga obuna bo'lib"
    return "YouTube kanalimizga obuna bo'lib"


@dp.message(F.chat.id.in_(GROUP_IDS))
async def guard(m: Message):
    # Anonim admin, kanal nomidan yozilganlar va botlar tegilmaydi
    if m.sender_chat or not m.from_user or m.from_user.is_bot:
        return
    # Faqat foydalanuvchi kontenti (xizmat xabarlariga tegmaymiz)
    if not any(
        [m.text, m.caption, m.photo, m.video, m.document, m.sticker, m.voice,
         m.audio, m.animation, m.video_note, m.poll]
    ):
        return

    # 0) Havola / @username (admin bo'lmaganlar uchun) — darhol o'chiriladi
    if await punish_link(m):
        return

    # Tekshiruv pauzada bo'lsa (/pauza), hech kimni cheklamaymiz
    if not enforce_on():
        return

    uid = m.from_user.id
    status = get_status(uid)
    verified = status in ("approved", "recheck")

    # Nima yetishmayapti: Telegram, YouTube yoki odam qo'shishmi?
    need_tg = need_yt = expired = False
    if SUB_REQUIRED:
        tg_res = await tg_status_cached(uid)
        if tg_res is None:
            await alert_admin_tg_error()
        need_tg = tg_res is False
        if verified:
            rows = db("SELECT verified_at FROM users WHERE user_id=?", (uid,), True)
            verified_at = rows[0][0] if rows else None
            expired = bool(
                RECHECK_DAYS > 0 and verified_at
                and time.time() - verified_at > RECHECK_DAYS * 86400
            )
            need_yt = expired
        else:
            need_yt = True
    need_inv = not invites_ok(uid)
    # 5 ta odam allaqachon qo'shgan bo'lsa — tugma bosmasdan ham sanaladi
    if need_inv and invite_count(uid) >= INVITE_REQUIRED:
        touch_user(m.from_user)
        db("UPDATE users SET invites_ok=1 WHERE user_id=?", (uid,))
        need_inv = False

    if not (need_tg or need_yt or need_inv):
        return  # hammasi joyida

    # Admin va egasiga tegmaymiz
    try:
        member = await bot.get_chat_member(m.chat.id, uid)
        if member.status in ("creator", "administrator"):
            return
    except Exception:
        pass

    touch_user(m.from_user)
    if verified and (expired or need_tg):
        db("UPDATE users SET status=? WHERE user_id=?", ("expired" if expired else "tg_left", uid))

    try:
        await bot.delete_message(m.chat.id, m.message_id)
    except Exception as e:
        logging.warning("Xabarni o'chirib bo'lmadi (Delete messages huquqi bormi?): %s", e)
    try:
        await bot.restrict_chat_member(m.chat.id, uid, permissions=NO_WRITE, use_independent_chat_permissions=True)
    except Exception as e:
        logging.warning("Cheklashda xato: %s", e)

    now = time.time()
    if now - warned.get(uid, 0) < 300:
        return
    warned[uid] = now

    parts = []
    if need_tg and need_yt:
        parts.append("Telegram va YouTube kanallarimizga obuna bo'lib, botda tasdiqlang")
    elif need_tg:
        parts.append("Telegram kanalimizga qayta obuna bo'ling va botda tekshiring")
    elif expired:
        parts.append("YouTube obunangizni qayta tasdiqlang (botga yangi skrinshot yuboring)")
    elif need_yt:
        parts.append("YouTube kanalimizga obuna bo'lib, botga skrinshot yuboring")
    if need_inv:
        parts.append(invite_phrase())

    sent = await bot.send_message(
        m.chat.id,
        f'⚠️ <a href="tg://user?id={uid}">{html.escape(m.from_user.full_name)}</a>, '
        f"xabaringiz o'chirildi. Guruhda yozish uchun: " + "; ".join(parts) + ".",
        reply_markup=await group_kb(sub=need_tg or need_yt, inv=need_inv),
    )
    asyncio.create_task(delete_later(m.chat.id, sent.message_id, 60))


@dp.edited_message(F.chat.id.in_(GROUP_IDS))
async def guard_edited(m: Message):
    await punish_link(m)


# ------------------------------------------------------------------ bot profilidagi hisoblagich
# Bot nomi tagidagi (qisqa tavsif) va bo'sh chatdagi tavsifda tasdiqlanganlar sonini ko'rsatadi.
last_count = None


async def update_profile_texts():
    global last_count
    n = db(
        "SELECT COUNT(*) FROM users WHERE status IN ('approved','recheck')", fetch=True
    )[0][0]
    if n == last_count:
        return
    try:
        await bot.set_my_short_description(
            short_description=f"✅ {n} ta tasdiqlangan a'zo | YouTube obuna tekshiruvchi"
        )
        await bot.set_my_description(
            description=(
                "YouTube kanalga obuna bo'lganingizni tasdiqlab, guruhda yozish "
                f"huquqini oling.\n\n✅ Hozirgacha {n} ta a'zo tasdiqlangan."
            )
        )
        last_count = n
    except Exception as e:
        logging.warning("Profil tavsifini yangilashda xato: %s", e)


async def profile_loop():
    while True:
        try:
            await update_profile_texts()
        except Exception:
            logging.exception("profile_loop xatosi")
        await asyncio.sleep(600)  # har 10 daqiqada, faqat son o'zgargan bo'lsa yangilanadi


# ------------------------------------------------------------------ ishga tushirish
async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini tekshiring.")
    init_db()
    if not GROUP_IDS:
        logging.warning("GROUP_ID = 0. Guruhda /id yozib, raqamni .env ga kiriting.")
    else:
        logging.info("Nazoratdagi guruhlar: %s", GROUP_IDS)
    if TG_CHANNEL:
        logging.info("Telegram kanal tekshiruvi yoqilgan: %s", TG_CHANNEL)
    asyncio.create_task(profile_loop())
    await dp.start_polling(
        bot, allowed_updates=["message", "edited_message", "callback_query", "chat_member"]
    )


if __name__ == "__main__":
    asyncio.run(main())
