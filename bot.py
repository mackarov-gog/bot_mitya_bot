import asyncio
import httpx 
import json
import random
import os
import logging
import requests
import whisper  
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, TelegramObject
from aiogram.filters import Command
from zoneinfo import ZoneInfo

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: токен не найден!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ИНИЦИАЛИЗАЦИЯ WHISPER ---
logging.info("Загрузка модели Whisper...")
# Используем модель 'tiny' для экономии памяти. Можно поменять на 'base' или 'small', если нужно точнее.
whisper_model = whisper.load_model("small")
logging.info("Whisper готов к работе!")

# --- ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ ---
seen_users = {}

class UserTrackingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user and not user.is_bot:
            seen_users[user.id] = user.first_name
        return await handler(event, data)

dp.message.middleware(UserTrackingMiddleware())

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


# --- Могзи  ---
async def ask_mitya_ai(user_text: str):
    url = "http://ollama:11434/api/chat"

    payload = {
        "model": "mitya-gemma",
        "messages": [
            {
                "role": "user",
                "content": user_text
            }
        ],
        "stream": False,
        "options": {
            "num_predict": 120,
            "temperature": 0.7  
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload)
        if response.status_code == 200:
            # В режиме /api/chat ответ лежит по другому пути в JSON
            return response.json().get("message", {}).get("content", "").strip()
        else:
            return "Чет связь оборвалась, перезвони позже."

# --- ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Здарова, {message.from_user.first_name}! 👋\n"
        "Я Митя. Теперь у меня есть память, характер и уши.\n"
        "Пиши /menu чтобы узнать, че я могу."
    )

@dp.message(Command("/menu"))
async def cmd_menu(message: types.Message):
    menu_text = (
        "🤖 **Что я умею:**\n\n"
        "🎤 **Слух:** Отправь голосовое сообщение.\n"
        "📜 **Цитаты:** Напиши 'Митя, выдай цитату'.\n"
        "🎲 **Выбор:** Напиши 'Митя, выбери пиво или квас'.\n"
        "🔮 **Шанс:** Напиши 'Митя, какой шанс на успех?'.\n"
        "🏆 **Игры:** Напиши 'Митя, кто сегодня красавчик?'.\n"
        "🎉 **Праздники:** Ищи в инлайн-режиме (@ ник бота).\n"
    )
    await message.answer(menu_text, parse_mode="Markdown")

@dp.message(F.text.lower().contains("митя") & 
           (F.text.lower().contains("умеешь") | 
            F.text.lower().contains("можешь") | 
            F.text.lower().contains("помощь")))
async def mitya_info_text(message: types.Message):
    await message.answer("Я умею слушать голосовые сообщения! Просто запиши что-нибудь.")





@dp.message(F.text.lower().contains("митя, выдай цитату"))
async def quote_handler(message: types.Message):
    await message.answer(f"📜 {get_random_quote()}")

@dp.message(F.text.lower().startswith("митя, кто"))
async def who_is_handler(message: types.Message):
    if not seen_users:
        await message.answer("Я пока никого не знаю. Напишите что-нибудь в чат!")
        return
    winner = random.choice(list(seen_users.values()))
    question = message.text.lower().replace("митя, кто", "").strip().rstrip("?")
    if not question: question = "сегодня везунчик"
    await message.answer(f"🤔 Анализирую чат...\n✨ {question.capitalize()} — это **{winner}**! 🏆")

@dp.message(F.text.lower().startswith("митя, выбери"))
async def choose_handler(message: types.Message):
    content = message.text[12:].lower()
    if " или " in content:
        options = [opt.strip() for opt in content.split(" или ") if opt.strip()]
        await message.answer(f"🎲 Мой выбор: **{random.choice(options)}**")
    else:
        await message.answer("Используй 'или'. Пример: Митя, выбери А или Б")

@dp.message(F.text.lower().contains("шанс") | F.text.lower().contains("вероятность"))
async def chance_handler(message: types.Message):
    if "митя" in message.text.lower():
        percent = random.randint(0, 100)
        await message.answer(f"🔮 Вероятность: **{percent}%**")

@dp.message(F.text.lower().contains("пидор"))
async def insult_handler(message: types.Message):
    user_name = message.from_user.first_name or "Друг"
    await message.answer(f"Пидор - {user_name}!", reply_to_message_id=message.message_id)

# --- ОБРАБОТЧИК ГОЛОСОВЫХ С ЛОГИКОЙ ОБРАЩЕНИЯ ---
@dp.message(F.voice)
async def handle_voice(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    local_filename = f"voice_{file_id}.ogg"

    try:
        # 1. Скачиваем и расшифровываем
        await bot.download_file(file.file_path, local_filename)
        result = whisper_model.transcribe(local_filename, fp16=False, language='ru')
        raw_text = result.get("text", "").strip()

        if not raw_text:
            await message.answer("Ничего не услышал. Попробуй еще раз.")
            return

        # 2. Проверяем, позвали ли Митю (регистронезависимо)
        if "митя" in raw_text.lower():
            # Отрезаем имя "Митя" для более чистого запроса к ИИ
            # (необязательно, но так нейросеть лучше понимает суть)
            clean_prompt = raw_text.lower().replace("митя", "").strip(",. ")

            ai_reply = await ask_mitya_ai(clean_prompt)
            await message.reply(f"🎤 *Ты сказал:* {raw_text}\n\n😎 *Митя:* {ai_reply}", parse_mode="Markdown")
        else:
            # Если Митю не звали — просто выдаем текст
            await message.reply(f"📝 *Расшифровка:* {raw_text}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Ошибка при обработке голосового: {e}")
        await message.answer("Не удалось обработать голос 😔")

    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)

# --- УМНЫЙ ТЕКСТОВЫЙ ОБРАБОТЧИК (Qwen для текста) ---
@dp.message(F.text.lower().startswith("митя"))
async def smart_text_handler(message: types.Message):
    text = message.text.lower()
    
    # Список слов-исключений, чтобы не перебивать старые команды (выбор, кто, цитата)
    exceptions = ["выбери", "кто", "выдай цитату", "шанс", "вероятность", "/settings", "/menu", "/start"]
    
    # Если это НЕ старая команда, то отправляем в мозги (Qwen)
    if not any(word in text for word in exceptions):
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        # Отрезаем само слово "Митя" и знаки препинания в начале
        prompt = message.text[4:].strip().lstrip(",. ")
        
        if not prompt:
            await message.answer("Чего звал? Я тут. Задай вопрос или запиши голосовое.")
            return

        ai_reply = await ask_mitya_ai(prompt)
        await message.answer(ai_reply)


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



# --- ЗАПУСК ---

async def main():
    logging.info("Митя запущен и готов к общению!")
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Перезагрузить"),
        types.BotCommand(command="menu", description="Возможности"),
        types.BotCommand(command="settings", description="Настройки чата")
    ])
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")