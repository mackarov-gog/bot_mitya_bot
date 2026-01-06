import asyncio
import httpx
import json
import random
import os
import logging
import whisper
import aiosqlite
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "mitya_data.db"

if not TOKEN:
    exit("Ошибка: токен не найден!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ИНИЦИАЛИЗАЦИЯ WHISPER ---
logging.info("Загрузка модели Whisper...")
# Используем модель 'tiny' для экономии памяти. Можно поменять на 'base' или 'small', если нужно точнее.
whisper_model = whisper.load_model("small")
logging.info("Whisper готов к работе!")


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
        except:
            pass  # Колонка уже есть

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
        await db.commit()


async def get_chat_settings(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        # Пытаемся получить настройки. Если колонки старые, запрос может упасть, но init_db должен был поправить.
        try:
            async with db.execute(
                    "SELECT ai_enabled, voice_enabled, reply_chance FROM chats WHERE chat_id = ?",
                    (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"ai_enabled": row[0], "voice_enabled": row[1], "reply_chance": row[2]}
        except Exception as e:
            logging.error(f"Ошибка чтения настроек: {e}")

        # Если чата нет или ошибка -> создаем дефолт
        await db.execute(
            "INSERT OR IGNORE INTO chats (chat_id, ai_enabled, voice_enabled, reply_chance) VALUES (?, 1, 1, 0)",
            (chat_id,)
        )
        await db.commit()
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
        await db.execute('''
            INSERT INTO users (user_id, chat_id, first_name, reputation)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET 
            reputation = reputation + ?,
            first_name = ?
        ''', (user_id, chat_id, name, change, change, name))
        await db.commit()


async def get_user_reputation(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT reputation FROM users WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def save_context(chat_id, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content)
        )
        await db.execute('''
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages WHERE chat_id = ?
                ORDER BY timestamp DESC LIMIT -1 OFFSET 25
            )
        ''', (chat_id,))
        await db.commit()


async def get_context(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT role, content FROM messages 
            WHERE chat_id = ? 
            AND timestamp > datetime('now', '-6 hours')
            ORDER BY timestamp ASC LIMIT 25
        ''', (chat_id,)) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r[0], "content": r[1]} for r in rows]


# --- ФУНКЦИИ КОНТЕНТА ---

# --- ФУНКЦИИ РАБОТЫ С ДАННЫМИ ---

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
    return data["joke"]["text"]

def get_cookies():
    url = "https://api.forismatic.com/api/1.0/?method=getQuote&format=json&lang=ru"
    response = requests.get(url)
    data = response.json()
    return data["quoteText"]

def get_random_quote():
    try:
        base_path = os.path.dirname(__file__)
        file_path = os.path.join(base_path, 'quotes_Statham.json')
        with open(file_path, 'r', encoding='utf-8') as f:
            quotes = json.load(f)
            quote_data = random.choice(quotes)
            return quote_data.get('text', "Текст не найден") if isinstance(quote_data, dict) else str(quote_data)
    except Exception as e:
        logging.error(f"Ошибка чтения цитат: {e}")
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
    except Exception as e:
        print(f"Ошибка парсинга праздников: {e}")
        return None


# --- МОЗГИ (LLM) ---

async def check_toxicity_llm(text: str) -> str:
    url = "http://ollama:11434/api/generate"
    prompt = f"System: Ты — модератор. Проанализируй сообщение. Если это мат или агрессия — ответь 'toxic'. Если позитив — 'positive'. Иначе 'neutral'.\nMessage: {text}\nAnswer:"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(url, json={
                "model": "mitya-gemma",
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 5, "temperature": 0.0}
            })
            result = response.json().get("response", "").lower()
            if "toxic" in result: return "toxic"
            if "positive" in result: return "positive"
            return "neutral"
    except:
        return "neutral"


async def ask_mitya_ai(chat_id: int, user_text: str, user_id: int = None, is_auto: bool = False):
    await save_context(chat_id, "user", user_text)
    history = await get_context(chat_id)

    system_instruction = ""
    if user_id:
        rep = await get_user_reputation(chat_id, user_id)
        if rep < -5:
            system_instruction = "Собеседник — грубиян. Отвечай дерзко."
        elif rep > 5:
            system_instruction = "Собеседник — друг. Будь вежлив."

    if is_auto:
        system_instruction += " Ты решил сам вмешаться в разговор. Шути коротко."

    if system_instruction:
        history.insert(0, {"role": "system", "content": system_instruction})

    payload = {
        "model": "mitya-gemma",
        "messages": history,
        "stream": False,
        "options": {"num_predict": 150, "temperature": 0.7}
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("http://ollama:11434/api/chat", json=payload)
            reply = response.json().get("message", {}).get("content", "").strip()
            if reply:
                await save_context(chat_id, "assistant", reply)
                return reply
    except Exception as e:
        logging.error(f"AI Error: {e}")
    return "Чет я задумался..."









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

@dp.message(F.text.lower().contains("братан, шанс") | F.text.lower().contains("братан, вероятность"))
async def chance_handler(message: types.Message):
    if "митя" in message.text.lower():
        percent = random.randint(0, 100)
        await message.answer(f"🔮 Вероятность: **{percent}%**")

@dp.message(F.text.lower().contains("пидор"))
async def insult_handler(message: types.Message):
    user_name = message.from_user.first_name or "Друг"
    await message.answer(f"Пидор - {user_name}!", reply_to_message_id=message.message_id)

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Здарова, {message.from_user.first_name}! 👋\n"
        "Я Митя. Теперь у меня есть память, характер и уши.\n"
        "Пиши /menu чтобы узнать, че я могу."
    )

@dp.message(Command("hi"))
async def cmd_start(message: types.Message):
    if message.chat.type == 'private':
        await message.answer(f"Привет! Мы в личном чате. Твой id чата {message.from_user.id}")
    else:
        await message.answer(f"Привет! Я работаю в группе: {message.chat.title} id чата {message.chat.id}")

# !!! 
@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    menu_text = (
        "📋 **Меню Мити**\n\n"
        "🤖 **Общение**\n"
        "— Напиши **«Митя, ...»** — я отвечу\n"
        "— В личке отвечаю всегда\n"
        "— В группе могу вклиниться сам (настраивается)\n\n"
        "🎤 **Голос**\n"
        "— Отправь голосовое\n"
        "— Если скажешь «Митя» — отвечу\n\n"
        "🎲 **Команды в чате**\n"
        "— `братан, выдай цитату`\n"
        "— `братан, выбери А или Б`\n"
        "— `братан, шанс ...`\n\n"
        "📈 **Репутация**\n"
        "— `/karma` — посмотреть свою карму\n"
        "— За токсик карма падает, за позитив растёт\n\n"
        "⚙️ **Управление**\n"
        "— `/settings` — настройки (для админов)\n"
        "— Вкл/выкл ИИ и голос\n"
        "— Шанс, что я сам начну говорить\n\n"
        "😎 **Совет**\n"
        "Чем ты вежливее — тем я добрее."
    )
    await message.answer(menu_text, parse_mode="Markdown")



@dp.message(Command("karma"))
async def cmd_karma(message: types.Message):
    rep = await get_user_reputation(message.chat.id, message.from_user.id)
    await message.reply(f"📈 Твоя репутация: {rep}")


# --- МЕНЮ НАСТРОЕК (ОБНОВЛЕННОЕ) ---

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):

    s = await get_chat_settings(message.chat.id)
    builder = InlineKeyboardBuilder()

    # Кнопки ВКЛ/ВЫКЛ
    builder.row(types.InlineKeyboardButton(text=f"🧠 ИИ: {'✅' if s['ai_enabled'] else '❌'}",
                                           callback_data=f"set_ai_{1 if not s['ai_enabled'] else 0}"))
    builder.row(types.InlineKeyboardButton(text=f"🎤 Войс: {'✅' if s['voice_enabled'] else '❌'}",
                                           callback_data=f"set_voice_{1 if not s['voice_enabled'] else 0}"))

    # Кнопки ШАНСА ОТВЕТА (Вместо счетчика)
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


@dp.callback_query(F.data.startswith("chance_"))
async def settings_chance(callback: CallbackQuery):
    value = int(callback.data.split("_")[1])
    await update_setting(callback.message.chat.id, "reply_chance", value)

    await callback.answer(f"Шанс: {value}%")  # Всплывающее уведомление
    await callback.message.delete()  # Удаляем старое меню настроек

    # Текст сообщения в зависимости от уровня
    if value == 0:
        msg = "🤐 Митя больше не будет вклиниваться в разговор сам (Шанс 0%)"
    elif value == 100:
        msg = "📢 Митя теперь будет комментировать каждое сообщение! (Шанс 100%)"
    else:
        msg = f"🎲 Теперь Митя будет встревать в диалог с вероятностью **{value}%**"

    await callback.message.answer(msg, parse_mode="Markdown")


# --- ГОЛОСОВЫЕ ---

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    s = await get_chat_settings(message.chat.id)
    if not s['voice_enabled']: return

    await bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
    file = await bot.get_file(message.voice.file_id)
    path = f"voice_{message.voice.file_id}.ogg"

    try:
        await bot.download_file(file.file_path, path)
        result = whisper_model.transcribe(path, language='ru')
        raw_text = result.get("text", "").strip()

        if not raw_text: return await message.answer("Не расслышал...")

        # Анализ токсичности
        sentiment = await check_toxicity_llm(raw_text)
        if sentiment == "toxic":
            await update_reputation(message.chat.id, message.from_user.id, message.from_user.first_name, -1)
        elif sentiment == "positive":
            await update_reputation(message.chat.id, message.from_user.id, message.from_user.first_name, 1)

        if "митя" in raw_text.lower():
            clean_text = raw_text.lower().replace("митя", "").strip()
            reply = await ask_mitya_ai(message.chat.id, clean_text, message.from_user.id)
            await message.reply(f"🎤 Расшифровка: {raw_text}\n\n😎 Митя: {reply}")
        else:
            await message.reply(f"🎤 Расшифровка: {raw_text}")
    except Exception as e:
        logging.error(f"Voice Error: {e}")
    finally:
        if os.path.exists(path): os.remove(path)


# --- ТЕКСТ ---

@dp.message(F.text)
async def smart_text_handler(message: types.Message):
    chat_id = message.chat.id[cite: 5]
    text = message.text.lower()[cite: 5]
    user_id = message.from_user.id[cite: 5]
    name = message.from_user.first_name[cite: 5]
    is_private = message.chat.type == "private"

    
    if message.from_user.is_bot:
        if "митя" not in text:
            return  # Игнорируем других ботов, если они не зовут Митю лично

    # 1. Проверка токсичности
    if "митя" in text:
        sentiment = await check_toxicity_llm(text)
        if sentiment == "toxic":
            await update_reputation(chat_id, user_id, name, -1)
        elif sentiment == "positive":
            await update_reputation(chat_id, user_id, name, 1)

    s = await get_chat_settings(chat_id)

    # 2. Если это ЛС - отвечаем всегда (если ИИ включен)
    if is_private:
        if s['ai_enabled']:
            reply = await ask_mitya_ai(chat_id, message.text, user_id)
            await message.answer(reply)
        return

    # 3. ГРУППА: Явный вызов по имени
    if text.startswith("митя"):
        if not s['ai_enabled']: return
        clean_prompt = message.text[4:].strip()
        reply = await ask_mitya_ai(chat_id, clean_prompt, user_id)
        await message.answer(reply)
        return

    # 4. ГРУППА: Случайное вклинивание (ВМЕСТО СЧЕТЧИКА)
    # Если шанс > 0, кидаем кубик от 1 до 100. Если выпало <= шансу, отвечаем.
    if s['ai_enabled'] and s['reply_chance'] > 0:
        if random.randint(1, 100) <= s['reply_chance']:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            reply = await ask_mitya_ai(chat_id, message.text, user_id, is_auto=True)
            await message.answer(reply)


# --- ЗАПУСК ---

async def main():
    await init_db()[cite: 5]
    logging.info("Митя запущен!")[cite: 5]
    await bot.set_my_commands([
        types.BotCommand(command="hi", description="Привет узнать id"),
        types.BotCommand(command="start", description="Перезапустить"),
        types.BotCommand(command="menu", description="Меню"),
        types.BotCommand(command="settings", description="Настройки"),
        types.BotCommand(command="karma", description="Репутация")
    ])
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())