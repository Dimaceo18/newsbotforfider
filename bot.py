"""
Minsk News Bot PRO — агрегатор новостей с полным контентом
Поддерживает: изображения, видео, полный текст статьи
Управление источниками через веб-интерфейс
"""

import asyncio
import logging
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Optional, List, Dict, Any

import feedparser
import httpx
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, request, jsonify, render_template_string

# ─── Конфигурация ────────────────────────────────────────────────────────────

BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID  = os.getenv("CHANNEL_ID", "@your_channel_here")
SEEN_FILE   = Path("seen_news.json")
SOURCES_FILE = Path("sources.json")
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")
WEB_PORT    = int(os.getenv("PORT", 10000))

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL),
)
log = logging.getLogger("minsk-news-bot")

# ─── Управление источниками ──────────────────────────────────────────────────

def load_sources() -> Dict[str, Any]:
    """Загружает источники из файла или создает дефолтные"""
    if SOURCES_FILE.exists():
        try:
            data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
            # Проверяем структуру
            if "rss" in data and "telegram" in data:
                return data
        except Exception:
            pass
    
    # Дефолтные источники
    default_sources = {
        "rss": [
            {"name": "Onliner", "url": "https://www.onliner.by/feed", "keywords": ["минск", "беларус"], "enabled": True},
            {"name": "БелТА", "url": "https://www.belta.by/rss/news/", "keywords": [], "enabled": True},
            {"name": "СБ.Беларусь Сегодня", "url": "https://www.sb.by/rss.xml", "keywords": [], "enabled": True},
            {"name": "Зеркало", "url": "https://mirror-media.com/feed/", "keywords": ["минск"], "enabled": True},
        ],
        "telegram": [
            {"name": "Onliner.by", "handle": "onliner_news", "enabled": True},
            {"name": "Мой Минск", "handle": "moy_minsk", "enabled": True},
        ]
    }
    save_sources(default_sources)
    return default_sources

def save_sources(sources: Dict[str, Any]):
    """Сохраняет источники в файл"""
    SOURCES_FILE.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")

# ─── Парсинг статьи ──────────────────────────────────────────────────────────

async def fetch_article_content(url: str) -> Dict[str, Any]:
    """
    Парсит полное содержимое статьи:
    - Заголовок
    - Текст
    - Изображения
    - Видео
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
    except Exception as e:
        log.warning("Не удалось загрузить статью %s: %s", url, e)
        return {"title": "", "text": "", "images": [], "videos": []}

    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Удаляем скрипты и стили
    for script in soup(["script", "style", "noscript"]):
        script.decompose()
    
    # Заголовок
    title = ""
    title_tag = soup.find("h1") or soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    
    # Основной текст
    text_parts = []
    content_selectors = [
        "article", ".article__content", ".news-text", ".content", 
        ".post-content", ".entry-content", "main", ".text"
    ]
    
    for selector in content_selectors:
        content = soup.select_one(selector)
        if content:
            # Собираем текст из параграфов
            for p in content.find_all(["p", "div"], recursive=False):
                p_text = p.get_text(strip=True)
                if len(p_text) > 50:  # Фильтруем короткие
                    text_parts.append(p_text)
            break
    
    # Если не нашли по селекторам - берем все параграфы
    if not text_parts:
        for p in soup.find_all("p"):
            p_text = p.get_text(strip=True)
            if len(p_text) > 50:
                text_parts.append(p_text)
    
    full_text = "\n\n".join(text_parts[:15])  # Ограничиваем 15 абзацев
    
    # Изображения
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and not src.startswith("data:"):
            # Делаем полный URL
            if not src.startswith("http"):
                src = urljoin(url, src)
            # Фильтруем по размеру
            width = img.get("width")
            if width and int(width) < 100:
                continue
            images.append(src)
    
    # Берем только первые 5 изображений
    images = images[:5]
    
    # Видео
    videos = []
    for video in soup.find_all("video"):
        src = video.get("src")
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = urljoin(url, src)
            videos.append(src)
    
    # YouTube видео
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if "youtube.com/embed" in src or "youtu.be" in src:
            videos.append(src)
    
    return {
        "title": title or "Статья",
        "text": full_text,
        "images": images,
        "videos": videos[:3]  # максимум 3 видео
    }

# ─── Парсинг RSS с полным контентом ─────────────────────────────────────────

async def fetch_rss_full(feed_cfg: dict, seen: set, cutoff: datetime) -> list[dict]:
    """Парсит RSS с полным разбором каждой статьи"""
    results = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(feed_cfg["url"])
            resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
    except Exception as e:
        log.warning("RSS %s: ошибка получения: %s", feed_cfg["name"], e)
        return []

    keywords = [k.lower() for k in feed_cfg.get("keywords", [])]

    for entry in parsed.entries[:15]:  # Ограничиваем 15 записей за раз
        title = entry.get("title", "").strip()
        url = entry.get("link", "").strip()
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

        # Парсим полное содержимое статьи
        log.info("Парсинг статьи: %s", title[:50])
        content = await fetch_article_content(url)
        
        # Если нет текста, используем summary из RSS
        if not content["text"]:
            content["text"] = _clean(entry.get("summary", ""))

        results.append({
            "id": nid,
            "title": content["title"] or title,
            "url": url,
            "source": feed_cfg["name"],
            "text": content["text"],
            "images": content["images"],
            "videos": content["videos"],
            "pub_dt": pub_dt if pub else datetime.now(timezone.utc),
        })

    log.info("RSS %s: найдено %d новых статей", feed_cfg["name"], len(results))
    return results

# ─── Парсинг Telegram с полным контентом ────────────────────────────────────

async def fetch_telegram_full(ch: dict, seen: set, cutoff: datetime) -> list[dict]:
    """Парсит Telegram каналы с полным содержимым"""
    url = f"https://t.me/s/{ch['handle']}"
    results = []
    
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        log.warning("TG-парсинг %s: %s", ch["handle"], e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    
    for msg in soup.select(".tgme_widget_message")[:20]:  # Ограничиваем
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        
        text = text_el.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue

        link_el = msg.select_one("a.tgme_widget_message_date")
        post_url = link_el["href"] if link_el else url

        # Время
        time_el = msg.select_one("time")
        pub_dt = datetime.now(timezone.utc)
        if time_el and time_el.get("datetime"):
            try:
                pub_dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
            except Exception:
                pass

        if pub_dt < cutoff:
            continue

        # Изображения
        images = []
        for img in msg.select(".tgme_widget_message_photo img"):
            src = img.get("src")
            if src:
                images.append(src)
        
        # Видео
        videos = []
        for video in msg.select(".tgme_widget_message_video video"):
            src = video.get("src")
            if src:
                videos.append(src)

        nid = news_id(text[:100], post_url)
        if nid in seen:
            continue

        results.append({
            "id": nid,
            "title": text[:120] + ("…" if len(text) > 120 else ""),
            "url": post_url,
            "source": ch["name"],
            "text": text,
            "images": images,
            "videos": videos,
            "pub_dt": pub_dt,
        })

    log.info("TG %s: найдено %d новых постов", ch["handle"], len(results))
    return results

# ─── Форматирование поста с медиа ──────────────────────────────────────────

def format_post_with_media(item: dict) -> tuple:
    """
    Создает пост с медиа (фото/видео) или текстовый пост
    Возвращает: (media_group, caption, is_media)
    """
    title = _escape(item["title"])
    source = _escape(item["source"])
    text = _escape(item["text"]) if item["text"] else ""
    url = item["url"]
    
    # Ограничиваем текст для подписи
    max_len = 900
    if text:
        text = text[:max_len] + ("…" if len(text) > max_len else "")
    
    # Формируем подпись
    caption_parts = [f"📰 *{title}*"]
    if text:
        caption_parts.append(f"\n\n{text}")
    caption_parts.append(f"\n\n🔗 [Читать полностью]({url})")
    caption_parts.append(f"\n\n🏙 *Источник:* {source}")
    
    caption = "".join(caption_parts)
    
    # Проверяем наличие медиа
    images = item.get("images", [])
    videos = item.get("videos", [])
    
    if images:
        # Альбом из фото
        media_group = []
        for i, img_url in enumerate(images[:5]):  # максимум 5 фото
            if i == 0:
                media_group.append(InputMediaPhoto(media=img_url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
            else:
                media_group.append(InputMediaPhoto(media=img_url))
        return media_group, caption, True
    elif videos:
        # Видео
        video_url = videos[0]
        return [InputMediaVideo(media=video_url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)], caption, True
    else:
        # Только текст
        return caption, caption, False

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _clean(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500] + ("…" if len(text) > 500 else "")

def _escape(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2"""
    if not text:
        return ""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(
        json.dumps(list(seen)[-5000:]),
        encoding="utf-8",
    )

def news_id(title: str, url: str) -> str:
    key = f"{title.strip().lower()}|{url.strip()}"
    return hashlib.md5(key.encode()).hexdigest()

# ─── Основная задача ──────────────────────────────────────────────────────────

async def collect_and_publish():
    log.info("=== Начинаю сбор новостей ===")
    seen = load_seen()
    sources = load_sources()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    bot = Bot(token=BOT_TOKEN)

    tasks = []
    
    # RSS источники
    for feed in sources.get("rss", []):
        if feed.get("enabled", True):
            tasks.append(fetch_rss_full(feed, seen, cutoff))
    
    # Telegram каналы
    for ch in sources.get("telegram", []):
        if ch.get("enabled", True):
            tasks.append(fetch_telegram_full(ch, seen, cutoff))

    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    items = []
    for r in all_results:
        if isinstance(r, list):
            items.extend(r)

    items.sort(key=lambda x: x["pub_dt"], reverse=True)
    items = items[:10]  # максимум 10 постов

    if not items:
        log.info("Новых новостей нет.")
        return

    log.info("Публикую %d новостей в канал %s", len(items), CHANNEL_ID)

    published = 0
    for item in items:
        try:
            media, caption, is_media = format_post_with_media(item)
            
            if is_media and isinstance(media, list):
                # Отправка медиа-группы (фото/видео)
                await bot.send_media_group(
                    chat_id=CHANNEL_ID,
                    media=media
                )
                # Отправляем ссылку отдельно если это не первое фото
                if len(media) > 1:
                    await bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=f"🔗 [Читать полностью]({item['url']})",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
            else:
                # Текстовый пост
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=media,  # caption в текстовом режиме
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=False,
                )
            
            seen.add(item["id"])
            published += 1
            await asyncio.sleep(2)  # пауза между постами
            
        except TelegramError as e:
            log.error("Ошибка публикации '%s': %s", item["title"][:60], e)
            # Пробуем отправить без медиа
            try:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"📰 *{_escape(item['title'])}*\n\n🔗 [Читать]({item['url']})",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except:
                pass

    save_seen(seen)
    log.info("Опубликовано: %d  |  Всего в базе: %d", published, len(seen))

# ─── Веб-интерфейс управления источниками ──────────────────────────────────

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Управление источниками новостей</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               background: #f5f7fa; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { color: #1a1a2e; margin-bottom: 20px; }
        .card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; 
                box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        .card h2 { color: #2d3436; font-size: 18px; margin-bottom: 15px; 
                   border-bottom: 2px solid #e8f0fe; padding-bottom: 10px; }
        .source-item { display: flex; align-items: center; gap: 15px; padding: 10px 0; 
                       border-bottom: 1px solid #f0f0f0; flex-wrap: wrap; }
        .source-item:last-child { border-bottom: none; }
        .source-info { flex: 1; min-width: 200px; }
        .source-name { font-weight: 600; color: #2d3436; }
        .source-url { font-size: 13px; color: #636e72; word-break: break-all; }
        .source-status { display: flex; align-items: center; gap: 10px; }
        .btn { padding: 6px 16px; border: none; border-radius: 6px; cursor: pointer; 
               font-weight: 500; transition: all 0.2s; }
        .btn-toggle { background: #e8f0fe; color: #1a73e8; }
        .btn-toggle:hover { background: #d2e3fc; }
        .btn-toggle.active { background: #e8f5e9; color: #2e7d32; }
        .btn-delete { background: #fce4ec; color: #c62828; }
        .btn-delete:hover { background: #f8bbd0; }
        .btn-add { background: #1a73e8; color: white; padding: 8px 24px; }
        .btn-add:hover { background: #1557b0; }
        .form-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 15px; }
        .form-row input, .form-row select { padding: 8px 12px; border: 1px solid #ddd; 
                                            border-radius: 6px; flex: 1; min-width: 150px; }
        .form-row input:focus, .form-row select:focus { outline: none; border-color: #1a73e8; }
        .empty { color: #636e72; text-align: center; padding: 20px; }
        .badge { background: #e8f5e9; color: #2e7d32; padding: 2px 10px; border-radius: 12px; 
                 font-size: 12px; }
        .badge.disabled { background: #fce4ec; color: #c62828; }
        .message { padding: 12px; border-radius: 8px; margin-bottom: 15px; }
        .message.success { background: #e8f5e9; color: #2e7d32; }
        .message.error { background: #fce4ec; color: #c62828; }
        .actions { display: flex; gap: 8px; flex-wrap: wrap; }
        @media (max-width: 600px) {
            .source-item { flex-direction: column; align-items: stretch; }
            .form-row { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📰 Управление источниками новостей</h1>
        
        <div id="message"></div>
        
        <!-- RSS Источники -->
        <div class="card">
            <h2>📡 RSS-ленты</h2>
            <div id="rss-list">
                {% for source in sources.rss %}
                <div class="source-item" data-id="{{ loop.index0 }}">
                    <div class="source-info">
                        <div class="source-name">{{ source.name }}</div>
                        <div class="source-url">{{ source.url }}</div>
                        {% if source.keywords %}
                        <div style="font-size:12px;color:#636e72;margin-top:4px;">
                            Ключевые слова: {{ source.keywords|join(', ') }}
                        </div>
                        {% endif %}
                    </div>
                    <div class="source-status">
                        <span class="badge {{ 'disabled' if not source.enabled else '' }}">
                            {{ 'Включен' if source.enabled else 'Отключен' }}
                        </span>
                        <div class="actions">
                            <button class="btn btn-toggle {{ 'active' if source.enabled else '' }}" 
                                    onclick="toggleSource('rss', {{ loop.index0 }})">
                                {{ 'Выключить' if source.enabled else 'Включить' }}
                            </button>
                            <button class="btn btn-delete" onclick="deleteSource('rss', {{ loop.index0 }})">
                                ✕
                            </button>
                        </div>
                    </div>
                </div>
                {% else %}
                <div class="empty">Нет RSS-источников</div>
                {% endfor %}
            </div>
            
            <div class="form-row">
                <input type="text" id="rss-name" placeholder="Название источника">
                <input type="text" id="rss-url" placeholder="URL RSS-ленты">
                <input type="text" id="rss-keywords" placeholder="Ключевые слова (через запятую)">
                <button class="btn btn-add" onclick="addSource('rss')">➕ Добавить</button>
            </div>
        </div>
        
        <!-- Telegram каналы -->
        <div class="card">
            <h2>📱 Telegram каналы</h2>
            <div id="telegram-list">
                {% for source in sources.telegram %}
                <div class="source-item" data-id="{{ loop.index0 }}">
                    <div class="source-info">
                        <div class="source-name">{{ source.name }}</div>
                        <div class="source-url">@{{ source.handle }}</div>
                    </div>
                    <div class="source-status">
                        <span class="badge {{ 'disabled' if not source.enabled else '' }}">
                            {{ 'Включен' if source.enabled else 'Отключен' }}
                        </span>
                        <div class="actions">
                            <button class="btn btn-toggle {{ 'active' if source.enabled else '' }}" 
                                    onclick="toggleSource('telegram', {{ loop.index0 }})">
                                {{ 'Выключить' if source.enabled else 'Включить' }}
                            </button>
                            <button class="btn btn-delete" onclick="deleteSource('telegram', {{ loop.index0 }})">
                                ✕
                            </button>
                        </div>
                    </div>
                </div>
                {% else %}
                <div class="empty">Нет Telegram-каналов</div>
                {% endfor %}
            </div>
            
            <div class="form-row">
                <input type="text" id="tg-name" placeholder="Название канала">
                <input type="text" id="tg-handle" placeholder="Username (без @)">
                <button class="btn btn-add" onclick="addSource('telegram')">➕ Добавить</button>
            </div>
        </div>
    </div>
    
    <script>
        async function apiCall(endpoint, method, data) {
            const response = await fetch(endpoint, {
                method: method,
                headers: {'Content-Type': 'application/json'},
                body: data ? JSON.stringify(data) : undefined
            });
            return await response.json();
        }
        
        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.className = `message ${type}`;
            msg.textContent = text;
            setTimeout(() => msg.textContent = '', 5000);
        }
        
        async function toggleSource(type, index) {
            const result = await apiCall('/toggle', 'POST', {type, index});
            if (result.success) {
                location.reload();
            } else {
                showMessage('Ошибка: ' + result.error, 'error');
            }
        }
        
        async function deleteSource(type, index) {
            if (!confirm('Удалить этот источник?')) return;
            const result = await apiCall('/delete', 'POST', {type, index});
            if (result.success) {
                location.reload();
            } else {
                showMessage('Ошибка: ' + result.error, 'error');
            }
        }
        
        async function addSource(type) {
            let data = {type};
            if (type === 'rss') {
                data.name = document.getElementById('rss-name').value;
                data.url = document.getElementById('rss-url').value;
                data.keywords = document.getElementById('rss-keywords').value.split(',').map(k => k.trim()).filter(k => k);
                if (!data.name || !data.url) {
                    showMessage('Заполните название и URL', 'error');
                    return;
                }
            } else {
                data.name = document.getElementById('tg-name').value;
                data.handle = document.getElementById('tg-handle').value;
                if (!data.name || !data.handle) {
                    showMessage('Заполните название и username', 'error');
                    return;
                }
            }
            
            const result = await apiCall('/add', 'POST', data);
            if (result.success) {
                location.reload();
            } else {
                showMessage('Ошибка: ' + result.error, 'error');
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    sources = load_sources()
    return render_template_string(HTML_TEMPLATE, sources=sources)

@app.route('/toggle', methods=['POST'])
def toggle_source():
    data = request.json
    sources = load_sources()
    type_key = data['type']
    index = data['index']
    
    try:
        sources[type_key][index]['enabled'] = not sources[type_key][index]['enabled']
        save_sources(sources)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete', methods=['POST'])
def delete_source():
    data = request.json
    sources = load_sources()
    type_key = data['type']
    index = data['index']
    
    try:
        sources[type_key].pop(index)
        save_sources(sources)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/add', methods=['POST'])
def add_source():
    data = request.json
    sources = load_sources()
    type_key = data['type']
    
    try:
        if type_key == 'rss':
            sources['rss'].append({
                'name': data['name'],
                'url': data['url'],
                'keywords': data.get('keywords', []),
                'enabled': True
            })
        else:
            sources['telegram'].append({
                'name': data['name'],
                'handle': data['handle'],
                'enabled': True
            })
        save_sources(sources)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/health')
def health():
    return 'OK', 200

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    log.info("Minsk News Bot PRO запущен")
    log.info("Канал: %s", CHANNEL_ID)
    
    # Запускаем Flask в отдельном потоке
    import threading
    def run_flask():
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
    
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Веб-интерфейс: http://0.0.0.0:%d", WEB_PORT)
    
    # Первый запуск
    await collect_and_publish()
    
    # Расписание
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
