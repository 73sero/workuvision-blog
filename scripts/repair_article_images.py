"""Einmalige Reparatur: lädt für existierende Artikel passende Bilder von ihren
Original-Quellen nach.

Strategie:
1. Gehe alle Artikel in content.json durch
2. Wenn der Artikel ein 'sourceUrl'-Feld hat:
   - Suche dieses Item nochmal in den aktuellen RSS-Feeds
   - Wenn gefunden → Bild laden, optimieren, speichern
3. Wenn der Artikel KEIN sourceUrl hat (manuell erstellte Artikel):
   - Versuche, das Original-Bild über die sourceUrl direkt zu laden
   - Wenn das nicht klappt → behalte das Stock-Bild, aber wechsle zu einem
     weniger benutzten aus dem Pool (für mehr Abwechslung)
4. Aktualisiert content.json mit den neuen Thumbnail-Pfaden

Aufruf: python scripts/repair_article_images.py

KEIN automatischer Workflow-Bestandteil — wird manuell ausgeführt.
"""
import io
import json
import random
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("❌ Pillow fehlt. pip install Pillow")
    sys.exit(1)

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"
ARTICLE_IMG_DIR = ROOT / "img" / "articles"

# Gleiche Feeds wie aggregate_news.py — wir brauchen die zur Item-Suche
FEEDS = [
    ("Bavarian Football Works", "https://www.bavarianfootballworks.com/rss/index.xml"),
    ("Miasanrot (Fanblog)",     "https://miasanrot.de/feed/"),
    ("kicker News",             "https://newsfeed.kicker.de/news/aktuell"),
    ("Tagesschau Sport",        "https://www.tagesschau.de/sport/index~rss2.xml"),
    ("Spiegel Sport",           "https://www.spiegel.de/sport/index.rss"),
    ("FAZ Sport",               "https://www.faz.net/rss/aktuell/sport/"),
    ("ZEIT Sport",              "https://newsfeed.zeit.de/sport/index"),
    ("NTV Sport",               "https://www.n-tv.de/sport/rss"),
    ("Süddeutsche Sport",       "https://rss.sueddeutsche.de/rss/Sport"),
]

UA = "Mozilla/5.0 (compatible; Workuvision-Bot/1.0)"

# Erweiterter Stock-Pool für besseren Fallback
STOCK_POOL = {
    "tactics":  ["img/tactics.jpg", "img/stadium1.jpg", "img/football.jpg",
                 "img/hero.jpg", "img/night.jpg"],
    "transfer": ["img/transfer.jpg", "img/night.jpg", "img/football.jpg",
                 "img/hero.jpg", "img/stadium1.jpg"],
    "reaction": ["img/fans.jpg", "img/stadium1.jpg", "img/hero.jpg",
                 "img/football.jpg", "img/transfer.jpg"],
}


def extract_image_from_entry(entry):
    """Sucht in einem feedparser-Entry nach einer Bild-URL."""
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    if hasattr(entry, "media_content") and entry.media_content:
        for mc in entry.media_content:
            url = mc.get("url")
            if url and not url.endswith(".mp4"):
                return url
    if hasattr(entry, "links"):
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    summary = entry.get("summary", "") + entry.get("description", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', summary)
    if m:
        return m.group(1)
    return None


def fetch_html(url, timeout=15):
    """Holt HTML einer Seite (für Open-Graph-Image-Extraktion)."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = r.read()
        if content[:2] == b'\x1f\x8b':
            import gzip
            content = gzip.decompress(content)
        return content.decode("utf-8", errors="replace")


def find_og_image(article_url):
    """Holt die Originalseite und extrahiert <meta property=og:image>."""
    if not article_url:
        return None
    try:
        html = fetch_html(article_url)
    except Exception as e:
        print(f"     ⚠️  Seite nicht ladbar: {e}")
        return None
    # og:image
    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'<meta\s+content="([^"]+)"\s+property="og:image"', html)
    if m:
        return m.group(1)
    # Twitter Card als Fallback
    m = re.search(r'<meta\s+name="twitter:image"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)
    return None


def download_and_optimize_image(image_url, slug, target_dir):
    """Lädt ein Bild und schneidet auf 1200×675 als JPEG zurecht."""
    if not image_url:
        return None
    target_path = target_dir / f"{slug}.jpg"
    try:
        req = urllib.request.Request(image_url, headers={
            "User-Agent": UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://workuvision.de/",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 2000:
            return None
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        target_w, target_h = 1200, 675
        src_w, src_h = img.size
        if src_w / src_h > target_w / target_h:
            new_h = target_h
            new_w = int(src_w * (target_h / src_h))
        else:
            new_w = target_w
            new_h = int(src_h * (target_w / src_w))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        target_dir.mkdir(parents=True, exist_ok=True)
        img.save(target_path, "JPEG", quality=82, optimize=True)
        return f"img/articles/{slug}.jpg"
    except Exception as e:
        print(f"     ❌ Download fehlgeschlagen: {e}")
        return None


def find_in_feeds(article_url, article_title):
    """Sucht in den aktuellen RSS-Feeds nach dem Artikel anhand URL oder Titel."""
    title_words = set(re.findall(r"\w{4,}", article_title.lower()))
    for source_name, feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue
        for entry in feed.entries[:30]:
            entry_url = entry.get("link", "")
            entry_title = entry.get("title", "")
            # URL-Match
            if article_url and article_url == entry_url:
                return entry, source_name
            # Title-Word-Overlap
            entry_words = set(re.findall(r"\w{4,}", entry_title.lower()))
            if title_words and len(title_words & entry_words) >= 3:
                return entry, source_name
    return None, None


def pick_diverse_stock(category, used_so_far):
    """Wählt Stock-Bild mit Vermeidung der bereits oft benutzten."""
    pool = STOCK_POOL.get(category, STOCK_POOL["tactics"])
    counts = Counter(used_so_far)
    # Sortiere Pool nach Häufigkeit (selten benutzte zuerst)
    weighted = sorted(pool, key=lambda p: counts.get(p, 0))
    # Wähle aus den seltensten 50%
    cutoff = max(1, len(weighted) // 2)
    return random.choice(weighted[:cutoff])


def main():
    if not CONTENT_FILE.exists():
        print("Keine content.json — abgebrochen.")
        return

    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        content = json.load(f)

    articles = content.get("articles", [])
    if not articles:
        print("Keine Artikel zum Reparieren.")
        return

    print(f"=== Bild-Reparatur für {len(articles)} Artikel ===\n")

    ARTICLE_IMG_DIR.mkdir(parents=True, exist_ok=True)

    used_thumbs = []  # für Stock-Diversität
    repaired = 0
    rss_hits = 0
    og_hits = 0
    stock_changed = 0

    for i, article in enumerate(articles, 1):
        title = article.get("title", "?")[:60]
        current_thumb = article.get("thumbnail", "")
        slug = article.get("slug", f"article-{i}")
        cat = article.get("category", "tactics")

        print(f"[{i}/{len(articles)}] {title}")
        print(f"    Aktuell: {current_thumb}")

        # Wenn schon ein lokales /articles/-Bild existiert, überspringen
        if current_thumb.startswith("img/articles/"):
            print(f"    ✓ schon lokales Bild — skip")
            used_thumbs.append(current_thumb)
            continue

        new_thumb = None
        source_url = article.get("sourceUrl", "")

        # 1) Versuch: Item in aktuellen RSS-Feeds finden + Bild davon
        if source_url or article.get("title"):
            entry, source = find_in_feeds(source_url, article.get("title", ""))
            if entry:
                img_url = extract_image_from_entry(entry)
                if img_url:
                    print(f"    🔍 RSS-Match in {source} — Bild gefunden")
                    new_thumb = download_and_optimize_image(img_url, slug, ARTICLE_IMG_DIR)
                    if new_thumb:
                        rss_hits += 1
                        print(f"    ✅ RSS-Bild gespeichert")

        # 2) Fallback: Open-Graph-Image direkt von der sourceUrl holen
        if not new_thumb and source_url:
            print(f"    🔍 Versuche og:image von Original-Seite")
            og_url = find_og_image(source_url)
            if og_url:
                new_thumb = download_and_optimize_image(og_url, slug, ARTICLE_IMG_DIR)
                if new_thumb:
                    og_hits += 1
                    print(f"    ✅ og:image gespeichert")

        # 3) Letzter Fallback: Stock-Bild mit besserer Verteilung
        if not new_thumb:
            new_thumb = pick_diverse_stock(cat, used_thumbs)
            if new_thumb != current_thumb:
                stock_changed += 1
                print(f"    🔄 Stock-Bild gewechselt: {current_thumb} → {new_thumb}")
            else:
                print(f"    ✓ Stock bleibt: {new_thumb}")

        article["thumbnail"] = new_thumb
        used_thumbs.append(new_thumb)
        repaired += 1
        print()

    # content.json speichern
    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    print(f"=== Zusammenfassung ===")
    print(f"  Bearbeitete Artikel: {repaired}")
    print(f"  RSS-Match-Treffer:    {rss_hits}")
    print(f"  og:image-Treffer:     {og_hits}")
    print(f"  Stock-Bilder neu:     {stock_changed}")
    print(f"  Total mit RSS-Bild:   {rss_hits + og_hits}")

    final_thumbs = Counter(a.get("thumbnail") for a in articles)
    print(f"\n=== Bild-Verteilung nach Reparatur ===")
    for t, n in final_thumbs.most_common():
        marker = "📷" if t.startswith("img/articles/") else "  "
        print(f"  {marker} {t}: {n}x")


if __name__ == "__main__":
    main()
