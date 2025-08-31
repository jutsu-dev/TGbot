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
# ENV & LOGGING (Render -> Environment Variables)
# ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "100"))
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не найден! Добавь переменные в Render → Environment")

logging.basicConfig(level=logging.INFO)

# ====================
# DB (эпемерная на Free тарифе)
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

# Ensure owner is admin
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
            # приватный канал без username — просто напоминание
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

    text = (
        f"📌 {t['title']}\n\n"
        f"{t['description'] or ''}\n\n"
        f"Награда: {t['reward']} Gold"
    )
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
    cur.execute(
        "INSERT OR IGNORE INTO user_tasks (user_id, task_id, status) VALUES (?, ?, 'new')",
        (uid, task_id),
    )
    cur.execute(
        "UPDATE user_tasks SET status='done', checked_at=? WHERE user_id=? AND task_id=?",
        (datetime.utcnow().isoformat(), uid, task_id),
    )
    cur.execute(
        "UPDATE users SET balance = balance + ?, completed_tasks = completed_tasks + 1 WHERE id=?",
        (t["reward"], uid),
    )
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
    await cb.message.edit_text(
        f"Сколько Gold вывести? (от {MIN_WITHDRAW})\nНапиши число:",
        reply_markup=back_menu_kb(),
    )
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
    cur.execute(
        "INSERT INTO withdrawals (user_id, amount, game_account) VALUES (?, ?, ?)",
        (uid, amount, account),
    )
    cur.execute("UPDATE users SET balance = balance - ? WHERE id=?", (amount, uid))
    conn.commit()

    await state.clear()
    await message.answer("✅ Заявка на вывод создана. Ожидайте подтверждения.")

    try:
        text = (
            "🧾 Новая заявка на вывод\n\n"
            f"User: @{message.from_user.username or message.from_user.id} ({message.from_user.id})\n"
            f"Сумма: {amount} Gold\n"
            f"Аккаунт: {account}"
        )
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

# ====================
# ADMIN
# ====================
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

@router.callback_query(F.data == "a_stats")
async def a_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT COUNT(*) c FROM users")
    users_cnt = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM users WHERE strftime('%Y-%m-%d', joined_at)=strftime('%Y-%m-%d','now')")
    users_today = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'")
    wd_pending = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(reward),0) s FROM tasks t JOIN user_tasks ut ON ut.task_id=t.id WHERE ut.status='done'")
    paid_total = cur.fetchone()["s"]
    text = (
        "📊 Статистика\n\n"
        f"Пользователей: {users_cnt} (+{users_today} сегодня)\n"
        f"Выплаты в Gold (начислено): {paid_total}\n"
        f"Заявок на вывод (ожидают): {wd_pending}"
    )
    await cb.message.edit_text(text)
    await cb.answer()

@router.callback_query(F.data == "a_bcast")
async def a_bcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(BroadcastFSM.text)
    await cb.message.edit_text("Введи текст рассылки (без форматирования):")
    await cb.answer()

@router.message(BroadcastFSM.text)
async def a_bcast_go(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    cur.execute("SELECT tg_id FROM users")
    ids = [row[0] for row in cur.fetchall()]
    sent, fail = 0, 0
    for uid in ids:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.03)
    await message.answer(f"Готово. Отправлено: {sent}, ошибок: {fail}")

@router.callback_query(F.data == "a_sponsors")
async def a_sponsors(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT * FROM sponsors ORDER BY id DESC")
    rows = cur.fetchall()
    lines = ["📌 Спонсоры (активные отмечены ✅):\n"]
    for r in rows:
        lines.append(f"{r['id']}. {r['title'] or r['username'] or r['chat_id']} {'✅' if r['active'] else '❌'}")
    text = "\n".join(lines) or "Пусто"

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data="a_sp_add")
    kb.button(text="♻️ Переключить", callback_data="a_sp_toggle")
    kb.button(text="🗑 Удалить", callback_data="a_sp_del")
    kb.button(text="⬅️ Назад", callback_data="admin")
    kb.adjust(2, 2)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "a_sp_add")
async def a_sp_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AddSponsorFSM.username_or_id)
    await cb.message.edit_text("Введи @username или numeric ID канала/чата (бот должен иметь доступ):")
    await cb.answer()

@router.message(AddSponsorFSM.username_or_id)
async def a_sp_add_go(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    chat_id = raw
    if not raw.startswith("@"):
        try:
            chat_id = int(raw)
        except Exception:
            await message.answer("Не понял ID/username, попробуй ещё раз.")
            return
    try:
        chat = await bot.get_chat(chat_id)
        username = chat.username or None
        title = chat.title or None
        real_id = chat.id
        cur.execute(
            "INSERT OR REPLACE INTO sponsors (chat_id, username, title, active) VALUES (?, ?, ?, 1)",
            (real_id, username, title),
        )
        conn.commit()
        await message.answer(f"Добавлен спонсор: {title or username or real_id}")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()

@router.callback_query(F.data == "a_sp_toggle")
async def a_sp_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT id, title, username, active FROM sponsors ORDER BY id DESC")
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['id']}: {r['title'] or r['username']} ({'✅' if r['active'] else '❌'})", callback_data=f"a_sp_t:{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="a_sponsors")
    kb.adjust(1)
    await cb.message.edit_text("Выбери спонсора для переключения:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("a_sp_t:"))
async def a_sp_tog_one(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    sp_id = int(cb.data.split(":")[1])
    cur.execute("UPDATE sponsors SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (sp_id,))
    conn.commit()
    await cb.answer("Готово")
    await a_sponsors(cb)

@router.callback_query(F.data == "a_sp_del")
async def a_sp_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT id, title, username FROM sponsors ORDER BY id DESC")
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 {r['id']}: {r['title'] or r['username']}", callback_data=f"a_sp_d:{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="a_sponsors")
    kb.adjust(1)
    await cb.message.edit_text("Выбери, кого удалить:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("a_sp_d:"))
async def a_sp_del_one(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    sp_id = int(cb.data.split(":")[1])
    cur.execute("DELETE FROM sponsors WHERE id=?", (sp_id,))
    conn.commit()
    await cb.answer("Удалено")
    await a_sponsors(cb)

@router.callback_query(F.data == "a_tasks")
async def a_tasks(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()
    lines = ["🧩 Задания:\n"]
    for r in rows:
        lines.append(f"{r['id']}. {r['title']} +{r['reward']} Gold ({'✅' if r['active'] else '❌'})")
    text = "\n".join(lines) or "Пусто"

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data="a_t_add")
    kb.button(text="♻️ Переключить", callback_data="a_t_toggle")
    kb.button(text="⬅️ Назад", callback_data="admin")
    kb.adjust(2, 1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "a_t_add")
async def a_t_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AddTaskFSM.title)
    await cb.message.edit_text("Название задания (подписка):")
    await cb.answer()

@router.message(AddTaskFSM.title)
async def a_t_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddTaskFSM.reward)
    await message.answer("Награда в Gold (число):")

@router.message(AddTaskFSM.reward)
async def a_t_add_reward(message: Message, state: FSMContext):
    try:
        reward = int(message.text.strip())
    except Exception:
        await message.answer("Введи число, например 50")
        return
    await state.update_data(reward=reward)
    await state.set_state(AddTaskFSM.channel)
    await message.answer("Канал: @username или numeric ID (бот должен иметь доступ):")

@router.message(AddTaskFSM.channel)
async def a_t_add_channel(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    title = data["title"]
    reward = data["reward"]
    raw = message.text.strip()
    chat_id = raw
    if not raw.startswith("@"):
        try:
            chat_id = int(raw)
        except Exception:
            await message.answer("Не понял ID/username, попробуй ещё раз.")
            return
    try:
        chat = await bot.get_chat(chat_id)
        real_id = chat.id
        url = f"https://t.me/{chat.username}" if chat.username else None
        cur.execute(
            "INSERT INTO tasks (type, title, reward, target_chat_id, url, active) VALUES ('subscribe', ?, ?, ?, ?, 1)",
            (title, reward, real_id, url),
        )
        conn.commit()
        await message.answer("Задание создано ✅")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()

@router.callback_query(F.data == "a_t_toggle")
async def a_t_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute("SELECT id, title, active FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['id']}: {r['title']} ({'✅' if r['active'] else '❌'})", callback_data=f"a_t_t:{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="a_tasks")
    kb.adjust(1)
    await cb.message.edit_text("Выбери задание для переключения:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("a_t_t:"))
async def a_t_toggle_one(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    t_id = int(cb.data.split(":")[1])
    cur.execute("UPDATE tasks SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (t_id,))
    conn.commit()
    await cb.answer("Готово")
    await a_tasks(cb)

@router.callback_query(F.data == "a_withdraws")
async def a_withdraws(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur.execute(
        "SELECT w.id, u.tg_id, u.username, w.amount, w.game_account, w.status, w.created_at "
        "FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id"
    )
    rows = cur.fetchall()
    if not rows:
        await cb.message.edit_text("Нет ожидающих заявок.")
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    text_lines = ["Ожидают:\n"]
    for r in rows:
        text_lines.append(
            f"#{r['id']} — @{r['username'] or r['tg_id']} — {r['amount']} Gold → {r['game_account']}"
        )
        kb.button(text=f"✅ {r['id']}", callback_data=f"a_w_ok:{r['id']}")
        kb.button(text=f"❌ {r['id']}", callback_data=f"a_w_no:{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="admin")
    kb.adjust(2, 1)
    await cb.message.edit_text("\n".join(text_lines), reply_markup=kb.as_markup())
    await cb.answer()

async def _withdraw_notify(bot: Bot, w_id: int, status: str, comment: str | None):
    cur.execute(
        "SELECT w.id, u.tg_id, w.amount, w.game_account FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=?",
        (w_id,)
    )
    r = cur.fetchone()
    if not r:
        return
    text = (
        f"Ваша заявка #{w_id}: {status.upper()}\n"
        f"Сумма: {r['amount']} Gold\n"
        f"Аккаунт: {r['game_account']}\n"
        f"Комментарий: {comment or '—'}"
    )
    try:
        await bot.send_message(r["tg_id"], text)
    except Exception:
        pass

@router.callback_query(F.data.startswith("a_w_ok:"))
async def a_w_ok(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return
    w_id = int(cb.data.split(":")[1])
    cur.execute(
        "UPDATE withdrawals SET status='approved', processed_by=?, processed_at=?, comment='Выплачено' "
        "WHERE id=? AND status='pending'",
        (cb.from_user.id, datetime.utcnow().isoformat(), w_id),
    )
    conn.commit()
    await _withdraw_notify(bot, w_id, "approved", "Выплачено")
    await cb.answer("Одобрено")
    await a_withdraws(cb)

@router.callback_query(F.data.startswith("a_w_no:"))
async def a_w_no(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return
    w_id = int(cb.data.split(":")[1])
    cur.execute("SELECT user_id, amount FROM withdrawals WHERE id=? AND status='pending'", (w_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["amount"], row["user_id"]))
    cur.execute(
        "UPDATE withdrawals SET status='rejected', processed_by=?, processed_at=?, comment='Отказ' "
        "WHERE id=? AND status='pending'",
        (cb.from_user.id, datetime.utcnow().isoformat(), w_id),
    )
    conn.commit()
    await _withdraw_notify(bot, w_id, "rejected", "Отказ")
    await cb.answer("Отклонено")
    await a_withdraws(cb)

@router.callback_query(F.data == "a_users")
async def a_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🔨 Бан", callback_data="a_u_ban")
    kb.button(text="🧯 Разбан", callback_data="a_u_unban")
    kb.button(text="💰 Изменить баланс", callback_data="a_u_balance")
    kb.button(text="⬅️ Назад", callback_data="admin")
    kb.adjust(2, 2)
    await cb.message.edit_text("Управление пользователями:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.in_({"a_u_ban", "a_u_unban", "a_u_balance"}))
async def a_users_choose(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    action = cb.data
    await state.set_state(UserEditFSM.target)
    await state.update_data(action=action)
    await cb.message.edit_text("Введи целевой user_id (число):")
    await cb.answer()

@router.message(UserEditFSM.target)
async def a_users_target(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data["action"]
    try:
        uid = int(message.text.strip())
    except Exception:
        await message.answer("Нужен числовой user_id")
        return
    await state.update_data(uid=uid)
    if action == "a_u_balance":
        await state.set_state(UserEditFSM.delta)
        await message.answer("На сколько изменить баланс (можно -100 или +100):")
    else:
        cur.execute("UPDATE users SET is_banned=? WHERE tg_id=?", (1 if action == "a_u_ban" else 0, uid))
        conn.commit()
        await state.clear()
        await message.answer("Готово.")

@router.message(UserEditFSM.delta)
async def a_users_delta(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        delta = int(message.text.strip())
    except Exception:
        await message.answer("Нужна цифра, пример: -50 или 200")
        return
    uid = data["uid"]
    cur.execute("UPDATE users SET balance = balance + ? WHERE tg_id=?", (delta, uid))
    conn.commit()
    await state.clear()
    await message.answer("Готово.")

@router.callback_query(F.data == "admin")
async def admin_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await admin_panel(cb.message)
    await cb.answer()

# ====================
# HTTP server for Render (healthcheck)
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

