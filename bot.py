import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
import sqlite3

# =========================
# НАСТРОЙКИ
# =========================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан. Укажи его в Render Environment.")

HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if not HOSTNAME:
    raise SystemExit("❌ RENDER_EXTERNAL_HOSTNAME не найден. Добавь его в Render Environment.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{HOSTNAME}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8080))

OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # админ id

# =========================
# FSM для вывода
# =========================
class WithdrawFSM(StatesGroup):
    amount = State()
    account = State()

# =========================
# DB (sqlite для примера)
# =========================
conn = sqlite3.connect("bot.db")
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    balance INTEGER DEFAULT 0
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    game_account TEXT
)""")
conn.commit()

def get_user(tg_id):
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    u = cur.fetchone()
    if not u:
        cur.execute("INSERT INTO users (tg_id, balance) VALUES (?, 0)", (tg_id,))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        u = cur.fetchone()
    return {"id": u[0], "tg_id": u[1], "balance": u[2]}

def is_admin(tg_id: int):
    return tg_id == OWNER_ID

# =========================
# БОТ
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =========================
# ХЭНДЛЕРЫ
# =========================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    u = get_user(message.from_user.id)
    await message.answer(f"✅ Привет! Твой баланс: {u['balance']} Gold\n\nКоманды:\n/withdraw — вывести\n/admin — админка")

# FSM: сумма
@dp.message(Command("withdraw"))
async def start_withdraw(message: Message, state: FSMContext):
    await state.set_state(WithdrawFSM.amount)
    await message.answer("Введи сумму для вывода:")

@dp.message(WithdrawFSM.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except Exception:
        await message.answer("Введи число, например 150")
        return
    u = get_user(message.from_user.id)
    MIN_WITHDRAW = 50
    if amount < MIN_WITHDRAW or amount > u["balance"]:
        await message.answer("❌ Неверная сумма. Проверь баланс/минималку.")
        return
    await state.update_data(amount=amount)
    await state.set_state(WithdrawFSM.account)
    await message.answer("Введи ID/ник Standoff2 для перевода Gold:")

@dp.message(WithdrawFSM.account)
async def withdraw_account(message: Message, state: FSMContext):
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

    if OWNER_ID:
        text = ( "🧾 Новая заявка на вывод\n\n"
                 f"User: @{message.from_user.username or message.from_user.id} ({message.from_user.id})\n"
                 f"Сумма: {amount} Gold\n"
                 f"Аккаунт: {account}" )
        await bot.send_message(OWNER_ID, text)

# Админ-панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    for text, data in [
        ("📊 Статистика", "a_stats"),
        ("📢 Рассылка", "a_bcast"),
        ("💳 Выводы", "a_withdraws"),
        ("👥 Пользователи", "a_users"),
    ]:
        kb.button(text=text, callback_data=data)
    kb.adjust(2, 2)
    await message.answer("Админ-панель:", reply_markup=kb.as_markup())

# =========================
# WEBHOOK SERVER
# =========================
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"🚀 Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    logging.info("🛑 Webhook удалён, сессия закрыта")

app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

# Healthcheck
async def healthcheck(_):
    return web.Response(text="ok")
app.router.add_get("/", healthcheck)

# Приём апдейтов
async def handle_webhook(request: web.Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update, bot=bot)
    return web.Response()
app.router.add_post(WEBHOOK_PATH, handle_webhook)

# MAIN
if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


