import os
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# =========================
# CONFIG
# =========================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")  # https://tgbot-xxxx.onrender.com
WEBHOOK_PATH = "/webhook"

if not BOT_TOKEN or not BASE_URL:
    raise RuntimeError("❌ BOT_TOKEN и BASE_URL должны быть заданы в Environment!")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()

# =========================
# HANDLERS
# =========================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("✅ Бот работает через webhook 🚀")

dp.include_router(router)

# =========================
# WEBHOOK SETUP
# =========================
async def on_startup(app: web.Application):
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logging.info(f"🚀 Webhook установлен: {webhook_url}")

async def on_cleanup(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🛑 Webhook удалён")

def main():
    app = web.Application()

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
