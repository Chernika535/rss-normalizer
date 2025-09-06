import os
import re
import time
import hashlib
import mimetypes
from datetime import datetime, timezone
from typing import Optional, Tuple
import requests
import feedparser
from bs4 import BeautifulSoup
from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from urllib.parse import urljoin, urlparse
from lxml import etree
import bleach

APP_TITLE = "RSS Normalizer for Dzen & Telegram"
SOURCE_FEED_URL = os.getenv("SOURCE_FEED_URL", "https://neiromantra.ru/12583-feed.xml")
SITE_BASE = os.getenv("SITE_BASE", "https://neiromantra.ru/")
FEED_TITLE = os.getenv("FEED_TITLE", "Neiromantra — нормализованный RSS")
FEED_LINK = os.getenv("FEED_LINK", "https://neiromantra.ru/")
FEED_DESCRIPTION = os.getenv("FEED_DESCRIPTION", "Нормализованная лента для автопостинга в Дзен и Телеграм")
TELEGRAM_MAX = int(os.getenv("TELEGRAM_MAX", "4096"))  # лимит сообщений
CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))  # секунды

app = FastAPI(title=APP_TITLE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET"], allow_headers=["*"],
)

_cache = {"t": 0, "zen": b"", "tg": b""}

# Разрешённый HTML для Телеграма (под parse_mode=HTML у ботов)
TG_ALLOWED_TAGS = [
    "b", "strong", "i", "em", "u", "s", "del", "code", "pre", "a", "br"
]
TG_ALLOWED_ATTRS = {"a": ["href"]}

# Умеренный набор для Дзен (в фиде кладём в yandex:full-text)
ZEN_ALLOWED_TAGS = [
    "p", "br", "ul", "ol", "li", "blockquote",
    "b", "strong", "i", "em", "u", "s", "del", "code", "pre",
    "h2", "h3", "h4", "img", "a", "figure", "figcaption"
]
ZEN_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title", "width", "height"]
}

def http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": "RSS-Normalizer/1.0 (+https://github.com/)"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

def absolutize_url(src: str, base: str) -> str:
    try:
        return urljoin(base, src)
    except Exception:
        return src

def extract_main_html(entry) -> Tuple[str, Optional[str]]:
    html = ""
    base = entry.get("link") or SITE_BASE
    # 1) content:encoded
    if "content" in entry and entry.content:
        for c in entry.content:
            if c.get("type", "").startswith("text/html"):
                html = c.get("value") or ""
                break
    # 2) summary/detail
    if not html:
        html = entry.get("summary", "") or entry.get("description", "") or ""
    # 3) добираем первую картинку
    first_img = None
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    if img and img.get("src"):
        first_img = absolutize_url(img["src"], base)
    # Абсолютные ссылки
    for tag in soup.find_all("a", href=True):
        tag["href"] = absolutize_url(tag["href"], base)
    for tag in soup.find_all("img", src=True):
        tag["src"] = absolutize_url(tag["src"], base)
    return str(soup), first_img

def sanitize_for_tg(html: str) -> str:
    cleaned = bleach.clean(html, tags=TG_ALLOWED_TAGS, attributes=TG_ALLOWED_ATTRS, strip=True)
    # Telegram не любит лишние пустые теги и много &nbsp;
    cleaned = re.sub(r"\s+",&nbsp_replacer, cleaned)
    cleaned = re.sub(r"(?:\s*<br\s*/?>\s*){3,}", "<br><br>", cleaned)
    return cleaned

def &nbsp_replacer(match):
    s = match.group(0)
    return " "

def sanitize_for_zen(html: str) -> str:
    cleaned = bleach.clean(html, tags=ZEN_ALLOWED_TAGS, attributes=ZEN_ALLOWED_ATTRS, strip=True)
    # Убедимся, что картинки абсолютные и без data:
    soup = BeautifulSoup(cleaned, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("data:"):
            img.decompose()
    return str(soup)

def pick_enclosure(entry, html_first_img: Optional[str]) -> Optional[Tuple[str, str]]:
    # 1) enclosure в ленте
    if "enclosures" in entry and entry.enclosures:
        enc = entry.enclosures[0]
        href = enc.get("href") or enc.get("url")
        if href:
            href = absolutize_url(href, entry.get("link") or SITE_BASE)
            t = enc.get("type") or mimetypes.guess_type(href)[0] or "image/jpeg"
            return href, t
    # 2) медиа из контента
    if html_first_img:
        mime = mimetypes.guess_type(html_first_img)[0] or "image/jpeg"
        return html_first_img, mime
    return None

def dt_to_rfc822(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def safe_pubdate(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def guid_for(entry) -> str:
    g = entry.get("id") or entry.get("guid") or entry.get("link") or (entry.get("title","") + str(entry.get("published","")))
    return hashlib.sha256(g.encode("utf-8", "ignore")).hexdigest()

def fetch_source_feed():
    r = http_get(SOURCE_FEED_URL)
    fp = feedparser.parse(r.content)
    if fp.bozo and not fp.entries:
        raise HTTPException(502, f"Не удалось распарсить исходный RSS: {fp.bozo_exception}")
    return fp

def build_zen_xml(fp) -> bytes:
    NSMAP = {
        None: "http://backend.userland.com/rss2",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom": "http://www.w3.org/2005/Atom",
        "yandex": "http://news.yandex.ru",
        "media": "http://search.yahoo.com/mrss/"
    }
    rss = etree.Element("rss", nsmap=NSMAP, version="2.0")
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = FEED_TITLE
    etree.SubElement(channel, "link").text = FEED_LINK
    etree.SubElement(channel, "description").text = FEED_DESCRIPTION
    atom_link = etree.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    atom_link.set("href", FEED_LINK.rstrip("/") + "/zen.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for e in fp.entries:
        item = etree.SubElement(channel, "item")
        title = e.get("title") or "Без названия"
        link = e.get("link") or FEED_LINK
        etree.SubElement(item, "title").text = title
        etree.SubElement(item, "link").text = link
        etree.SubElement(item, "guid").text = guid_for(e)
        etree.SubElement(item, "pubDate").text = dt_to_rfc822(safe_pubdate(e))

        raw_html, first_img = extract_main_html(e)
        full_html = sanitize_for_zen(raw_html)

        # content:encoded
        cencoded = etree.SubElement(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        cencoded.text = etree.CDATA(full_html)

        # yandex:full-text
        yfull = etree.SubElement(item, "{http://news.yandex.ru}full-text")
        yfull.text = etree.CDATA(full_html)

        # media и enclosure
        enc = pick_enclosure(e, first_img)
        if enc:
            url, mime = enc
            enclosure = etree.SubElement(item, "enclosure")
            enclosure.set("url", url)
            enclosure.set("type", mime)
            mcontent = etree.SubElement(item, "{http://search.yahoo.com/mrss/}content")
            mcontent.set("url", url)
            mcontent.set("type", mime)

        # категория/автор по возможности
        if e.get("author"):
            etree.SubElement(item, "author").text = e.get("author")
        for tag in e.get("tags", [])[:10]:
            term = tag.get("term")
            if term:
                etree.SubElement(item, "category").text = term

        # короткое описание из первого абзаца
        soup = BeautifulSoup(full_html, "html.parser")
        p = soup.find("p")
        if p:
            etree.SubElement(item, "description").text = p.get_text(" ", strip=True)[:500]

    return etree.tostring(rss, encoding="utf-8", xml_declaration=True, pretty_print=True)

def chunk_plain_text(s: str, maxlen: int) -> str:
    # Для RSS description Телеграма мы делаем один блок <= maxlen.
    if len(s) <= maxlen:
        return s
    # Обрезаем по слову и добавляем многоточие
    cut = s[:maxlen-1]
    cut = re.sub(r"\s+\S*$", "", cut).strip()
    return cut + "…"

def build_tg_xml(fp) -> bytes:
    # Простой валидный RSS 2.0; description очищен и ужат для безопасного автопостинга
    rss = etree.Element("rss", version="2.0")
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = FEED_TITLE + " — Telegram"
    etree.SubElement(channel, "link").text = FEED_LINK
    etree.SubElement(channel, "description").text = "Лента, подготовленная для автопостинга в Телеграм"

    for e in fp.entries:
        item = etree.SubElement(channel, "item")
        title = e.get("title") or "Без названия"
        link = e.get("link") or FEED_LINK
        etree.SubElement(item, "title").text = title
        etree.SubElement(item, "link").text = link
        etree.SubElement(item, "guid").text = guid_for(e)
        etree.SubElement(item, "pubDate").text = dt_to_rfc822(safe_pubdate(e))

        raw_html, _ = extract_main_html(e)
        safe_html = sanitize_for_tg(raw_html)

        # Telegram-интеграции часто игнорируют HTML в <description>, но многие боты его поддерживают.
        # Одновременно добавим <description> с plain-text.
        plain = BeautifulSoup(safe_html, "html.parser").get_text("\n", strip=True)
        desc_text = chunk_plain_text(plain, TELEGRAM_MAX)
        etree.SubElement(item, "description").text = desc_text

    return etree.tostring(rss, encoding="utf-8", xml_declaration=True, pretty_print=True)

def maybe_refresh_cache(force: bool = False):
    now = time.time()
    if not force and (now - _cache["t"] < CACHE_TTL) and _cache["zen"] and _cache["tg"]:
        return
    fp = fetch_source_feed()
    _cache["zen"] = build_zen_xml(fp)
    _cache["tg"] = build_tg_xml(fp)
    _cache["t"] = now

@app.get("/health")
def health():
    return {"ok": True, "source": SOURCE_FEED_URL, "updated": _cache["t"]}

@app.get("/zen.xml")
def zen_feed():
    try:
        maybe_refresh_cache()
        return Response(content=_cache["zen"], media_type="application/rss+xml; charset=utf-8")
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации Дзен RSS: {e}")

@app.get("/telegram.xml")
def telegram_feed():
    try:
        maybe_refresh_cache()
        return Response(content=_cache["tg"], media_type="application/rss+xml; charset=utf-8")
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации Telegram RSS: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=False)
