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
# Disabled by default: GDELT DOC frequently rate-limits shared GitHub runner IPs.
ENABLE_GDELT = os.getenv("ENABLE_GDELT", "false").lower() == "true"
ENABLE_DIRECT_RSS = os.getenv("ENABLE_DIRECT_RSS", "true").lower() == "true"
MIN_SCORE = int(os.getenv("MIN_SCORE", "3"))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KDefenseMonitor/1.0 (+personal-research-news-alerts)"})
TIMEOUT = 20

# Non-exclusive source targets: foreign general news and specialist defense press.
SOURCES = {
    # International newspapers and wire services.
    "reuters.com", "bloomberg.com", "ft.com", "nikkei.com", "asia.nikkei.com",
    "apnews.com", "bbc.com", "bbc.co.uk", "theguardian.com", "economist.com",
    "wsj.com", "nytimes.com", "washingtonpost.com", "politico.com",
    "straitstimes.com", "scmp.com", "japantimes.co.jp", "thejakartapost.com",
    "inquirer.net", "philstar.com", "rappler.com", "bangkokpost.com",
    "thestar.com.my", "nst.com.my", "daily-sun.com", "dailysabah.com",
    "aa.com.tr", "aljazeera.com", "dw.com", "france24.com", "lemonde.fr",
    "thehindu.com", "hindustantimes.com", "economictimes.indiatimes.com",
    "english.news.cn", "businessinsider.com", "abc.net.au", "smh.com.au",
    # Defense and aerospace specialist outlets.
    "defensenews.com", "breakingdefense.com", "janes.com", "defenseone.com",
    "defence24.pl", "defence24.com", "rzeczpospolita.pl", "rp.pl", "wnp.pl",
    "infodefensa.com", "edrmagazine.eu", "navalnews.com", "armyrecognition.com",
    "flightglobal.com", "thedefensepost.com", "defence-blog.com",
    "asiapacificdefencereporter.com.au", "defence-industry.eu", "milmag.pl",
    "naval-technology.com", "army-technology.com", "defenceweb.co.za",
    "military.com", "thedrive.com", "defensemirror.com", "defence-ua.com",
    "euro-sd.com", "defenceconnect.com.au", "defense-aerospace.com",
    "aviationweek.com", "airforce-technology.com", "breakingdefense.com",
}

# Only use this mapping if the Google News RSS record explicitly supplies this
# publisher name. Never infer the publisher from its Google News redirect URL.
SOURCE_ALIASES = {
    "reuters": "reuters.com", "bloomberg": "bloomberg.com",
    "financial times": "ft.com", "nikkei asia": "asia.nikkei.com",
    "nikkei": "nikkei.com", "associated press": "apnews.com",
    "ap news": "apnews.com", "bbc": "bbc.com", "the guardian": "theguardian.com",
    "the wall street journal": "wsj.com", "the new york times": "nytimes.com",
    "the washington post": "washingtonpost.com", "politico": "politico.com",
    "the straits times": "straitstimes.com", "south china morning post": "scmp.com",
    "the japan times": "japantimes.co.jp", "the jakarta post": "thejakartapost.com",
    "the philippine star": "philstar.com", "inquirer.net": "inquirer.net",
    "the bangkok post": "bangkokpost.com", "the star": "thestar.com.my",
    "defense news": "defensenews.com", "defence news": "defensenews.com",
    "breaking defense": "breakingdefense.com", "janes": "janes.com",
    "janes defence": "janes.com", "defense one": "defenseone.com",
    "defence24": "defence24.pl", "rzeczpospolita": "rzeczpospolita.pl",
    "infodefensa": "infodefensa.com", "naval news": "navalnews.com",
    "army recognition": "armyrecognition.com", "flightglobal": "flightglobal.com",
    "the defense post": "thedefensepost.com", "defence blog": "defence-blog.com",
    "the war zone": "thedrive.com", "aviation week": "aviationweek.com",
    "defence industry europe": "defence-industry.eu",
}

# Targeted searches keep GDELT requests small and more robust.
QUERIES = [
    '("Hanwha Aerospace" OR "Hanwha Ocean" OR "Hyundai Rotem" OR "Korea Aerospace Industries")',
    '("LIG Nex1" OR "KAI" OR "HD Hyundai Heavy Industries") defense',
    '("K2 Black Panther" OR "K9 Thunder" OR "FA-50" OR "KF-21")',
    '("South Korea" OR "Korean") ("arms export" OR "defense contract" OR "weapons deal")',
    '("Hanhwa" OR "Hanwha" OR "Hyundai Rotem") (Poland OR Europe OR export)',
]
# Publisher-provided RSS endpoints. The previous armyrecognition.com/.../rss
# endpoint returned HTML and failed to parse on GitHub Actions.
DIRECT_RSS_FEEDS = [
    "https://breakingdefense.com/feed/",
    "https://www.navalnews.com/feed/",
    "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://www.bbc.co.uk/news/world/rss.xml",
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
    """Optional GDELT source; one 429 stops the batch to respect server throttling."""
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    found = []
    for i, q in enumerate(QUERIES, 1):
        try:
            r = SESSION.get(endpoint, params={"query": q, "mode": "artlist", "format": "json", "sort": "datedesc", "timespan": f"{LOOKBACK_HOURS}h", "maxrecords": 100}, timeout=TIMEOUT)
            if r.status_code == 429:
                LOG.warning("GDELT rate-limited (429). Stopping GDELT requests for this run.")
                break
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")
            try:
                payload = r.json()
            except ValueError:
                LOG.warning("GDELT returned non-JSON response for query %s (type=%s)", i, content_type)
                continue
            added = 0
            for a in payload.get("articles", []):
                url = a.get("url", "")
                domain = a.get("domain") or urlparse(url).netloc
                if not url.startswith(("https://", "http://")) or not is_approved_source(domain):
                    continue
                found.append({"title": a.get("title", ""), "url": url, "domain": domain, "published": a.get("seendate", ""), "description": "", "origin": "GDELT"})
                added += 1
            LOG.info("GDELT query %d: %d approved-source items", i, added)
        except requests.RequestException as exc:
            LOG.warning("GDELT query %d failed (%s)", i, type(exc).__name__)
        time.sleep(1.0)
    LOG.info("GDELT provider: %d articles accepted", len(found))
    return found


def parse_news_feed(content):
    feed = feedparser.parse(content)
    if not feed.entries and getattr(feed, "bozo", False):
        error = str(getattr(feed, "bozo_exception", "invalid feed"))[:160]
        raise ValueError(f"Invalid RSS/Atom feed: {error}")
    return feed


def google_publisher(entry):
    """Return (approved publisher domain, display title) or ('', title).

    Google RSS article links usually point to news.google.com and MUST NOT be
    treated as a publisher. Prefer its <source url='...'> field, then a
    curated exact-match fallback based on the publisher's displayed name.
    """
    source = entry.get("source") or {}
    name = html.unescape(str(source.get("title") or "")).strip()
    website = str(source.get("href") or source.get("url") or "")
    host = (urlparse(website).hostname or "").lower().removeprefix("www.")
    if is_approved_source(host):
        return host, name
    alias = SOURCE_ALIASES.get(re.sub(r"\s+", " ", name.casefold()))
    if alias:
        return alias, name
    return "", name


def from_google_rss():
    """Google News RSS: accept approved publisher metadata, not Google links."""
    results = []
    for i, query in enumerate(RSS_QUERIES, 1):
        try:
            resp = SESSION.get("https://news.google.com/rss/search", params={"q": query + " when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=TIMEOUT)
            resp.raise_for_status()
            feed = parse_news_feed(resp.content)
            accepted = 0
            rejected = []
            for entry in feed.entries:
                source_domain, source_title = google_publisher(entry)
                if not source_domain:
                    if len(rejected) < 6:
                        raw_source = entry.get("source") or {}
                        rejected.append(f"{source_title or '(no name)'} [{(raw_source.get('href') or '')[:65]}]")
                    continue
                title = entry.get("title", "")
                if source_title and title.endswith(" - " + source_title):
                    title = title[: -(len(source_title) + 3)]
                results.append({"title": title, "url": entry.get("link", ""), "domain": source_domain, "published": entry.get("published", ""), "description": "", "origin": "Google News RSS"})
                accepted += 1
            LOG.info("Google RSS query %d: %d feed items, %d approved-source items", i, len(feed.entries), accepted)
            if not accepted and rejected:
                LOG.warning("Google RSS query %d sample excluded publishers: %s", i, "; ".join(rejected))
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Google RSS query %d failed: %s: %s", i, type(exc).__name__, str(exc)[:140])
        time.sleep(.5)
    LOG.info("Google RSS provider: %d articles accepted", len(results))
    return results


def from_direct_rss():
    """A first-party publisher feed provides fallback when search aggregators throttle."""
    results = []
    for feed_url in DIRECT_RSS_FEEDS:
        domain = urlparse(feed_url).netloc
        if not is_approved_source(domain):
            continue
        try:
            resp = SESSION.get(feed_url, timeout=TIMEOUT)
            resp.raise_for_status()
            if resp.content[:400].lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise ValueError("Publisher returned an HTML web page, not an RSS feed")
            feed = parse_news_feed(resp.content)
            accepted = 0
            for entry in feed.entries[:100]:
                url = entry.get("link", "")
                if not url.startswith(("https://", "http://")):
                    continue
                results.append({"title": entry.get("title", ""), "url": url, "domain": domain, "published": entry.get("published", ""), "description": "", "origin": "Publisher RSS"})
                accepted += 1
            LOG.info("Publisher RSS %s: %d feed items, %d accepted", domain, len(feed.entries), accepted)
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Publisher RSS %s failed: %s: %s", domain, type(exc).__name__, str(exc)[:140])
    LOG.info("Publisher RSS provider: %d articles accepted", len(results))
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
    articles = []
    if ENABLE_GOOGLE_RSS:
        articles.extend(from_google_rss())
    if ENABLE_DIRECT_RSS:
        articles.extend(from_direct_rss())
    if ENABLE_GDELT:
        articles.extend(from_gdelt())
    LOG.info("Source collection completed: %d raw articles", len(articles))
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
    LOG.info("Relevant matches in last %s hours: %d", LOOKBACK_HOURS, len(candidates))
    # Only mark first run as seeded if at least one provider actually returned articles.
    first = db.execute("SELECT value FROM meta WHERE name='initialized'").fetchone() is None
    if not articles:
        LOG.error("No feed articles received: news monitoring is NOT healthy. Check the source logs above.")
        raise RuntimeError("All news sources returned 0 accepted feed articles")
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
