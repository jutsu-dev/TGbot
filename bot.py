import os
import asyncio
import logging
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

# ====================
# ENV & LOGGING (Render → Environment Variables)
# ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "100"))

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не найден! Добавь переменные в Render → Environment")

logging.basicConfig(level=logging.INFO)

# ====================
# DB
# ====================
DB_PATH = "bot.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.executescript(
    """
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        first_name TEXT,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
        balance INTEGER DEFAULT 0,
        completed_tasks INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS sponsors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        title TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        reward INTEGER NOT NULL,
        target_chat_id INTEGER NOT NULL,
        url TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS user_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        status TEXT DEFAULT 'new',
        checked_at TEXT,
        UNIQUE(user_id, task_id)
    );

    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        game_account TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        processed_by INTEGER,
        processed_at TEXT,
        comment TEXT
    );

    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE NOT NULL,
        role TEXT DEFAULT 'admin'
    );
    """
)
conn.commit()

if OWNER_ID:
    cur.execute("INSERT OR IGNORE INTO admins (tg_id, role) VALUES (?, 'owner')", (OWNER_ID,))
    conn.commit()

# ====================
# HELPERS & KEYBOARDS
# ====================
def get_user(tg_id: int) -> sqlite3.Row | None:
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    return cur.fetchone()

def ensure_user(msg: Message):
    u = get_user(msg.from_user.id)
    if not u:
        cur.execute(
            "INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)",
            (msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or ""),
        )
        conn.commit()

def is_admin(tg_id: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,))
    return cur.fetchone() is not None

async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"member", "administrator", "creator"}
    except TelegramBadRequest:
        return False

def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 Задания", callback_data="tasks")
    kb.button(text="👤 Профиль", callback_data="profile")
    kb.button(text="💳 Вывод", callback_data="withdraw")
    kb.button(text="❓ Помощь", callback_data="help")
    kb.adjust(2, 2)
    return kb.as_markup()

def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]
    )

def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in [
        ("📊 Статистика", "a_stats"),
        ("📢 Рассылка", "a_bcast"),
        ("📌 Спонсоры", "a_sponsors"),
        ("🧩 Задания", "a_tasks"),
        ("💳 Выводы", "a_withdraws"),
        ("👥 Пользователи", "a_users"),
    ]:
        kb.button(text=text, callback_data=data)
    kb.adjust(2, 2, 2)
    return kb.as_markup()

async def sponsor_check_kb() -> InlineKeyboardMarkup:
    cur.execute("SELECT * FROM sponsors WHERE active=1")
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        if r["username"]:
            kb.button(text=f"📢 {r['title'] or r['username']}", url=f"https://t.me/{r['username']}")
    kb.button(text="✅ Проверить подписку", callback_data="check_sponsors")
    return kb.as_markup()

async def require_sponsor_membership(bot: Bot, user_id: int) -> bool:
    cur.execute("SELECT chat_id FROM sponsors WHERE active=1")
    for row in cur.fetchall():
        if not await is_member(bot, row["chat_id"], user_id):
            return False
    return True

# ====================
# FSM
# ====================
class WithdrawFSM(StatesGroup):
    amount = State()
    account = State()

class BroadcastFSM(StatesGroup):
    text = State()

class AddSponsorFSM(StatesGroup):
    username_or_id = State()

class AddTaskFSM(StatesGroup):
    title = State()
    reward = State()
    channel = State()

class UserEditFSM(StatesGroup):
    target = State()
    delta = State()

# ====================
# ROUTER
# ====================
router = Router()

@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    ensure_user(message)
    if get_user(message.from_user.id)["is_banned"]:
        await message.answer("⛔️ Вы заблокированы.")
        return

    if not await require_sponsor_membership(bot, message.from_user.id):
        await message.answer(
            "Чтобы начать, подпишитесь на спонсоров и нажмите «Проверить подписку».",
            reply_markup=await sponsor_check_kb(),
        )
        return

    await message.answer(
        "Привет! Это бот заданий за Gold. Выберите действие:",
        reply_markup=main_menu_kb(),
    )

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await cb.answer()

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_kb())

@router.callback_query(F.data == "admin")
async def admin_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Админ-панель:", reply_markup=admin_kb())
    await cb.answer()

# ====================
# HTTP server for Render
# ====================
async def start_http_server():
    async def ok(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ====================
# APP
# ====================
async def main():
    bot = Bot(BOT_TOKEN, parse_mode=None)
    dp = Dispatcher()
    dp.include_router(router)
    await asyncio.gather(
        start_http_server(),
        dp.start_polling(bot),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
