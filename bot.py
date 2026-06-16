"""
Minsk News Bot — агрегатор новостей Минска для Telegram-канала.
Источники: RSS (Onliner, БелТА, SB.by), Google News RSS, Telegram-каналы (через парсинг).
Публикует каждый час, дедуплицирует по URL и заголовку.
"""

import asyncio
import logging
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Конфигурация ────────────────────────────────────────────────────────────

BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID  = os.getenv("CHANNEL_ID", "@your_channel_here")   # или числовой ID: -1001234567890
SEEN_FILE   = Path("seen_news.json")
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")

# RSS-ленты с новостями Минска / Беларуси
RSS_FEEDS = [
    {
        "name": "Onliner",
        "url": "https://www.onliner.by/feed",
        "keywords": ["минск", "беларус", "беларуь"],
    },
    {
        "name": "БелТА",
        "url": "https://www.belta.by/rss/news/",
        "keywords": [],   # всё из БелТА — про Беларусь
    },
    {
        "name": "СБ.Беларусь Сегодня",
        "url": "https://www.sb.by/rss.xml",
        "keywords": [],
    },
    {
        "name": "Зеркало",
        "url": "https://mirror-media.com/feed/",
        "keywords": ["минск", "беларус"],
    },
    {
        "name": "Google News — Минск",
        "url": "https://news.google.com/rss/search?q=Минск&hl=ru&gl=BY&ceid=BY:ru",
        "keywords": [],
    },
    {
        "name": "Google News — Беларусь",
        "url": "https://news.google.com/rss/search?q=Беларусь+новости&hl=ru&gl=BY&ceid=BY:ru",
        "keywords": [],
    },
]

# Публичные Telegram-каналы для парсинга через t.me/s/
TELEGRAM_CHANNELS = [
    {"name": "Onliner.by",    "handle": "onliner_news"},
    {"name": "Мой Минск",     "handle": "moy_minsk"},
    {"name": "Беларусь сегодня", "handle": "sb_by_news"},
]

MAX_AGE_HOURS   = 2      # игнорировать новости старше N часов
MAX_PER_RUN     = 10     # не более N постов за одну итерацию
FETCH_TIMEOUT   = 15     # секунды на HTTP-запрос

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL),
)
log = logging.getLogger("minsk-news-bot")

# ─── Хранилище уже опубликованных новостей ───────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(
        json.dumps(list(seen)[-5000:]),   # храним не более 5 000 хешей
        encoding="utf-8",
    )

def news_id(title: str, url: str) -> str:
    """Стабильный ID новости — хеш заголовка + URL."""
    key = f"{title.strip().lower()}|{url.strip()}"
    return hashlib.md5(key.encode()).hexdigest()

# ─── Парсинг RSS ──────────────────────────────────────────────────────────────

async def fetch_rss(feed_cfg: dict, seen: set, cutoff: datetime) -> list[dict]:
    results = []
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(feed_cfg["url"])
            resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
    except Exception as e:
        log.warning("RSS %s: ошибка получения: %s", feed_cfg["name"], e)
        return []

    keywords = [k.lower() for k in feed_cfg.get("keywords", [])]

    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        url   = entry.get("link", "").strip()
        if not title or not url:
            continue

        # Фильтр по ключевым словам
        if keywords:
            text = (title + " " + entry.get("summary", "")).lower()
            if not any(kw in text for kw in keywords):
                continue

        # Фильтр по времени
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue

        nid = news_id(title, url)
        if nid in seen:
            continue

        results.append({
            "id":      nid,
            "title":   title,
            "url":     url,
            "source":  feed_cfg["name"],
            "summary": _clean(entry.get("summary", "")),
            "pub_dt":  pub_dt if pub else datetime.now(timezone.utc),
        })

    log.info("RSS %s: найдено %d новых записей", feed_cfg["name"], len(results))
    return results

# ─── Парсинг Telegram-каналов через t.me/s/ ───────────────────────────────────

async def fetch_telegram_channel(ch: dict, seen: set, cutoff: datetime) -> list[dict]:
    url = f"https://t.me/s/{ch['handle']}"
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True,
                                      headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        log.warning("TG-парсинг %s: %s", ch["handle"], e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for msg in soup.select(".tgme_widget_message"):
        # Текст сообщения
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(" ", strip=True)
        if not text or len(text) < 30:
            continue

        # Ссылка на пост
        link_el = msg.select_one("a.tgme_widget_message_date")
        post_url = link_el["href"] if link_el else url

        # Время публикации
        time_el = msg.select_one("time")
        pub_dt = datetime.now(timezone.utc)
        if time_el and time_el.get("datetime"):
            try:
                pub_dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
            except Exception:
                pass

        if pub_dt < cutoff:
            continue

        title = text[:120] + ("…" if len(text) > 120 else "")
        nid   = news_id(title, post_url)
        if nid in seen:
            continue

        results.append({
            "id":      nid,
            "title":   title,
            "url":     post_url,
            "source":  ch["name"],
            "summary": "",
            "pub_dt":  pub_dt,
        })

    log.info("TG %s: найдено %d новых постов", ch["handle"], len(results))
    return results

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _clean(html: str) -> str:
    """Удалить HTML-теги и лишние пробелы."""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300] + ("…" if len(text) > 300 else "")

def _escape(text: str) -> str:
    """Экранировать спецсимволы для MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def format_post(item: dict) -> str:
    """Форматировать новость для Telegram (MarkdownV2)."""
    title   = _escape(item["title"])
    source  = _escape(item["source"])
    url     = item["url"]
    summary = _escape(item["summary"]) if item["summary"] else ""
    time_s  = item["pub_dt"].strftime("%H:%M")

    lines = [f"📰 *{title}*"]
    if summary:
        lines.append(f"\n{summary}")
    lines.append(f"\n🔗 [Читать]({url})  •  🏙 {source}  •  🕐 {time_s}")
    return "\n".join(lines)

# ─── Основная задача ──────────────────────────────────────────────────────────

async def collect_and_publish():
    log.info("=== Начинаю сбор новостей ===")
    seen   = load_seen()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    bot    = Bot(token=BOT_TOKEN)

    # Параллельный сбор из всех источников
    tasks = []
    for feed in RSS_FEEDS:
        tasks.append(fetch_rss(feed, seen, cutoff))
    for ch in TELEGRAM_CHANNELS:
        tasks.append(fetch_telegram_channel(ch, seen, cutoff))

    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    items = []
    for r in all_results:
        if isinstance(r, list):
            items.extend(r)

    # Сортировка: самые свежие вперёд, ограничение за один запуск
    items.sort(key=lambda x: x["pub_dt"], reverse=True)
    items = items[:MAX_PER_RUN]

    if not items:
        log.info("Новых новостей нет.")
        return

    log.info("Публикую %d новостей в канал %s", len(items), CHANNEL_ID)

    published = 0
    for item in items:
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=format_post(item),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=False,
            )
            seen.add(item["id"])
            published += 1
            await asyncio.sleep(2)   # пауза между постами (лимиты Telegram)
        except TelegramError as e:
            log.error("Ошибка публикации '%s': %s", item["title"][:60], e)

    save_seen(seen)
    log.info("Опубликовано: %d  |  Всего в базе: %d", published, len(seen))

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    log.info("Minsk News Bot запущен. Канал: %s", CHANNEL_ID)

    # Первый запуск сразу
    await collect_and_publish()

    # Расписание: каждый час
    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    scheduler.add_job(collect_and_publish, "interval", hours=1)
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
