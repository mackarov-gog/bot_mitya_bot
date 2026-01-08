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
from bs4 import BeautifulSoup
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
        # Таблица для стикеров
        await db.execute('''
            CREATE TABLE IF NOT EXISTS collected_stickers (
                file_id TEXT PRIMARY KEY,
                emoji TEXT,
                sentiment TEXT
            )
        ''')
        await db.commit()

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


async def mit_info_search(query: str):
    """Парсинг DuckDuckGo HTML для Mit Info"""
    url = "https://html.duckduckgo.com/html/"
    payload = {'q': query}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://duckduckgo.com/"
    }
    try:
        # Используем httpx, так как он уже есть в твоем проекте
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, data=payload, headers=headers)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            for result in soup.find_all('div', class_='result'):
                snippet_tag = result.find('a', class_='result__snippet')
                if snippet_tag:
                    text = snippet_tag.get_text(strip=True)
                    results.append(re.sub(r'\s+', ' ', text))

            return "\n\n".join(results[:3]) if results else None
    except Exception as e:
        logging.error(f"Search error: {e}")
        return None

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
        return data.get("quoteText", "Цитата пустая") # Добавлен return
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
        f""" Ты — судья в чате. Оцени сообщение от -5 до +5.

ШКАЛА:
-5 = Жесткое оскорбление, мат на человека, агрессия.
-1 = Грубость, токсичность, попытка обидеть.
0 = Обычный текст, вопрос, инфо, мат без злобы (междометие).
+1 = Вежливость, хороший совет, доброе слово.
+5 = Огромная помощь, высшее уважение.

ПРАВИЛА:

Если текст нейтральный или это просто вопрос — ставь 0.
Не ищи зло там, где его нет. Сомневаешься — ставь 0.
Отвечай ТОЛЬКО числом.
Message: {text}
        Answer:"""
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


async def ask_mitya_special(prompt, system_instruction):
    """
    Универсальная функция для разовых задач (анекдоты, дополнение текста).
    Не сохраняет историю, работает максимально быстро.
    """
    payload = {
        "model": "mitya-gemma",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "options": {
            "temperature": 0.8, # Чуть выше для креативности в анекдотах
            "num_predict": 150
        }
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post("http://ollama:11434/api/chat", json=payload)
            response.raise_for_status()
            return response.json()['message']['content'].strip()
    except Exception as e:
        logging.error(f"Ошибка в special_ai: {e}")
        return "Чето не придумывается ниче, брат..."

async def ask_mitya_ai(chat_id: int, user_text: str, user_id: int = None,
                     user_name: str = "Пацан", reply_to_text: str = None, is_auto: bool = False):
    # 1. Сохраняем с именем
    await save_context(chat_id, "user", user_text, user_name)

    # 2. Получаем историю
    history = await get_context(chat_id)

    rep = 0
    if user_id is not None:
        rep = await get_user_reputation(chat_id, user_id)

    if reply_to_text:
        history.append({"role": "assistant", "content": reply_to_text})


    extra_info = "Относись нейтрально."
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
        f"👋 Здарова, {user_name}! Я Митя — твой ровный ИИ‑соавтор. Вот чё я умею — читай внимательно, чтоб потом не переспрашивать:\n\n"


        "🤖 **1. ОБЩЕНИЕ СО МНОЙ**\n"
        "— В личке пиши что хочешь — отвечу всегда.\n"
        "— В группах зови по имени: **«Митя, [твой вопрос]»**.\n"
        "— Могу сам вклиниться в диалог (настраивается в `/settings`).\n\n"

        "🎭 **2. КРЕАТИВ И РАЗВЛЕЧЕНИЯ**\n"
        "— `Mit a [тема]` — сочный анекдот на заданную тему.\n"
        "— `Mit t [начало]` — продолжу твою фразу в живом стиле.\n"
        "— `братан, выдай цитату` — случайная цитата 📜.\n"
        "— `братан, выбери А или Б` — сделаю выбор за тебя 🎲.\n"
        "— `братан, шанс ...` — посчитаю вероятность 🔮.\n"
        "— Отправь голосовое 🎤 — я всё услышу и отвечу (можно голосом!).\n\n"

        "🖼 **3. СТИКЕРЫ И РЕАКЦИИ**\n"
        "— Запоминаю ваши стикеры и кидаю их в тему.\n"
        "— Ставлю реакции на сообщения — смотрю, что ты пишешь.\n\n"

        "📈 **4. КАРМА И РЕПУТАЦИЯ**\n"
        "— `/karma` — узнай, кто ты: Авторитет или Чушпан.\n"
        "— Вежливость повышает карму, хамство — снижает.\n"
        "— Токсичные сообщения = я гноблю, ровные = мы кореша.\n\n"

        "⚙️ **5. НАСТРОЙКИ И ИНЛАЙН‑ЗАПРОСЫ**\n"
        "— `/settings` — настрой мои «мозги»: шанс ответа, ИИ, голос, авто‑вмешательство.\n"
        "— `@Твой_Юзернейм_Бота` — инлайн‑меню с:\n"
        "   • 📜 Цитатами\n"
        "   • 🥳 Праздниками\n"
        "   • 🤡 Шутками\n"
        "   • 🥠 Печеньем с предсказанием\n"
        "   • 👋 Приветствием\n\n"

        "☝️ **ВАЖНО**\n"
        "— Я запоминаю последние 20 сообщений — не делай вид, что мы не знакомы.\n"
        "— Общайся красиво — и всё будет ровно.\n"
        "— Чем ты вежливее — тем я добрее 😉"
    )

    await message.answer(menu_text, parse_mode="Markdown")


def get_rank_name(rep):
    levels = [
        (120, "💎 Легенда двора"),
        (100, "👑 Авторитет"),
        (80, "🤝 Старший кореш"),
        (60, "🤝 Ровный тип"),
        (40, "🙂 Уважаемый"),
        (10, "👤 Свой пацан"),
        (-5, "👤 Прохожий"),
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


@dp.message(F.sticker)
async def catch_stickers_handler(message: types.Message):
    if message.from_user.id == bot.id:
        return

    f_id = message.sticker.file_id
    emoji = message.sticker.emoji or "❓"

    # Быстрая оценка контекста (чтобы не гонять LLM на каждый чих)
    # Если хочешь идеальной точности, можно вызвать тут check_toxicity_llm(emoji)
    score = await check_toxicity_llm(f"Стикер с эмодзи: {emoji}")

    if score >= 1:
        sentiment = "positive"
    elif score <= -1:
        sentiment = "toxic"
    else:
        sentiment = "neutral"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO collected_stickers (file_id, emoji, sentiment) VALUES (?, ?, ?)",
            (f_id, emoji, sentiment)
        )
        await db.commit()


# --- КОМАНДА: Mit a (Анекдот) ---
@dp.message(F.text.lower().startswith("mit a") | F.text.lower().startswith("мит а"))
async def mitya_joke_handler(message: types.Message):
    topic = message.text[5:].strip()
    sys_instr = "Ты — мастер анекдотов. Рассказывай коротко, смешно, в стиле ростовского пацана."
    prompt = f"Расскажи анекдот на тему: {topic}" if topic else "Расскажи любой анекдот."

    await bot.send_chat_action(message.chat.id, "typing")
    joke = await ask_mitya_special(prompt, sys_instr)
    await message.reply(joke)


# --- КОМАНДА: Mit t (Продолжи фразу) ---
@dp.message(F.text.lower().startswith("mit t") | F.text.lower().startswith("мит т"))
async def mitya_continue_handler(message: types.Message):
    # Вырезаем префикс "mit t " аккуратно
    start_text = message.text[5:].lstrip()

    if not start_text:
        return await message.reply("Напиши, че продолжить-то? Можешь даже слово не дописывать.")

    # Обновленный системный промпт для склейки слов
    sys_instr = (
        "Ты — соавтор. Тебе дают начало текста (возможно, оборванное на полуслове). "
        "Твоя задача: ПРЯМО ПРОДОЛЖИТЬ текст без пробелов в начале, если это логично. "
        "Пиши ТОЛЬКО продолжение. Стиль: кратко, дерзко, по-пацански."
    )

    await bot.send_chat_action(message.chat.id, "typing")
    continuation = await ask_mitya_special(start_text, sys_instr)

    # Убираем лишние пробелы в начале ответа ИИ, если он их всё же добавил
    continuation = continuation.lstrip()

    # Склеиваем БЕЗ пробела, чтобы можно было дополнять слова
    await message.answer(f"{start_text}{continuation}")

# --- КОМАНДА: Mit s (Случайный стикер) ---
@dp.message(F.text.lower().startswith("mit s") | F.text.lower().startswith("мит c"))
async def mitya_random_sticker_handler(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        # Выбираем один случайный file_id из всей таблицы
        cursor = await db.execute(
            "SELECT file_id FROM collected_stickers ORDER BY RANDOM() LIMIT 1"
        )
        row = await cursor.fetchone()

        if row:
            sticker_id = row[0]
            # Отправляем стикер как ответ на команду
            await message.answer_sticker(sticker=sticker_id)
        else:
            # Если база еще пустая, Митя ответит по-пацански
            await message.reply("Пусто в закромах, еще ни одного стикера не подрезал.")

# --- КОМАНДА: Mit i (Пробить инфу) ---
@dp.message(F.text.lower().startswith("mit i") | F.text.lower().startswith("мит и"))
async def mitya_web_search_handler(message: types.Message):
    # Извлекаем сам запрос
    if message.text.lower().startswith("митя, пробни"):
        query = message.text[12:].strip()
    else:
        query = message.text[8:].strip()

    if not query:
        return await message.reply("А че пробивать-то? Пиши запрос после команды, не тупи.")

    await bot.send_chat_action(message.chat.id, "typing")

    # 1. Лезем в инет
    raw_info = await mit_info_search(query)

    if not raw_info:
        return await message.reply("Слышь, в инете по этой теме глухо, как в танке.")

    # 2. Просим ИИ пересказать инфу
    sys_instr = (
        "Ты — Митя. Тебе принесли инфу из интернета. "
        "Твоя задача: коротко (2-3 предложения) пояснить корешу суть на ростовском сленге. "
        "Не читай лекций, говори как в жизни."
    )
    prompt = f"Вот инфа из поиска: {raw_info}\n\nПоясни за это: {query}"

    mitya_explanation = await ask_mitya_special(prompt, sys_instr)

    await message.reply(f"🔍 **Mit Info докладывает:**\n\n{mitya_explanation}")

# --- ГОЛОСОВЫЕ ---
@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if whisper_model is None:
        logging.warning("Whisper model not loaded")
        return await message.reply("Голосовой модуль недоступен.")

    # Ограничение длительности
    if message.voice.duration > 60:
        return await message.reply("Слышь, я такие длинные телеги не слушаю. Давай короче, до минуты!")

    s = await get_chat_settings(message.chat.id)
    if not s['voice_enabled']:
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    file = await bot.get_file(message.voice.file_id)

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
    text_lower = raw_text.lower() # Исправлено имя переменной для консистентности

    user_id, name, is_bot, username = extract_sender_info(message)
    is_private = message.chat.type == "private"

    is_reply_to_me = (
            message.reply_to_message and
            message.reply_to_message.from_user and
            message.reply_to_message.from_user.id == bot.id
    )

    # 1. Оценка токсичности
    score = await check_toxicity_llm(raw_text)
    sentiment = "neutral"
    if score > 0:
        sentiment = "positive"
    elif score < 0:
        sentiment = "toxic"

    # 2. Обновление кармы
    if not is_bot and not is_forward:
        should_check_karma = is_private or ("митя" in text_lower) or is_reply_to_me
        if should_check_karma and score != 0:
            await update_reputation(chat_id, user_id, name, score)

    s = await get_chat_settings(chat_id)


    # --- БЛОК 1: РЕАКЦИИ (Независимо) ---
    rand_val = random.randint(1, 100)
    if rand_val <= 20:
        EMOJI_MAP =  {
    "positive": [
        "🔥", "👍", "🚀", "💥", "💪", "👑", "😎", "🥳", "✨", "🌟", "❤️",
        "👍", "🙌", "🔥🔥", "💯", "😍", "🤩", "👏", "🤑", "🎉"
    ],
    "toxic": [
        "👎", "🤡", "🤨", "🖕", "😒", "🤬", "🤮", "💩", "🗑️", "😤",
        "🤡🤡", "🙄", "😑", "🤦‍♂️", "🤦", "🐍", "🤢", "🚮", "😡"
    ],
    "neutral": [
        "👀", "🤝", "😐", "🤔", "👌", "🔍", "📊", "💭", "🧐", "🤷",
         "👁️", "🕵️", "⚖️", "🟡", "🤙", "✌️", "🧘", "🔎", "📝"
    ]
}
        try:
            emo = random.choice(EMOJI_MAP.get(sentiment, ["👀"]))
            await message.react([types.ReactionTypeEmoji(emoji=emo)])
        except Exception:
            pass

    # --- БЛОК 2: СТИКЕРЫ (Независимо) ---
    rand_val = random.randint(1, 100)
    if 35 <= rand_val <= 55:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Выбираем случайный стикер по нужному настроению
                cursor = await db.execute(
                    "SELECT file_id FROM collected_stickers WHERE sentiment = ? ORDER BY RANDOM() LIMIT 1",
                    (sentiment,)
                )
                row = await cursor.fetchone()

                if row:
                    sticker_to_send = row[0]
                    await message.reply_sticker(sticker=sticker_to_send)
                else:
                    # Фоллбэк (если база еще пустая)
                    backup = STICKERS_POSITIVE if sentiment == "positive" else STICKERS_TOXIC
                    await message.reply_sticker(sticker=random.choice(backup))
        except Exception as e:
            logging.error(f"Ошибка при выдаче стикера из БД: {e}")

    # --- БЛОК 3: ТЕКСТОВЫЙ ОТВЕТ ИИ (Теперь вне условий стикеров!) ---
    if not s['ai_enabled']:
        return

    reply_text = None
    is_auto = False

    # А. Логика для лички
    if is_private:
        reply_text = await ask_mitya_ai(chat_id, raw_text, user_id=user_id)

    # Б. Логика для групп (обращение или реплай)
    elif "митя" in text_lower or is_reply_to_me:
        clean_prompt = raw_text
        if "митя" in text_lower:
            # Используем регулярку, чтобы убрать только слово "митя"
            clean_prompt = re.sub(r'\bмитя\b', '', raw_text, flags=re.IGNORECASE).strip()
            if not clean_prompt: clean_prompt = "Ау"

        reply_to_context = message.reply_to_message.text if is_reply_to_me else None
        reply_text = await ask_mitya_ai(
            chat_id,
            clean_prompt,
            user_id=user_id,
            user_name=name,
            reply_to_text=reply_to_context
        )

    # В. Случайное вклинивание
    elif s['reply_chance'] > 0 and random.randint(1, 100) <= s['reply_chance']:
        is_auto = True
        reply_text = await ask_mitya_ai(chat_id, raw_text, user_id=user_id, is_auto=True)

    # ОТПРАВКА ТЕКСТА
    if reply_text:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(random.uniform(0.5, 1.5))
        if is_auto:
            await message.answer(reply_text)
        else:
            await message.reply(reply_text)


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

