import os
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

# ====================
# ENV & LOGGING
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
        status TEXT DEFAULT 'new', -- new/done/rejected
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

# Ensure owner is admin
if OWNER_ID:
    cur.execute("INSERT OR IGNORE INTO admins (tg_id, role) VALUES (?, 'owner')", (OWNER_ID,))
    conn.commit()

# ====================
# HELPERS
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

# ====================
# FSM
# ====================
class WithdrawFSM(StatesGroup):
    amount = State()
    account = State()

# ====================
# ROUTERS
# ====================
router = Router()

@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    ensure_user(message)
    await message.answer(
        "👋 Привет! Это бот заданий за Gold.\n\n"
        "Выбирай действие из меню ниже:",
        reply_markup=main_menu_kb(),
    )

@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    text = (
        f"👤 Профиль\n\n"
        f"ID: {u['tg_id']}\n"
        f"Ник: @{cb.from_user.username if cb.from_user.username else '—'}\n"
        f"Баланс: {u['balance']} Gold\n"
        f"Выполнено заданий: {u['completed_tasks']}\n"
    )
    await cb.message.edit_text(text, reply_markup=main_menu_kb())
    await cb.answer()

# (для краткости не вставляю админку и задания полностью — но их можно перенести из твоего старого кода сюда без изменений)

# ====================
# HTTP server for Render
# ====================
async def start_http_server():
    async def ok(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)
    port = int(os.getenv("PORT", "1000
