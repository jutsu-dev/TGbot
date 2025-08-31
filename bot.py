import os
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")  # https://tgbot-xxxx.onrender.com
WEBHOOK_PATH = "/webhook"

if not BOT_TOKEN or not BASE_URL:
    raise RuntimeError("BOT_TOKEN или BASE_URL не заданы в Environment")

bot = Bot(BOT_TOKEN, parse_mode=None)
dp = Dispatcher()
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("✅ Привет! Бот работает через webhook (aiogram v3) 🚀")

dp.include_router(router)

async def on_startup(app: web.Application):
    await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}")
    logging.info(f"Webhook установлен: {BASE_URL}{WEBHOOK_PATH}")

async def on_cleanup(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Webhook удалён")

def main():
    app = web.Application()
    # Подключаем aiogram к aiohttp
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()



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


