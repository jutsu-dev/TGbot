import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from aiohttp import web

# ====================
# ENV & LOGGING
# ====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "100"))
BASE_URL = os.getenv("BASE_URL")  # https://tgbot-xxxx.onrender.com

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не найден!")

# ====================
# DB INIT
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
    """
)
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

# ====================
# FSM
# ====================
class WithdrawFSM(StatesGroup):
    amount = State()
    account = State()

# ====================
# ROUTER
# ====================
router = Router()

@router.message(CommandStart())
async def start(message: Message):
    ensure_user(message)
    await message.answer("✅ Бот работает! Ты в главном меню.")

# ====================
# APP
# ====================
async def on_startup(bot: Bot):
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logging.info(f"🚀 Webhook установлен: {webhook_url}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logging.info("🛑 Webhook удалён, сессия закрыта")

async def handle_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return web.Response()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)

app = web.Application()
app.router.add_post("/webhook", handle_webhook)

async def main():
    app.on_startup.append(lambda _: on_startup(bot))
    app.on_shutdown.append(lambda _: on_shutdown(bot))
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()




