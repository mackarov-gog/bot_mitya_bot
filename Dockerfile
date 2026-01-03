# 1. Используем официальный образ Python. 
FROM python:3.10-slim

# 2. Устанавливаем системные зависимости.
# ffmpeg — ОБЯЗАТЕЛЕН для работы Whisper (декодирует аудио из Telegram).
# git — нужен для установки Whisper напрямую из репозитория, если потребуется.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 3. Создаем рабочую директорию внутри контейнера.
WORKDIR /app

# 4. Сначала копируем только список зависимостей.
# Это позволяет Docker кэшировать установленные библиотеки.
COPY requirements.txt .

# 5. Устанавливаем библиотеки Python.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 6. Копируем все остальные файлы проекта.
# Сюда попадут: bot.py, holidays.json, quotes_Statham.json, xa.json и Митя.png.
COPY . .

# 7. Запускаем бота.
CMD ["python", "bot.py"]