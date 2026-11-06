"""
Чат-бот управляющей компании для мессенджера MAX.
Функции: регистрация жильцов (телефон, ФИО, адрес), приём заявок с номером,
статус заявки по номеру, аварийные ситуации, информация по дому, контакты УК,
обновление статусов и публикация объявлений сотрудниками.

Запуск: python bot.py
Зависимости: pip install maxapi python-dotenv aiosqlite
"""

import asyncio
import logging
import os
import re
from datetime import datetime

import aiosqlite
from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.types import (
    Attachment, BotStarted, CallbackButton, Command,
    MessageCallback, MessageCreated, ButtonsPayload,
)
from maxapi.enums.intent import Intent

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ID сотрудников (админов), которые могут менять статусы и публиковать объявления.
ADMIN_IDS = {82205193, 6198981}

# ID рабочего чата УК для уведомлений о новых заявках
WORK_CHAT_ID = -80938009169161

CONTACTS = (
    "☎️ **Контакты управляющей компании**\n\n"
    "• Диспетчерская (9:00–17:00): 8 (928) 101-44-79\n"
    "• Аварийная служба: 8 (863) 303-20-03\n"
    "• Приёмная: 8 (928) 101-44-79\n"
    "• E-mail: strategy20-30@yandex.ru\n"
    "• Адрес офиса: г. Ростов-на-Дону, ул. Налбандяна 92/24, оф 1\n"
    "• Приёмные часы: пн–чт 9:00–17:00, пт 09:00–16:00"
)

# ---- База данных (асинхронная) ----
DB_PATH = "uk_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            fio TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            address TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Принята',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS emergencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS house_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        cursor = await conn.execute("SELECT COUNT(*) FROM emergencies")
        row = await cursor.fetchone()
        if row[0] == 0:
            await conn.executemany(
                "INSERT INTO emergencies (title, description) VALUES (?, ?)",
                [
                    ("Отключение горячей воды",
                     "09.11–15.11 с 9:00 до 18:00 будут отключены горячие вода "
                     "по адресам: ул. Примерная 1–10. Причина — гидравлические "
                     "испытания."),
                    ("Плановый ремонт лифта",
                     "До 12.11 лифт в подъезде № 3 дома по ул. Примерной, 1 не "
                     "работает. Ремонт окончен будет ориентировочно 12.11."),
                ],
            )
        await conn.commit()

# ---- Работа с жильцами ----
async def get_user(chat_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def save_user(chat_id, fio, phone, address):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users (chat_id, fio, phone, address, registered_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET fio=?, phone=?, address=?",
            (chat_id, fio, phone, address,
             datetime.now().strftime("%d.%m.%Y %H:%M"), fio, phone, address),
        )
        await conn.commit()

# ---- Уведомление в рабочий чат ----
async def notify_work_chat(req_id, category, address, description):
    """Отправляет уведомление о новой заявке в рабочий чат УК"""
    text = (
        f"🚨 **Новая заявка №{req_id:05d}**\n\n"
        f"🏠 Адрес: {address}\n"
        f"🗂 Категория: {category}\n"
        f"📝 Описание: {description}"
    )
    try:
        await bot.send_message(chat_id=WORK_CHAT_ID, text=text)
        logging.info(f"Уведомление о заявке №{req_id} отправлено в рабочий чат")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление в рабочий чат: {e}")

# ---- Заявки ----
async def create_request(chat_id, category, address, description):
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "INSERT INTO requests (chat_id, category, address, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, category, address, description,
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        await conn.commit()
        request_id = cursor.lastrowid
    
    asyncio.create_task(notify_work_chat(request_id, category, address, description))
    return request_id

async def get_request(number):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM requests WHERE id = ?", (number,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_recent_requests(limit=20):
    """Получить последние заявки для админ-панели"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def set_status(number, status):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, number))
        await conn.commit()

# ---- Аварии и информация по дому ----
async def get_active_emergencies():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM emergencies WHERE active = 1 ORDER BY id")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def add_house_info(title, text):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO house_info (title, text, created_at) VALUES (?, ?, ?)",
            (title, text, datetime.now().strftime("%d.%m.%Y")),
        )
        await conn.commit()

async def delete_house_info(info_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM house_info WHERE id = ?", (info_id,))
        await conn.commit()

async def get_house_info():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM house_info ORDER BY id DESC LIMIT 10")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

# ---- Состояния диалога ----
STATES = {}

STATUSES = ["Принята", "В работе", "Выполнена", "Отклонена"]

def is_valid_phone(text: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', text)
    return bool(re.match(r"^(\+7|8)\d{10}$", clean_phone))

NAME_RE = re.compile(r"^[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z\- ]{2,80}$")

# ---- Клавиатуры ----
def main_menu_kb(chat_id=None):
    """Главное меню. Для админов добавляется кнопка АДМ."""
    buttons = [
        [CallbackButton(text="📝 Оставить заявку", payload="new_request", intent=Intent.POSITIVE),
         CallbackButton(text="🔍 Статус заявки", payload="check_status", intent=Intent.DEFAULT)],
        [CallbackButton(text="🚨 Аварийные ситуации", payload="emergencies", intent=Intent.NEGATIVE),
         CallbackButton(text="🏠 Информация по дому", payload="house_info", intent=Intent.DEFAULT)],
        [CallbackButton(text="☎️ Контакты", payload="contacts", intent=Intent.DEFAULT),
         CallbackButton(text="👤 Профиль", payload="profile", intent=Intent.DEFAULT)],
    ]
    # Добавляем кнопку АДМ только для админов
    if chat_id in ADMIN_IDS:
        buttons.append([CallbackButton(text="⚙️ АДМ", payload="admin_menu", intent=Intent.DEFAULT)])
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=buttons))

def admin_menu_kb():
    """Меню администратора"""
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[
        [CallbackButton(text="📝 Изменить статус заявки", payload="admin_change_status", intent=Intent.DEFAULT)],
        [CallbackButton(text="📢 Опубликовать объявление", payload="admin_new_announcement", intent=Intent.DEFAULT)],
        [CallbackButton(text="📋 Список заявок", payload="admin_requests_list", intent=Intent.DEFAULT)],
        [CallbackButton(text="🗑 Удалить объявление", payload="admin_delete_announcement", intent=Intent.DEFAULT)],
        [CallbackButton(text="⬅️ В главное меню", payload="menu", intent=Intent.SECONDARY if hasattr(Intent, 'SECONDARY') else Intent.DEFAULT)],
    ]))

def status_kb(request_id):
    """Клавиатура со статусами для смены"""
    buttons = [[CallbackButton(text=s, payload=f"set_status_{request_id}_{s}", intent=Intent.DEFAULT)]
               for s in STATUSES]
    buttons.append([CallbackButton(text="❌ Отмена", payload="admin_menu", intent=Intent.DEFAULT)])
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=buttons))

def category_kb():
    buttons = [[CallbackButton(text=c, payload=f"cat_{c}", intent=Intent.DEFAULT)]
               for c in ["Электрика", "Сантехника", "Лифт", "Кровля и фасад", "Другое"]]
    buttons.append([CallbackButton(text="⬅️ В меню", payload="menu", intent=Intent.DEFAULT)])
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=buttons))

def back_kb():
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[[
        CallbackButton(text="⬅️ В меню", payload="menu", intent=Intent.DEFAULT),
    ]]))

def admin_back_kb():
    """Кнопка возврата в админ-меню"""
    return Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[[
        CallbackButton(text="⬅️ В админ-меню", payload="admin_menu", intent=Intent.DEFAULT),
    ]]))

def menu_text():
    return ("🏠 **УК «Стратегия 2030» — бот обслуживания**\n\n"
            "Выберите действие на кнопках или напишите:\n"
            "/start — меню\n/контакты — контакты УК\n"
            "/аварии — аварийные ситуации\n/инфо — объявления по дому")

def admin_menu_text():
    return ("⚙️ **Админ-панель**\n\n"
            "Выберите действие:")

async def send_menu(chat_id, text=None):
    await bot.send_message(chat_id=chat_id, text=text or menu_text(),
                           attachments=[main_menu_kb(chat_id)])

async def send_admin_menu(chat_id):
    await bot.send_message(chat_id=chat_id, text=admin_menu_text(),
                           attachments=[admin_menu_kb()])

async def send_emergencies(chat_id):
    items = await get_active_emergencies()
    if not items:
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Активных аварийных ситуаций нет. Работаем в штатном режиме.",
            attachments=[back_kb()],
        )
        return
    text = "🚨 **Аварийные и аварийно-предупредительные события:**\n\n" + "\n\n".join(
        f"⚠️ **{e['title']}**\n{e['description']}" for e in items
    )
    await bot.send_message(chat_id=chat_id, text=text, attachments=[back_kb()])

async def send_house_info(chat_id):
    items = await get_house_info()
    if not items:
        await bot.send_message(
            chat_id=chat_id,
            text="🏠 **Информация по дому**\n\nПока объявлений нет — следите за обновлениями.",
            attachments=[back_kb()],
        )
        return
    text = "🏠 **Информация по дому — актуальные объявления:**\n\n" + "\n\n".join(
        f"📌 **{i['title']}** ({i['created_at']})\n{i['text']}" for i in items
    )
    await bot.send_message(chat_id=chat_id, text=text, attachments=[back_kb()])

# ---- Профиль и регистрация ----
async def send_profile(chat_id):
    user = await get_user(chat_id)
    if not user:
        await bot.send_message(
            chat_id=chat_id,
            text="👤 Вы ещё не зарегистрированы.\n\n"
                 "Нажмите «Зарегистрироваться», чтобы оставлять заявки.",
            attachments=Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[[
                CallbackButton(text="✅ Зарегистрироваться", payload="register", intent=Intent.POSITIVE),
            ], [CallbackButton(text="⬅️ В меню", payload="menu", intent=Intent.DEFAULT)]])),
        )
        return
    await bot.send_message(
        chat_id=chat_id,
        text=f"👤 **Ваш профиль**\n\n"
             f"ФИО: {user['fio']}\nТелефон: {user['phone']}\n"
             f"Адрес: {user['address']}\nЗарегистрированы: {user['registered_at']}",
        attachments=Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[[
            CallbackButton(text="✏️ Изменить данные", payload="register", intent=Intent.DEFAULT),
        ], [CallbackButton(text="⬅️ В меню", payload="menu", intent=Intent.DEFAULT)]])),
    )

# ---- Хендлеры ----

@dp.bot_started()
async def on_started(event: BotStarted):
    await send_menu(event.chat_id)

@dp.message_created(Command("start"))
async def cmd_start(event: MessageCreated):
    chat_id = event.message.recipient.chat_id
    STATES.pop(chat_id, None)
    await send_menu(chat_id)

@dp.message_created(Command("контакты"))
async def cmd_contacts(event: MessageCreated):
    await bot.send_message(chat_id=event.message.recipient.chat_id,
                           text=CONTACTS, attachments=[back_kb()])

@dp.message_created(Command("аварии"))
async def cmd_emergencies(event: MessageCreated):
    await send_emergencies(event.message.recipient.chat_id)

@dp.message_created(Command("инфо"))
async def cmd_info(event: MessageCreated):
    await send_house_info(event.message.recipient.chat_id)

# Обработка текстовых сообщений
@dp.message_created()
async def router(event: MessageCreated):
    chat_id = event.message.recipient.chat_id
    text = (event.message.body.text or "").strip()

    if text.startswith("/"):
        await send_menu(chat_id)
        return

    # --- Машина состояний ---
    state = STATES.get(chat_id)
    if state:
        kind, data = state

        # --- Регистрация ---
        if kind == "reg_phone":
            if not is_valid_phone(text):
                await bot.send_message(chat_id=chat_id,
                    text="Номер не распознан. Пришлите в формате +7 900 123-45-67:")
                return
            STATES[chat_id] = ("reg_fio", {"phone": text})
            await bot.send_message(chat_id=chat_id,
                text="Укажите ФИО (например: Иванов Иван Иванович):")
            return
        if kind == "reg_fio":
            if not NAME_RE.match(text):
                await bot.send_message(chat_id=chat_id,
                    text="ФИО не распознано. Укажите полные фамилию, имя и отчество:")
                return
            STATES[chat_id] = ("reg_address", {**data, "fio": text})
            await bot.send_message(chat_id=chat_id,
                text="Укажите адрес (например: ул. Примерная, 1, под. 2, кв. 15):")
            return
        if kind == "reg_address":
            await save_user(chat_id, data["fio"], data["phone"], text)
            STATES.pop(chat_id, None)
            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ **Регистрация завершена!**\n\n"
                     f"ФИО: {data['fio']}\nТелефон: {data['phone']}\nАдрес: {text}\n\n"
                     f"Теперь вы можете оставлять заявки.",
                attachments=[main_menu_kb(chat_id)],
            )
            return

        # --- Создание заявки ---
        if kind == "new_address":
            if text.lower().strip() == "по адресу профиля" and data.get("profile_address"):
                data["address"] = data["profile_address"]
            else:
                data["address"] = text
            STATES[chat_id] = ("new_text", data)
            await bot.send_message(chat_id=chat_id,
                text="Опишите проблему одним сообщением (что и где случилось):")
            return
        if kind == "new_text":
            user = await get_user(chat_id)
            number = await create_request(chat_id, data["category"], data["address"], text)
            STATES.pop(chat_id, None)
            await bot.send_message(chat_id=chat_id,
                text=f"✅ **Заявка принята!**\n\n"
                     f"Номер заявки: **№ {number:05d}**\n"
                     f"Категория: {data['category']}\nАдрес: {data['address']}\n"
                     f"Заявитель: {user['fio']}\n\n"
                     f"По номеру заявки вы всегда можете узнать её статус.",
                attachments=[main_menu_kb(chat_id)])
            return
        if kind == "check_number":
            STATES.pop(chat_id, None)
            m2 = re.match(r"^(\d{1,7})$", text.replace("№", "").strip())
            if not m2:
                await bot.send_message(chat_id=chat_id,
                    text="Не понял номер. Пришлите, пожалуйста, просто цифры, "
                         "например: 00012", attachments=[back_kb()])
                return
            req = await get_request(int(m2.group(1)))
            if not req:
                await bot.send_message(chat_id=chat_id,
                    text=f"Заявки №{m2.group(1)} не найдено. Проверьте номер.",
                    attachments=[back_kb()])
            else:
                await bot.send_message(chat_id=chat_id,
                    text=f"🔍 **Заявка №{req['id']:05d}**\n"
                         f"Категория: {req['category']}\n"
                         f"Адрес: {req['address']}\n"
                         f"Создана: {req['created_at']}\n"
                         f"Статус: **{req['status']}**",
                    attachments=[back_kb()])
            return

        # --- АДМИН: ввод номера заявки для смены статуса ---
        if kind == "admin_wait_request_number":
            m2 = re.match(r"^(\d{1,7})$", text.replace("№", "").strip())
            if not m2:
                await bot.send_message(chat_id=chat_id,
                    text="Не понял номер. Пришлите просто цифры, например: 12\n"
                         "Или нажмите «❌ Отмена».",
                    attachments=[admin_back_kb()])
                return
            req = await get_request(int(m2.group(1)))
            if not req:
                await bot.send_message(chat_id=chat_id,
                    text=f"Заявки №{m2.group(1)} не найдено. Проверьте номер.\n\n"
                         f"Используйте «📋 Список заявок», чтобы увидеть актуальные номера.",
                    attachments=[admin_back_kb()])
                return
            STATES.pop(chat_id, None)
            await bot.send_message(chat_id=chat_id,
                text=f"📝 **Заявка №{req['id']:05d}**\n"
                     f"Категория: {req['category']}\n"
                     f"Адрес: {req['address']}\n"
                     f"Создана: {req['created_at']}\n"
                     f"Текущий статус: **{req['status']}**\n\n"
                     f"Выберите новый статус:",
                attachments=[status_kb(req['id'])])
            return

        # --- АДМИН: ввод заголовка объявления ---
        if kind == "admin_wait_announcement_title":
            if len(text) < 3 or len(text) > 100:
                await bot.send_message(chat_id=chat_id,
                    text="Заголовок должен быть от 3 до 100 символов. Попробуйте ещё раз:",
                    attachments=[admin_back_kb()])
                return
            STATES[chat_id] = ("admin_wait_announcement_text", {"title": text})
            await bot.send_message(chat_id=chat_id,
                text=f"✅ Заголовок: **{text}**\n\n"
                     f"Теперь пришлите текст объявления (можно несколько абзацев):")
            return

        # --- АДМИН: ввод текста объявления ---
        if kind == "admin_wait_announcement_text":
            title = data["title"]
            await add_house_info(title, text)
            STATES.pop(chat_id, None)
            await bot.send_message(chat_id=chat_id,
                text=f"✅ **Объявление опубликовано!**\n\n"
                     f"📌 Заголовок: {title}\n"
                     f"📝 Текст: {text}\n\n"
                     f"Жильцы увидят его в разделе «Информация по дому».",
                attachments=[admin_menu_kb()])
            return

    await send_menu(chat_id, "Не понял сообщение 🙂 Выберите действие на кнопке:")

# Callback-события от кнопок
@dp.message_callback()
async def on_callback(event: MessageCallback):
    try:
        payload = event.callback.payload
        callback_id = event.callback.callback_id
        chat_id = event.chat.chat_id if hasattr(event.chat, 'chat_id') else event.chat.id

        logging.info(f"Callback: payload={payload}, chat_id={chat_id}")

        async def answer(notification=""):
            if notification:
                await bot.send_callback(callback_id=callback_id, notification=notification)

        # --- Главное меню ---
        if payload == "menu":
            STATES.pop(chat_id, None)
            await answer()
            await send_menu(chat_id)
        elif payload == "profile":
            await answer()
            await send_profile(chat_id)
        elif payload == "register":
            await answer()
            STATES[chat_id] = ("reg_phone", {})
            await bot.send_message(chat_id=chat_id,
                text="📋 **Регистрация**\n\n"
                     "Пришлите ваш номер телефона в формате +7 900 123-45-67.")
        elif payload == "new_request":
            await answer()
            if not await get_user(chat_id):
                STATES[chat_id] = ("reg_phone", {})
                await bot.send_message(chat_id=chat_id,
                    text="Чтобы оставить заявку, сначала пройдите регистрацию.\n\n"
                         "📋 Пришлите ваш номер телефона в формате +7 900 123-45-67.")
                return
            await bot.send_message(chat_id=chat_id,
                text="Выберите категорию заявки:", attachments=[category_kb()])
        elif payload.startswith("cat_"):
            category = payload[4:]
            await answer()
            user = await get_user(chat_id)
            STATES[chat_id] = ("new_address", {
                "category": category,
                "profile_address": user["address"] if user else None,
            })
            hint = ""
            if user:
                hint = (f"\n\nАдрес из вашего профиля: {user['address']}.\n"
                        f"Напишите «по адресу профиля», чтобы использовать его, "
                        f"или пришлите другой адрес.")
            await bot.send_message(chat_id=chat_id,
                text=f"Укажите адрес и подъезд/квартиру "
                     f"(например: ул. Примерная, 1, под. 2, кв. 15){hint}:")
        elif payload == "check_status":
            await answer()
            STATES[chat_id] = ("check_number", {})
            await bot.send_message(chat_id=chat_id,
                text="Пришлите номер заявки цифрами, например: 00012",
                attachments=[admin_back_kb() if chat_id in ADMIN_IDS else back_kb()])
        elif payload == "emergencies":
            await answer()
            await send_emergencies(chat_id)
        elif payload == "house_info":
            await answer()
            await send_house_info(chat_id)
        elif payload == "contacts":
            await answer()
            await bot.send_message(chat_id=chat_id, text=CONTACTS, attachments=[back_kb()])

        # --- АДМИН-МЕНЮ ---
        elif payload == "admin_menu":
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            STATES.pop(chat_id, None)
            await send_admin_menu(chat_id)

        elif payload == "admin_change_status":
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            STATES[chat_id] = ("admin_wait_request_number", {})
            await bot.send_message(chat_id=chat_id,
                text="📝 **Смена статуса заявки**\n\n"
                     "Пришлите номер заявки цифрами (например: 12).\n"
                     "Или нажмите «📋 Список заявок», чтобы увидеть актуальные номера.",
                attachments=[Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[
                    [CallbackButton(text="📋 Список заявок", payload="admin_requests_list", intent=Intent.DEFAULT)],
                    [CallbackButton(text="❌ Отмена", payload="admin_menu", intent=Intent.DEFAULT)],
                ]))])

        elif payload == "admin_requests_list":
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            requests_list = await get_recent_requests(20)
            if not requests_list:
                await bot.send_message(chat_id=chat_id,
                    text="📋 Заявок пока нет.",
                    attachments=[admin_menu_kb()])
                return
            text = "📋 **Последние заявки (до 20):**\n\n"
            for r in requests_list:
                text += (f"№ **{r['id']:05d}** | {r['status']} | {r['category']}\n"
                         f"   🏠 {r['address']}\n"
                         f"   📅 {r['created_at']}\n\n")
            text += "Чтобы сменить статус, нажмите кнопку ниже и пришлите номер заявки."
            await bot.send_message(chat_id=chat_id, text=text,
                attachments=[Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=[
                    [CallbackButton(text="📝 Сменить статус заявки", payload="admin_change_status", intent=Intent.DEFAULT)],
                    [CallbackButton(text="⬅️ В админ-меню", payload="admin_menu", intent=Intent.DEFAULT)],
                ]))])

        elif payload == "admin_new_announcement":
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            STATES[chat_id] = ("admin_wait_announcement_title", {})
            await bot.send_message(chat_id=chat_id,
                text="📢 **Публикация объявления**\n\n"
                     "Пришлите заголовок объявления (от 3 до 100 символов):",
                attachments=[admin_back_kb()])

        elif payload == "admin_delete_announcement":
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            items = await get_house_info()
            if not items:
                await bot.send_message(chat_id=chat_id,
                    text="🗑 Объявлений для удаления нет.",
                    attachments=[admin_menu_kb()])
                return
            text = "🗑 **Удаление объявления**\n\nВыберите, какое объявление удалить:"
            buttons = [[CallbackButton(
                text=f"❌ {i['title'][:30]} ({i['created_at']})",
                payload=f"admin_confirm_del_{i['id']}",
                intent=Intent.NEGATIVE
            )] for i in items]
            buttons.append([CallbackButton(text="⬅️ Назад", payload="admin_menu", intent=Intent.DEFAULT)])
            await bot.send_message(chat_id=chat_id, text=text,
                attachments=[Attachment(type="inline_keyboard", payload=ButtonsPayload(buttons=buttons))])

        elif payload.startswith("admin_confirm_del_"):
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            try:
                info_id = int(payload.split("_")[-1])
            except (ValueError, IndexError):
                await bot.send_message(chat_id=chat_id, text="Ошибка ID.")
                return
            await delete_house_info(info_id)
            await bot.send_message(chat_id=chat_id,
                text=f"✅ Объявление №{info_id} удалено.",
                attachments=[admin_menu_kb()])

        # --- Смена статуса через кнопки ---
        elif payload.startswith("set_status_"):
            await answer()
            if chat_id not in ADMIN_IDS:
                await bot.send_message(chat_id=chat_id, text="⛔ Доступ запрещён.")
                return
            # Формат: set_status_{request_id}_{status}
            # Но статус может содержать пробелы ("В работе"), поэтому парсим аккуратно
            # Убираем префикс "set_status_"
            rest = payload[len("set_status_"):]
            # Находим первый символ '_', который разделяет id и статус
            sep_idx = rest.find("_")
            if sep_idx == -1:
                await bot.send_message(chat_id=chat_id, text="Ошибка формата.")
                return
            try:
                request_id = int(rest[:sep_idx])
                status = rest[sep_idx+1:]
            except ValueError:
                await bot.send_message(chat_id=chat_id, text="Ошибка ID.")
                return

            if status not in STATUSES:
                await bot.send_message(chat_id=chat_id,
                    text=f"Неизвестный статус: {status}")
                return
            req = await get_request(request_id)
            if not req:
                await bot.send_message(chat_id=chat_id,
                    text=f"Заявка №{request_id} не найдена.",
                    attachments=[admin_menu_kb()])
                return
            await set_status(request_id, status)
            await bot.send_message(chat_id=chat_id,
                text=f"✅ Заявка №{request_id:05d} → статус «**{status}**»")
            # Уведомляем жильца
            try:
                await bot.send_message(chat_id=req["chat_id"],
                    text=f"📌 Статус вашей заявки №{request_id:05d} обновлён: **{status}**")
            except Exception as e:
                logging.error(f"Не удалось уведомить жильца: {e}")
            await send_admin_menu(chat_id)

        else:
            await answer()
    except Exception as e:
        logging.error(f"Ошибка в обработчике callback: {e}")
        import traceback
        traceback.print_exc()

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())