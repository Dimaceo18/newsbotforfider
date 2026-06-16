FROM python:3.12-slim

WORKDIR /app

# Системные зависимости для lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Хранилище seen_news.json
RUN mkdir -p /app/data
ENV SEEN_FILE=/app/data/seen_news.json

CMD ["python", "-u", "bot.py"]
