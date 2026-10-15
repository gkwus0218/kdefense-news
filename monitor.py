#!/usr/bin/env python3
"""Foreign-media K-defense news -> Telegram; public metadata only, no paywall bypass."""
import argparse
import hashlib
import html
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger("kdefense")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/articles.db")
POLL_MINUTES = max(5, int(os.getenv("POLL_MINUTES", "15")))
LOOKBACK_HOURS = max(1, min(48, int(os.getenv("LOOKBACK_HOURS", "6"))))
SEED_ON_FIRST_RUN = os.getenv("SEED_ON_FIRST_RUN", "true").lower() == "true"
ENABLE_GOOGLE_RSS = os.getenv("ENABLE_GOOGLE_RSS", "true").lower() == "true"
MIN_SCORE = int(os.getenv("MIN_SCORE", "3"))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KDefenseMonitor/1.0 (+personal-research-news-alerts)"})
TIMEOUT = 20

# Non-exclusive source targets: foreign general news and specialist defense press.
SOURCES = {
    "reuters.com", "bloomberg.com", "ft.com", "nikkei.com", "asia.nikkei.com",
    "apnews.com", "bbc.com", "theguardian.com", "economist.com", "wsj.com",
    "straitstimes.com", "scmp.com", "japantimes.co.jp",
    "defensenews.com", "breakingdefense.com", "janes.com", "defenseone.com",
    "defence24.pl", "rzeczpospolita.pl", "wnp.pl", "infodefensa.com",
    "edrmagazine.eu", "navalnews.com", "armyrecognition.com", "flightglobal.com",
    "thedefensepost.com", "defence-blog.com", "asiapacificdefencereporter.com.au",
}
# Targeted searches keep GDELT requests small and more robust.
QUERIES = [
    '("Hanwha Aerospace" OR "Hanwha Ocean" OR "Hyundai Rotem" OR "Korea Aerospace Industries")',
    '("LIG Nex1" OR "KAI" OR "HD Hyundai Heavy Industries") defense',
    '("K2 Black Panther" OR "K9 Thunder" OR "FA-50" OR "KF-21")',
    '("South Korea" OR "Korean") ("arms export" OR "defense contract" OR "weapons deal")',
    '("Hanhwa" OR "Hanwha" OR "Hyundai Rotem") (Poland OR Europe OR export)',
]
RSS_QUERIES = [
    '"Hanwha Aerospace" OR "Hanwha Ocean" OR "Hyundai Rotem"',
    '"Korea Aerospace Industries" OR "LIG Nex1" OR "KF-21"',
    '"K9 Thunder" OR "K2 Black Panther" OR "FA-50"',
    '"South Korea" "defense exports"',
]
# Multilingual company/platform anchors, scoped to defense context or strong brand/platform mentions.
STRONG = [
    r'hanwha\s+(?:aerospace|ocean|systems)', r'hyundai\s+rotem',
    r'korea\s+aerospace\s+industries', r'lig\s+nex1', r'lig\s+d\s*&\s*a',
    r'k2\s*(?:black\s*panther|tank|pl)', r'k9\s*(?:thunder|howitzer|pl)',
    r'kf[ -]?21', r'fa[ -]?50', r'cheongung', r'cheon\s*gung',
    r'chunmoo', r'biho', r'redback', r'k239',
    r'한화에어로', r'한화오션', r'현대로템', r'한국항공우주', r'엘아이지넥스원',
    r'czołg\s+k2', r'armatohaubic\w*\s+k9',
]
WEAK = [r'south\s+korea', r'korean', r'koreańsk\w*', r'corean\w*', r'sudcorean\w*', r'한국', r'대한민국']
DEFENSE = [
    r'defen[cs]e', r'military', r'armed\s+forces', r'weapon', r'howitzer',
    r'armament', r'fighter\s+jet', r'frigate', r'destroyer', r'naval',
    r'missile', r'tank', r'artillery', r'procurement', r'arms\s+deal',
    r'export', r'defensa', r'défense', r'obron\w*', r'wojsk\w*',
    r'czołg', r'rakiet\w*', r'uzbrojen\w*', r'방산', r'방위', r'수출', r'무기',
]
BAD = [r'k-pop', r'k-drama', r'football', r'baseball', r'concert', r'celebrity']

def matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)

def relevance(text):
    """Conservative title/snippet keyword heuristic, not an AI semantic classifier."""
    if not text or matches(BAD, text):
        return 0
    strong = matches(STRONG, text)
    weak = matches(WEAK, text)
    defense = matches(DEFENSE, text)
    # Established unambiguous Korean defense firm or weapon system.
    if strong:
        return 5 if defense else 3
    return 3 if weak and defense else 0

def normalized_url(url):
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.")
    filtered = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", host, path, "", urlencode(filtered), ""))

def is_approved_source(domain):
    domain = domain.lower().removeprefix("www.")
    return any(domain == d or domain.endswith("." + d) for d in SOURCES)

def parse_dt(value):
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z?", value):
            return datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (ValueError, TypeError, IndexError):
        return None

def from_gdelt():
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    found = []
    for q in QUERIES:
        try:
            r = SESSION.get(endpoint, params={"query": q, "mode": "artlist", "format": "json", "sort": "datedesc", "timespan": f"{LOOKBACK_HOURS}h", "maxrecords": 100}, timeout=TIMEOUT)
            r.raise_for_status()
            for a in r.json().get("articles", []):
                url = a.get("url", "")
                domain = a.get("domain") or urlparse(url).netloc
                if not url.startswith("https://") and not url.startswith("http://"):
                    continue
                if not is_approved_source(domain):
                    continue
                found.append({"title": a.get("title", ""), "url": url, "domain": domain, "published": a.get("seendate", ""), "description": "", "origin": "GDELT"})
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("GDELT query failed: %s (%s)", q[:45], exc)
        time.sleep(1.0)
    return found

def from_google_rss():
    results = []
    for query in RSS_QUERIES:
        try:
            resp = SESSION.get("https://news.google.com/rss/search", params={"q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                source = entry.get("source", {})
                source_url = source.get("href", "") if isinstance(source, dict) else ""
                source_domain = urlparse(source_url).netloc
                if not is_approved_source(source_domain):
                    continue
                title = entry.get("title", "")
                source_title = source.get("title", "") if isinstance(source, dict) else ""
                if source_title and title.endswith(" - " + source_title):
                    title = title[: -(len(source_title) + 3)]
                results.append({"title": title, "url": entry.get("link", ""), "domain": source_domain, "published": entry.get("published", ""), "description": re.sub(r"<[^>]*>", " ", entry.get("summary", "")), "origin": "Google News RSS"})
        except requests.RequestException as exc:
            LOG.warning("RSS query failed: %s", exc)
        time.sleep(.5)
    return results

def setup_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, created_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    return db

def article_id(article):
    # Duplicate URLs, feed source redirects, and most cross-source same-title matches.
    title = re.sub(r'[^\w]+', ' ', article['title'].casefold()).strip()
    if title:
        return hashlib.sha256(title.encode()).hexdigest()
    return hashlib.sha256(normalized_url(article['url']).encode()).hexdigest()

def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID or BOT_TOKEN == "replace_me" or CHAT_ID == "replace_me":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = SESSION.post(api, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message: " + str(result.get("description")))

def get_chat_id():
    if not BOT_TOKEN or BOT_TOKEN == "replace_me":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env first")
    resp = SESSION.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"timeout": 0}, timeout=TIMEOUT)
    resp.raise_for_status()
    items = resp.json().get("result", [])
    ids = list(dict.fromkeys(str(i.get("message", i.get("channel_post", {})).get("chat", {}).get("id", "")) for i in items))
    ids = [i for i in ids if i]
    print("Found chat IDs:", ", ".join(ids) if ids else "none; open bot and send /start then retry")

def run_once(db, force_send=False):
    articles = from_gdelt()
    if ENABLE_GOOGLE_RSS:
        articles.extend(from_google_rss())
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(hours=LOOKBACK_HOURS)
    candidates = {}
    for article in articles:
        if not article['url'] or not article['title']:
            continue
        date = parse_dt(article.get('published'))
        if date and (date < earliest or date > now + timedelta(minutes=15)):
            continue
        score = relevance(article['title'] + ' ' + article.get('description', ''))
        if score < MIN_SCORE:
            continue
        key = article_id(article)
        if key not in candidates or (article['origin'] == 'GDELT' and candidates[key]['origin'] != 'GDELT'):
            candidates[key] = article
    candidates = list(candidates.items())
    # Only mark first run as seeded if at least one provider actually returned articles.
    first = db.execute("SELECT value FROM meta WHERE name='initialized'").fetchone() is None
    if not articles:
        LOG.warning("No results returned by providers. Skipping initialization; retry later.")
        return
    if first and SEED_ON_FIRST_RUN and not force_send:
        for key, article in candidates:
            db.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?, ?)", (key, article['title'], article['url'], now.isoformat()))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized','1')")
        db.commit()
        LOG.info("First run: seeded %d matches without alerts", len(candidates))
        return
    delivered = 0
    for key, article in candidates:
        if db.execute("SELECT 1 FROM sent WHERE id=?", (key,)).fetchone():
            continue
        safe_title = html.escape(article['title'][:300])
        safe_domain = html.escape(article['domain'][:120])
        # Google RSS links can redirect to publisher pages.
        safe_url = html.escape(article['url'], quote=True)
        msg = f"🛡 <b>해외 한국 방산 뉴스</b>\n\n<b>{safe_title}</b>\n출처: {safe_domain}\n발견 경로: {html.escape(article['origin'])}\n<a href=\"{safe_url}\">원문 기사 보기</a>"
        try:
            telegram_send(msg)
        except (requests.RequestException, RuntimeError) as exc:
            LOG.error("Telegram delivery failed; will retry next cycle: %s", exc)
            continue
        db.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?, ?)", (key, article['title'], article['url'], now.isoformat()))
        db.commit()
        delivered += 1
        time.sleep(.3)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized','1')")
    db.commit()
    LOG.info("Fetched %d raw, %d relevant unique, %d notifications sent", len(articles), len(candidates), delivered)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--loop', action='store_true', help='run continuously')
    p.add_argument('--test-telegram', action='store_true')
    p.add_argument('--get-chat-id', action='store_true')
    p.add_argument('--send-first-run', action='store_true', help='send matching historical articles on first run')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.get_chat_id:
        get_chat_id()
        return
    if args.test_telegram:
        telegram_send('✅ K-Defense Monitor 텔레그램 연동 성공!')
        print('Test message sent.')
        return
    db = setup_db()
    if args.loop:
        LOG.info('Monitoring every %s minutes', POLL_MINUTES)
        while True:
            try:
                run_once(db, force_send=args.send_first_run)
            except Exception:
                LOG.exception('Unexpected cycle error')
            time.sleep(POLL_MINUTES * 60)
    else:
        run_once(db, force_send=args.send_first_run)

if __name__ == '__main__':
    main()
