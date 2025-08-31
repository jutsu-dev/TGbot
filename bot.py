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
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ====================
# ENV (Render → Environment)
# ====================
BOT_TOKEN   = os.getenv("BOT_TOKEN")
OWNER_ID    = int(os.getenv("OWNER_ID", "0"))
MIN_WITHDRAW= int(os.getenv("MIN_WITHDRAW", "100"))
BASE_URL    = os.getenv("BASE_URL")   # например: https://tgbot-xxxx.onrender.com
WEBHOOK_PATH= os.getenv("WEBHOOK_PATH", "/webhook")  # можно изменить при желании

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не найден! (Render → Environment)")
if not BASE_URL:
    raise RuntimeError("❌ BASE_URL не задан! Укажи свой Primary URL Render (например https://tgbot-xxxx.onrender.com)")

logging.basicConfig(level=logging.INFO)

# ====================
# DB (на Free — эпемерная)
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
        type TEXT NOT NULL, -- 'subscribe'
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
        status TEXT DEFAULT 'pending', -- pending/approved/rejected
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

async def sponsor_check_kb() -> InlineKeyboardMarkup:
    cur.execute("SELECT * FROM sponsors WHERE active=1")
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        if r["username"]:
            kb.button(text=f"📢 {r['title'] or r['username']}", url=f"https://t.me/{r['username']}")
        else:
            kb.button(text=f"🔒 {r['title'] or r['chat_id']}", callback_data="noop")
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
        "привет! это бот заданий за Gold. выбирай, что дальше:",
        reply_markup=main_menu_kb(),
    )

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text("главное меню:", reply_markup=main_menu_kb())
    await cb.answer()

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    text = (
        "❓ Помощь\n\n"
        "1) Подпишись на спонсоров → «Проверить подписку».\n"
        "2) Открой «Задания», жми «Выполнить» → «Проверить».\n"
        "3) За выполненное задание Gold зачислятся на баланс.\n"
        f"4) Минималка на вывод: {MIN_WITHDRAW} Gold.\n\n"
        "Если канал приватный — бот должен быть админом там (иначе не увидит подписку)."
    )
    await cb.message.edit_text(text, reply_markup=back_menu_kb())
    await cb.answer()

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
    await cb.message.edit_text(text, reply_markup=back_menu_kb())
    await cb.answer()

@router.callback_query(F.data == "tasks")
async def cb_tasks(cb: CallbackQuery, bot: Bot):
    if not await require_sponsor_membership(bot, cb.from_user.id):
        await cb.message.edit_text(
            "Подпишитесь на спонсоров, чтобы открыть задания:",
            reply_markup=await sponsor_check_kb(),
        )
        await cb.answer()
        return

    cur.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id DESC")
    rows = cur.fetchall()
    if not rows:
        await cb.message.edit_text("Пока нет активных заданий.", reply_markup=back_menu_kb())
        await cb.answer()
        return

    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"➕ {r['title']} (+{r['reward']} Gold)", callback_data=f"task:{r['id']}")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text("Выбери задание:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("task:"))
async def cb_task_open(cb: CallbackQuery):
    task_id = int(cb.data.split(":")[1])
    cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    t = cur.fetchone()
    if not t or not t["active"]:
        await cb.answer("Задание недоступно", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    if t["type"] == "subscribe":
        if t["url"]:
            kb.button(text="🔗 Открыть канал", url=t["url"])
        kb.button(text="✅ Проверить", callback_data=f"task_check:{task_id}")
    kb.button(text="⬅️ Назад", callback_data="tasks")

    text = f"📌 {t['title']}\n\n{t['description'] or ''}\n\nНаграда: {t['reward']} Gold"
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("task_check:"))
async def cb_task_check(cb: CallbackQuery, bot: Bot):
    task_id = int(cb.data.split(":")[1])
    cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    t = cur.fetchone()
    if not t:
        await cb.answer("Задание не найдено", show_alert=True)
        return

    ok = await is_member(bot, t["target_chat_id"], cb.from_user.id)
    if not ok:
        await cb.answer("Подписка не обнаружена. Убедись, что вступил.", show_alert=True)
        return

    cur.execute(
        "SELECT id, status FROM user_tasks WHERE user_id=(SELECT id FROM users WHERE tg_id=?) AND task_id=?",
        (cb.from_user.id, task_id),
    )
    ut = cur.fetchone()
    if ut and ut["status"] == "done":
        await cb.answer("Это задание уже зачтено", show_alert=True)
        return

    cur.execute("SELECT id FROM users WHERE tg_id=?", (cb.from_user.id,))
    uid = cur.fetchone()[0]
    cur.execute("INSERT OR IGNORE INTO user_tasks (user_id, task_id, status) VALUES (?, ?, 'new')",
                (uid, task_id))
    cur.execute("UPDATE user_tasks SET status='done', checked_at=? WHERE user_id=? AND task_id=?",
                (datetime.utcnow().isoformat(), uid, task_id))
    cur.execute("UPDATE users SET balance = balance + ?, completed_tasks = completed_tasks + 1 WHERE id=?",
                (t["reward"], uid))
    conn.commit()

    await cb.answer("Готово! Награда начислена.", show_alert=True)
    await cb.message.edit_text("✅ Задание выполнено и оплачено.", reply_markup=back_menu_kb())

@router.callback_query(F.data == "withdraw")
async def cb_withdraw(cb: CallbackQuery, state: FSMContext):
    u = get_user(cb.from_user.id)
    if u["balance"] < MIN_WITHDRAW:
        await cb.answer(f"Минимум к выводу {MIN_WITHDRAW} Gold", show_alert=True)
        return
    await state.set_state(WithdrawFSM.amount)
    await cb.message.edit_text(f"Сколько Gold вывести? (от {MIN_WITHDRAW})\nНапиши число:",
                               reply_markup=back_menu_kb())
    await cb.answer()

@router.message(WithdrawFSM.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except Exception:
        await message.answer("Введи число, например 150")
        return
    u = get_user(message.from_user.id)
    if amount < MIN_WITHDRAW or amount > u["balance"]:
        await message.answer("Неверная сумма. Проверь баланс/минималку.")
        return
    await state.update_data(amount=amount)
    await state.set_state(WithdrawFSM.account)
    await message.answer("Введи ID/ник Standoff2 для перевода Gold:")

@router.message(WithdrawFSM.account)
async def withdraw_account(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount = data["amount"]
    account = message.text.strip()

    cur.execute("SELECT id FROM users WHERE tg_id=?", (message.from_user.id,))
    uid = cur.fetchone()[0]
    cur.execute("INSERT INTO withdrawals (user_id, amount, game_account) VALUES (?, ?, ?)",
                (uid, amount, account))
    cur.execute("UPDATE users SET balance = balance - ? WHERE id=?", (amount, uid))
    conn.commit()

    await state.clear()
    await message.answer("✅ Заявка на вывод создана. Ожидайте подтверждения.")

    try:
        text = ( "🧾 Новая заявка на вывод\n\n"
                 f"User: @{message.from_user.username or message.from_user.id} ({message.from_user.id})\n"
                 f"Сумма: {amount} Gold\n"
                 f"Аккаунт: {account}" )
        if OWNER_ID:
            await bot.send_message(OWNER_ID, text)
    except Exception:
        pass

@router.callback_query(F.data == "check_sponsors")
async def cb_check_sponsors(cb: CallbackQuery, bot: Bot):
    ok = await require_sponsor_membership(bot, cb.from_user.id)
    if ok:
        await cb.message.edit_text("Спасибо за подписку! Меню:", reply_markup=main_menu_kb())
    else:
        await cb.answer("Ещё не все подписки найдены.", show_alert=True)

# --- ADMIN (как раньше) ---
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
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
    await message.answer("Админ-панель:", reply_markup=kb.as_markup())

# ... (все админ-хэндлеры из твоей версии остаются — я их включил в предыдущих шагах;
# ради компактности здесь опущены, но их можно оставить без изменений.
# Если ты уже вставил полный вариант ранее — ничего дополнительно менять не нужно.)

# ====================
# WEBHOOK SERVER (без polling)
# ====================
async def create_app() -> web.Application:
    bot = Bot(BOT_TOKEN, parse_mode=None)
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()

    # healthcheck
    async def ok(_):
        return web.Response(text="ok")
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)

    # регистрация webhook-роутера
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # при старте — ставим webhook, при остановке — удаляем
    async def on_startup(app: web.Application):
        url = f"{BASE_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(url)
        logging.info(f"Set webhook to {url}")

    async def on_cleanup(app: web.Application):
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook deleted")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

def main():
    port = int(os.getenv("PORT", "10000"))
    app = create_app()
    web.run_app(asyncio.get_event_loop().run_until_complete(app),
                host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

