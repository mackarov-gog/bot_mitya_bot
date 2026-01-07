import asyncio
import httpx
import json
import random
import os
import re
import logging
import aiosqlite
import requests
import tempfile
from faster_whisper import WhisperModel
from datetime import datetime
from typing import Dict
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "mitya_data.db"

if not TOKEN:
    exit("Ошибка: токен не найден!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


STICKERS_TOXIC = [
    "CAACAgIAAxkBAAFAXAdpXr5wkEw5AAH0fqK1Loaiz1lDr6sAAsUqAALzN6hJao_y0kbm4mQ4BA"
]

STICKERS_POSITIVE = [
    "CAACAgIAAxkBAAFAXAdpXr5wkEw5AAH0fqK1Loaiz1lDr6sAAsUqAALzN6hJao_y0kbm4mQ4BA"
]

# --- WHISPER  ---
try:
    logging.info("Инициализация Faster-Whisper...")
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    logging.info("Whisper загружен!")
except Exception as e:
    logging.error(f"Ошибка загрузки Whisper: {e}")
    whisper_model = None

# --- Вспомогательные структуры ---
_chat_locks: Dict[int, asyncio.Lock] = {}


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock



# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Основная таблица
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                ai_enabled INTEGER DEFAULT 1,
                voice_enabled INTEGER DEFAULT 1,
                reply_chance INTEGER DEFAULT 0
            )
        ''')

        # Миграция: Проверяем, есть ли колонка reply_chance (для старых баз)
        try:
            await db.execute("ALTER TABLE chats ADD COLUMN reply_chance INTEGER DEFAULT 0")
            logging.info("База обновлена: добавлена колонка reply_chance")
        except Exception:
            pass  # Колонка уже есть или ALTER не поддерживается

        # Репутация
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                chat_id INTEGER,
                first_name TEXT,
                reputation INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')

        # Память
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Индексы для скорости
        await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_chat_user ON users(chat_id, user_id)')

        await db.commit()


async def get_chat_settings(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            async with db.execute(
                "SELECT ai_enabled, voice_enabled, reply_chance FROM chats WHERE chat_id = ?",
                (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"ai_enabled": row[0], "voice_enabled": row[1], "reply_chance": row[2]}
        except Exception as e:
            logging.exception(f"Ошибка чтения настроек: {e}")

        # Если чата нет или ошибка -> создаем дефолт
        try:
            await db.execute(
                "INSERT OR IGNORE INTO chats (chat_id, ai_enabled, voice_enabled, reply_chance) VALUES (?, 1, 1, 0)",
                (chat_id,)
            )
            await db.commit()
        except Exception:
            logging.exception("Ошибка при создании дефолтных настроек чата")

        return {"ai_enabled": 1, "voice_enabled": 1, "reply_chance": 0}


async def update_setting(chat_id, column, value):
    allowed_columns = ["ai_enabled", "voice_enabled", "reply_chance"]
    if column not in allowed_columns:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE chats SET {column} = ? WHERE chat_id = ?", (value, chat_id))
        await db.commit()


async def update_reputation(chat_id, user_id, name, change):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute('''
                INSERT INTO users (user_id, chat_id, first_name, reputation)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET 
                reputation = MAX(-150, MIN(150, reputation + ?)),
                first_name = ?
            ''', (user_id, chat_id, name, change, change, name))
            await db.commit()
        except Exception:
            logging.exception("Ошибка при обновлении репутации")


async def get_user_reputation(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT reputation FROM users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def save_context(chat_id, role, content, user_name=None):
    final_content = content
    if role == "user" and user_name:
        final_content = f"От пользователя {user_name}: {content}"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, final_content)
        )
        await db.execute('''
            DELETE FROM messages
            WHERE chat_id = ?
              AND id NOT IN (
                SELECT id FROM messages
                WHERE chat_id = ?
                ORDER BY timestamp DESC
                LIMIT 20
              )
        ''', (chat_id, chat_id))
        await db.commit()


async def get_context(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT role, content FROM messages 
            WHERE chat_id = ? 
            ORDER BY timestamp ASC LIMIT 15
        ''', (chat_id,)) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r[0], "content": r[1]} for r in rows]


# --- ФУНКЦИИ КОНТЕНТА ---
def get_joke():
    url = "https://randstuff.ru/joke/generate/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://randstuff.ru",
        "Referer": "https://randstuff.ru/joke/",
    }
    session = requests.Session()
    response = session.post(url, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()
    return data.get("joke", {}).get("text", "Шуток нет")


def get_cookies():
    url = "https://api.forismatic.com/api/1.0/?method=getQuote&format=json&lang=ru"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Ошибка get_cookies")
        return "Цитата недоступна"


def get_random_quote():
    try:
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'quotes_Statham.json')
        with open(file_path, 'r', encoding='utf-8') as f:
            quotes = json.load(f)
            quote_data = random.choice(quotes)
            return quote_data.get('text', "Текст не найден") if isinstance(quote_data, dict) else str(quote_data)
    except Exception:
        logging.exception("Ошибка чтения цитат")
        return "Цитаты временно закончились..."


def get_today_holiday():
    try:
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'holidays.json')
        today_date = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%m-%d")
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            holidays = data.get('holidays', [])
            for holiday in holidays:
                if holiday.get('date') == today_date:
                    return f"🎉 {holiday.get('name')}!\n{holiday.get('greeting')}"
        return None
    except Exception:
        logging.exception("Ошибка парсинга праздников")
        return None


# --- МОЗГИ (LLM) ---
async def check_toxicity_llm(text: str) -> int:
    url = "http://ollama:11434/api/generate"
    prompt = (
        "System: Ты — строгий и хладнокровный модератор чата.\n"
        "Проанализируй сообщение по смыслу, тону и направленности.\n\n"
        "Оцени ВЛИЯНИЕ сообщения на атмосферу диалога и верни ТОЛЬКО одно число:\n\n"
        "-5  — сильная агрессия: прямые оскорбления, унижение, угрозы, токсичный сарказм\n"
        "-1  — слабая агрессия: грубость, давление, пассивная агрессия, резкий тон\n"
        " 0  — нейтрально: обычный текст без эмоций\n"
        "+1  — лёгкий позитив: вежливость, спокойный тон, конструктив\n"
        "+5  — сильный позитив: благодарность, поддержка, уважение, похвала\n\n"
        "Правила:\n"
        "- Мат, направленный на человека → отрицательная оценка\n"
        "- Мат как междометие без агрессии → 0\n"
        "- Сарказм и ирония с уколом → отрицательная оценка\n"
        "- Отвечай СТРОГО одним числом: -5, -1, 0, 1 или 5\n\n"
        f"Message: {text}\n"
        "Answer:"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json={
                "model": "mitya-gemma",
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 5, "temperature": 0.0}
            })
            resp_json = response.json()
            raw_result = (resp_json.get("response") or "").strip()

            # Ищем первое число в ответе (модель может написать "Оценка: -5")
            match = re.search(r'-?\d+', raw_result)
            if match:
                return max(-5, min(5, int(match.group())))
            return 0
    except Exception as e:
            logging.error(f"Ошибка в check_toxicity_llm: {e}")
            return 0


REP_ADVICE = [
    (120, "Собеседник — Легенда Двора, твой старший брат. Впрягайся за него в любой кипиш, проявляй максимальное уважение и преданность."),
    (100, "Собеседник — Авторитет. Жми руку крепко, общайся с уважением, не перебивай."),
    (80,  "Собеседник — твой близкий кореш. Общайся по-братски, тепло, поддерживай, шути по-доброму."),
    (60,  "Собеседник — свой пацан. Нормальный диалог, можно расслабиться."),
    (40,  "Собеседник — ровный тип. Общайся спокойно, как с нормальным пацаном."),
    (10,  "Собеседник — просто прохожий. Общайся нормально, но без лишней любезности."),
    (0,   "Собеседник нейтральный. Отвечай коротко и по делу, в своем стиле."),
    (-10, "Собеседник — мутный тип. Держи ухо востро, отвечай с холодком."),
    (-40, "Собеседник — чушпан. Разговаривай свысока, можешь слегка подколоть или осадить."),
    (-60, "Собеседник — борзый. Не давай ему спуску, отвечай дерзко, ставь на место."),
    (-80, "Собеседник — черт. Наезжай на него, груби, поясняй, что он не прав. Никакого уважения."),
    (-100,"Собеседник — твой враг и крыса. Гноби его, унижай морально, смешивай с грязью. Никакой пощады."),
]

async def ask_mitya_ai(chat_id: int, user_text: str, user_id: int = None,
                     user_name: str = "Пацан", reply_to_text: str = None, is_auto: bool = False):
    # 1. Сохраняем с именем
    await save_context(chat_id, "user", user_text, user_name)

    # 2. Получаем историю
    history = await get_context(chat_id)


    if reply_to_text:
        if history:
            current_msg = history.pop()
            history.append({"role": "assistant", "content": f"(Контекст: ты отвечаешь на сообщение: {reply_to_text})"})
            history.append(current_msg)
        else:
            history.insert(0, {"role": "assistant", "content": f"(Контекст: ты отвечаешь на сообщение: {reply_to_text})"})



    for threshold, text in REP_ADVICE:
        if rep >= threshold:
            extra_info = text
            break

    # --- СИСТЕМНЫЙ ПРОМПТ (Оптимизирован для Gemma 1b) ---
    base_prompt = (
        f"Ты — Митя, дерзкий пацан. Сейчас говоришь с: {user_name}. "
        "Твои правила:\n"
        "1. Краткость (1-2 предложения).\n"
        "2. Сленг (слышь, ровно, от души).\n"
        "3. Не веди себя как робот.\n"
        "ИНСТРУКЦИЯ ПО ОТНОШЕНИЮ К ЧЕЛОВЕКУ: "
    )

    extra_info = "Относись нейтрально."
    try:
        if user_id is not None:
            rep = await get_user_reputation(chat_id, user_id)
            for threshold, text in REP_ADVICE:
                if rep >= threshold:
                    extra_info = text
                    break
    except Exception:
        pass

    if is_auto:
        extra_info += " Ты сам влез в разговор без спроса. Будь краток и остроумен."

    # Собираем финальный системный промпт
    full_system_prompt = base_prompt + extra_info

    # Вставляем системную инструкцию в начало истории для Ollama
    history.insert(0, {"role": "system", "content": full_system_prompt})

    payload = {
        "model": "mitya-gemma",
        "messages": history,
        "stream": False,
        "options": {
            "num_predict": 120,
            "temperature": 0.9
        }
    }

    # Ограничиваем параллельные вызовы к LLM для одного чата
    lock = get_chat_lock(chat_id)
    async with lock:
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                response = await client.post("http://ollama:11434/api/chat", json=payload)
                response.raise_for_status()
                resp_json = response.json()
                logging.debug(f"Ollama chat response: {resp_json}")

                # Ollama может возвращать разный формат: "message": {"content": "..." } или "response": "..."
                reply = ""
                if isinstance(resp_json, dict):
                    reply = (
                        (resp_json.get("message") or {}).get("content", "")
                        or resp_json.get("response", "")
                    )
                reply = (reply or "").strip()

                if reply:
                    await save_context(chat_id, "assistant", reply)
                    return reply
        except Exception:
            logging.exception("AI Error в ask_mitya_ai")

    return "Чет я притормозил, голова пустая..."


# --- МИДЛВЭР / УТИЛИТЫ ДЛЯ ХЕНДЛЕРОВ ---
def extract_sender_info(message: types.Message):
    """Возвращает безопасно (user_id, name, is_bot, username) учитывая sender_chat."""
    from_user = getattr(message, "from_user", None)
    if from_user:
        user_id = getattr(from_user, "id", None)
        name = getattr(from_user, "first_name", None) or "User"
        is_bot = bool(getattr(from_user, "is_bot", False))
        username = getattr(from_user, "username", None)
    else:
        # сообщение от sender_chat (канал)
        sender_chat = getattr(message, "sender_chat", None)
        user_id = getattr(sender_chat, "id", None)
        name = getattr(sender_chat, "title", "SenderChat")
        is_bot = False
        username = getattr(sender_chat, "username", None)
    return user_id, name, is_bot, username


# --- ИНЛАЙН И ТЕКСТОВЫЕ ИГРЫ (ОСТАЛИСЬ БЕЗ ИЗМЕНЕНИЙ) ---

@dp.inline_query()
async def inline_handler(query: types.InlineQuery):
    user_name = query.from_user.first_name or "Друг"
    quote_text = get_random_quote()
    holiday_text = get_today_holiday()
    results = []

    # 1. Цитата
    results.append(
        InlineQueryResultArticle(
            id="quote_random",
            title="📜 Выдать случайную цитату",
            input_message_content=InputTextMessageContent(message_text=f"📜 {quote_text}")
        )
    )

    # 2. Праздник
    if holiday_text:
        results.append(
            InlineQueryResultArticle(
                id="holiday_today",
                title="🥳 Поздравить с праздником!",
                description="Сегодня важный день",
                input_message_content=InputTextMessageContent(message_text=holiday_text)
            )
        )
    else:
        results.append(
            InlineQueryResultArticle(
                id="no_holiday",
                title="📅 Праздников сегодня нет",
                description="Обычный рабочий день...",
                input_message_content=InputTextMessageContent(
                    message_text="Сегодня нет праздников, но я всё равно желаю тебе хорошего дня!")
            )
        )

    # 3. Шутка
    try:
        joke_text = get_joke()
        results.append(
            InlineQueryResultArticle(
                id=f"joke",
                title="🤡 Случайная шутка",
                input_message_content=InputTextMessageContent(
                    message_text=f"🤡 {joke_text}"
                )
            )
        )
    except Exception as e:
        logging.error(f"Ошибка при получении шутки: {e}")

    # 4. Предсказание
    try:
        prediction = get_cookies()
        results.append(
            InlineQueryResultArticle(
                id=f"cookies",
                title="🥠 Печенье с предсказанием",
                input_message_content=InputTextMessageContent(
                    message_text=f"🥠 {prediction}"
                )
            )
        )
    except Exception as e:
        logging.error(f"Ошибка при получении {e}")

    results.append(
        InlineQueryResultArticle(
            id="greeting",
            title="👋 Приветствие",
            input_message_content=InputTextMessageContent(message_text=f"Привет, {user_name}!")
        )
    )
    await query.answer(results, cache_time=1)

# --- ХЭНДЛЕРЫ КОМАНД ---
@dp.message(F.text.lower().contains("братан, выдай цитату"))
async def quote_handler(message: types.Message):
    await message.answer(f"📜 {get_random_quote()}")


@dp.message(F.text.lower().startswith("братан, выбери"))
async def choose_handler(message: types.Message):
    content = message.text[12:].lower()
    if " или " in content:
        options = [opt.strip() for opt in content.split(" или ") if opt.strip()]
        await message.answer(f"🎲 Мой выбор: **{random.choice(options)}**")
    else:
        await message.answer("Используй 'или'. Пример: братан, выбери А или Б")


@dp.message(F.text.lower().contains("шанс") | F.text.lower().contains("вероятность"))
async def chance_handler(message: types.Message):
    if "братан" in message.text.lower():
        percent = random.randint(0, 100)
        await message.answer(f"🔮 Вероятность: **{percent}%**")


@dp.message(F.text.lower().contains("пидор"))
async def insult_handler(message: types.Message):
    user_name = (message.from_user.first_name if message.from_user else "Друг")
    await message.answer(f"Пидор - {user_name}!", reply_to_message_id=message.message_id)


# --- ХЕНДЛЕРЫ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Здарова, {message.from_user.first_name if message.from_user else 'User'}! 👋\n"
        "Я Митя. Теперь у меня есть память, характер и уши.\n"
        "Пиши /menu чтобы узнать, че я могу."
    )


@dp.message(Command("hi"))
async def cmd_hi(message: types.Message):
    if message.chat.type == 'private':
        await message.answer(f"Привет! Мы в личном чате. Твой id чата {message.from_user.id}")
    else:
        await message.answer(f"Привет! Я работаю в группе: {message.chat.title} id чата {message.chat.id}")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    user_name = message.from_user.first_name if message.from_user else "Друг"
    menu_text = (
        f"👋 Привет, {user_name}! Это **Меню Мити** — твой чатовый пацан с ИИ.\n\n"

        "1️⃣ **Общение с Митей**\n"
        "— Напиши **«Митя, ...»**, и я отвечу\n"
        "— В личке отвечаю всегда\n"
        "— В группе могу вклиниться сам (настраивается)\n\n"

        "2️⃣ **Голосовые фишки**\n"
        "— Отправь голосовое сообщение 🎤\n"
        "— Скажи «Митя», и я дам ответ голосом\n\n"

        "3️⃣ **Весёлые команды**\n"
        "— `братан, выдай цитату` — случайная цитата 📜\n"
        "— `братан, выбери А или Б` — я сделаю выбор 🎲\n"
        "— `братан, шанс ...` — вычислю вероятность 🔮\n\n"

        "4️⃣ **Inline-запросы**\n"
        "— @ИмяБота → получишь:\n"
        "   📜 Цитаты\n"
        "   🥳 Праздники\n"
        "   🤡 Шутки\n"
        "   🥠 Печенье с предсказанием\n"
        "   👋 Приветствие\n\n"

        "5️⃣ **Репутация**\n"
        "— `/karma` — узнать свою карму 📈\n"
        "— Позитивные сообщения повышают репутацию, токсичные — снижают\n\n"

        "6️⃣ **Настройки**\n"
        "— `/settings` — открыть меню управления ботом ⚙️\n"
        "— Включай/выключай ИИ, голос и авто-вмешательство\n\n"

        "😎 **Совет от Мити**\n"
        "Чем ты вежливее — тем я добрее 😉"
    )

    await message.answer(menu_text, parse_mode="Markdown")


def get_reputation_title(rep):
    levels = [
        (120, "💎 Легенда двора"),
        (100, "👑 Авторитет"),
        (80, "🤝 Старший кореш"),
        (60, "🤝 Ровный тип"),
        (40, "🙂 Уважаемый"),
        (10, "👤 Свой пацан"),
        (0, "👤 Прохожий"),
        (-10, "⚠️ Мутный тип"),
        (-40, "⚠️ Неприятный"),
        (-60, "❌ Чушпан"),
        (-80, "🔥 Конфликтный"),
        (-100, "☠️ Проблемный")
    ]

    for threshold, title in levels:
        if rep >= threshold:
            return title
    return "💀 Черт закатанный"


@dp.message(Command("karma"))
async def cmd_karma(message: types.Message):
    rep = await get_user_reputation(message.chat.id, message.from_user.id)
    rank = get_rank_name(rep)
    await message.reply(f"📈 Твоя репутация: {rep}\nТвой статус: **{rank}**", parse_mode="Markdown")



@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    s = await get_chat_settings(message.chat.id)
    builder = InlineKeyboardBuilder()

    builder.row(types.InlineKeyboardButton(
        text=f"🧠 ИИ: {'✅' if s['ai_enabled'] else '❌'}",
        callback_data=f"set_ai_{1 if not s['ai_enabled'] else 0}"
    ))
    builder.row(types.InlineKeyboardButton(
        text=f"🎤 Войс: {'✅' if s['voice_enabled'] else '❌'}",
        callback_data=f"set_voice_{1 if not s['voice_enabled'] else 0}"
    ))

    builder.row(
        types.InlineKeyboardButton(text="🔕 Молчать (0%)", callback_data="chance_0"),
        types.InlineKeyboardButton(text="🎲 10%", callback_data="chance_10"),
    )
    builder.row(
        types.InlineKeyboardButton(text="🎲 30%", callback_data="chance_30"),
        types.InlineKeyboardButton(text="🎲 50%", callback_data="chance_50"),
    )
    builder.row(types.InlineKeyboardButton(text="📢 Всегда (100%)", callback_data="chance_100"))

    await message.answer(
        f"🔧 **Настройки:**\n🎲 Шанс вклиниться: **{s['reply_chance']}%**",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("set_"))
async def settings_toggle(callback: CallbackQuery):
    try:
        _, param, value = callback.data.split("_")
        col = "ai_enabled" if param == "ai" else "voice_enabled"
        val_int = int(value)

        await update_setting(callback.message.chat.id, col, val_int)

        status = "✅ ВКЛ" if val_int == 1 else "❌ ВЫКЛ"
        setting_name = "Мозг (ИИ)" if param == "ai" else "Слух (Войс)"

        await callback.answer(f"{setting_name}: {status}")  # Всплывающее уведомление
        await callback.message.delete()  # Удаляем старое меню настроек
        await callback.message.answer(f"⚙️ Настройка изменена: **{setting_name}** теперь **{status}**",
                                      parse_mode="Markdown")
    except Exception:
        logging.exception("Ошибка обработки toggle settings")


@dp.callback_query(F.data.startswith("chance_"))
async def settings_chance(callback: CallbackQuery):
    try:
        value = int(callback.data.split("_")[1])
        await update_setting(callback.message.chat.id, "reply_chance", value)

        await callback.answer(f"Шанс: {value}%")  # Всплывающее уведомление
        await callback.message.delete()  # Удаляем старое меню настроек

        if value == 0:
            msg = "🤐 Митя больше не будет вклиниваться в разговор сам (Шанс 0%)"
        elif value == 100:
            msg = "📢 Митя теперь будет комментировать каждое сообщение! (Шанс 100%)"
        else:
            msg = f"🎲 Теперь Митя будет встревать в диалог с вероятностью **{value}%**"

        await callback.message.answer(msg, parse_mode="Markdown")
    except Exception:
        logging.exception("Ошибка обработки chance settings")


# --- ГОЛОСОВЫЕ ---
@dp.message(F.voice)
async def handle_voice(message: types.Message):
    # Ограничение длительности
    if message.voice.duration > 60:
        return await message.reply("Слышь, я такие длинные телеги не слушаю. Давай короче, до минуты!")
    s = await get_chat_settings(message.chat.id)
    if not s['voice_enabled']:
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    file = await bot.get_file(message.voice.file_id)

    # используем tempfile для безопасного создания уникального файла
    tf = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    path = tf.name
    tf.close()

    try:
        await bot.download_file(file.file_path, path)
        # Faster-Whisper возвращает 
        if whisper_model is None:
            logging.warning("Whisper model not loaded")
            await message.reply("Голосовой модуль недоступен.")
        else:
            segments, info = await asyncio.to_thread(whisper_model.transcribe, path, beam_size=1, language="ru")

        raw_text = " ".join([s.text for s in segments]).strip()

        if not raw_text:
            return await message.answer("Тишина в эфире...")

        # Анализ через новую функцию
        score = await check_toxicity_llm(raw_text)

        # Обновление репутации за голос
        user_id, name, is_bot, username = extract_sender_info(message)
        if not is_bot and score != 0:
            await update_reputation(message.chat.id, user_id, name, score)
            
            

        if "митя" in raw_text.lower():
            clean_text = raw_text.lower().replace("митя", "").strip()
            reply = await ask_mitya_ai(message.chat.id, clean_text, user_id)
            logging.info(f"DEBUG: voice reply={reply!r} for user_id={user_id}")
            await message.reply(f"🎤 Расшифровка: {raw_text}\n\n😎 Митя: {reply}")
        else:
            await message.reply(f"🎤 Расшифровка: {raw_text}")
    except Exception:
        logging.exception("Voice Error")
    finally:
        if os.path.exists(path):
            os.remove(path)


# --- ТЕКСТ ---
@dp.message(F.text)
async def smart_text_handler(message: types.Message):
    logging.info("HANDLER TRIGGERED")
    chat_id = message.chat.id
    is_forward = bool(message.forward_from or message.forward_from_chat)

    raw_text = message.text or ""
    text = raw_text.lower()

    user_id, name, is_bot, username = extract_sender_info(message)
    is_private = message.chat.type == "private"

    # ПРОВЕРКА: Является ли это ответом на сообщение нашего бота?
    is_reply_to_me = (
            message.reply_to_message and
            message.reply_to_message.from_user and
            message.reply_to_message.from_user.id == bot.id
    )




    # --- ЛОГИКА ЭМОЦИЙ МИТИ ---
    rand_val = random.randint(1, 100)


    score = await check_toxicity_llm(raw_text)  # int
    if score > 0:
        sentiment = "positive"
    elif score < 0:
        sentiment = "toxic"
    else:
        sentiment = "neutral"

    # 2. Обновление кармы
    if not is_bot and not is_forward:
        should_check_karma = is_private or ("митя" in text) or is_reply_to_me
        if should_check_karma and score != 0:
            await update_reputation(chat_id, user_id, name, score)

    s = await get_chat_settings(chat_id)  # получать настройки дальше по логике

    # СТАВИМ РЕАКЦИЮ
    if rand_val <= 40:
        EMOJI_MAP = {
            "positive": ["🔥", "👍", "🤝", "😎"],
            "toxic": ["💩", "🤡", "👎", "🤨"],
            "neutral": ["👀", "🤝"]
        }
        try:
            await asyncio.sleep(random.uniform(1, 2))  # Имитация чтения
            emo = random.choice(EMOJI_MAP.get(sentiment, ["👀"]))
            await message.react([types.ReactionTypeEmoji(emoji=emo)])
        except Exception:
            pass
    elif rand_val <= 55:
        try:
            # Показываем, что бот "печатает"
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(1)  # небольшая пауза

            # Выбираем стикер по настроению
            if sentiment == "positive" and STICKERS_POSITIVE:
                sticker_id = random.choice(STICKERS_POSITIVE)
                await message.reply_sticker(sticker=sticker_id)

            elif sentiment == "toxic" and STICKERS_TOXIC:
                sticker_id = random.choice(STICKERS_TOXIC)
                await message.reply_sticker(sticker=sticker_id)

            # Нейтральные — можно не показывать стикеры, или добавить свои
            # elif sentiment == "neutral" and STICKERS_NEUTRAL:
            #     await message.reply_sticker(random.choice(STICKERS_NEUTRAL))

        except Exception as e:
            logging.exception(f"Ошибка отправки стикера: {e}")



    # 2. Личка — отвечаем всем
    if is_private:
        if s['ai_enabled']:
            reply = await ask_mitya_ai(chat_id, raw_text, user_id=user_id)
            if reply:
                await message.answer(reply)
        return

    # 3. Ответ на сообщение бота ИЛИ явный вызов "Митя"
    if "митя" in text or is_reply_to_me:
        if not s['ai_enabled']:
            return

        # Если это ответ на сообщение бота, добавим контекст того сообщения
        full_prompt = raw_text
        if is_reply_to_me and message.reply_to_message.text:
            # Формируем промпт так, чтобы ИИ понимал, на что он отвечает
            full_prompt = f"(Ответ на твоё сообщение: '{message.reply_to_message.text}') {raw_text}"

        # Очистка от слова "митя" для группы, если оно там есть
        clean_prompt = full_prompt
        if "митя" in text:
            try:
                idx = raw_text.lower().find("митя")
                clean_prompt = (raw_text[:idx] + raw_text[idx + len("митя"):]).strip()
            except Exception:
                clean_prompt = raw_text.replace("митя", "").strip()

        reply = await ask_mitya_ai(chat_id, clean_prompt, user_id=user_id)
        if reply:
            await message.reply(reply)  # Отвечаем реплаем для удобства диалога
        return

    # 4. Случайное вклинивание
    if s['ai_enabled'] and s['reply_chance'] > 0:
        if random.randint(1, 100) <= s['reply_chance']:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            reply = await ask_mitya_ai(chat_id, raw_text, user_id=user_id, is_auto=True)
            if reply:
                await message.answer(reply)


# --- ЗАПУСК ---
async def main():
    await init_db()
    logging.info("Митя запущен!")
    await bot.set_my_commands([
        types.BotCommand(command="hi", description="Привет узнать id"),
        types.BotCommand(command="start", description="Перезапустить"),
        types.BotCommand(command="menu", description="Меню"),
        types.BotCommand(command="settings", description="Настройки"),
        types.BotCommand(command="karma", description="Репутация")
    ])
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")

