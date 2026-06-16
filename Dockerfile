FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .

# Создаем папку для данных
RUN mkdir -p /app/data

# Переменные окружения
ENV PORT=10000
ENV PYTHONUNBUFFERED=1

# Запускаем бота
CMD ["python", "bot.py"]
