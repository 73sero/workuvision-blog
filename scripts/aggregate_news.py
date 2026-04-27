"""News-Aggregator: Sammelt FC-Bayern-News aus RSS-Feeds, schickt sie an Claude
und schreibt 1-3 frische Artikel in content.json.

Voraussetzung: GitHub Secret ANTHROPIC_API_KEY

Verhalten:
- Maximal 1 neuer Artikel pro Lauf (alle 6h Lauf = max 4/Tag)
- Existierende Artikel bleiben unangetastet
- Älteste Artikel werden aus content.json entfernt, wenn >30 Stück
- Quelle wird IMMER verlinkt (Urheberrecht)
- Kein 1:1-Kopieren — Claude schreibt im Workuvision-Stil
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser

try:
    from anthropic import Anthropic
except ImportError:
    print("❌ 'anthropic' nicht installiert. pip install anthropic")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
if not API_KEY:
    print("⚠️  ANTHROPIC_API_KEY nicht gesetzt — Skip News-Aggregator.")
    sys.exit(0)

# RSS-Feeds für FC-Bayern-News
FEEDS = [
    ("kicker FC Bayern", "https://newsfeed.kicker.de/team/fcbayernmuenchen"),
    ("Bavarian Football Works", "https://www.bavarianfootballworks.com/rss/index.xml"),
    ("Sport1 Topnews", "https://www.sport1.de/news.rss"),
]

# Bilder-Pool für Artikel-Thumbnails (vorhanden in /img)
IMAGE_POOL = {
    "tactics": ["img/tactics.jpg", "img/stadium1.jpg", "img/football.jpg"],
    "transfer": ["img/transfer.jpg", "img/night.jpg", "img/football.jpg"],
    "reaction": ["img/fans.jpg", "img/stadium1.jpg", "img/hero.jpg"],
}

BADGE_MAP = {
    "tactics": ("Taktik-Analyse", "bt"),
    "transfer": ("Gerüchteküche", "bx"),
    "reaction": ("Reaktion", "brc"),
    "preview": ("Vorschau", "bv"),
}


def slugify(s, max_len=50):
    s = s.lower()
    s = re.sub(r"[äöüß]", lambda m: {"ä":"ae","ö":"oe","ü":"ue","ß":"ss"}[m.group()], s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].strip("-")


def load_content():
    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_content(data):
    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_feed_items():
    items = []
    for name, url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"⚠️  Feed {name} ({url}) nicht ladbar: {e}")
            continue
        for entry in feed.entries[:8]:
            title = entry.get("title", "").strip()
            if not title:
                continue
            # Nur Bayern-relevante Items (für Sport1: filtern)
            if "Sport1" in name or "Topnews" in name:
                if "bayern" not in (title + " " + entry.get("summary", "")).lower():
                    continue
            items.append({
                "source": name,
                "url": entry.get("link", ""),
                "title": title,
                "summary": re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:600].strip(),
                "published": entry.get("published", ""),
            })
    return items


def is_already_covered(items, articles):
    """Filtert RSS-Items raus, deren Kernthemen bereits im content.json sind."""
    existing_keywords = set()
    for a in articles[-15:]:  # letzte 15 Artikel checken
        words = re.findall(r"\b[A-Z][a-zäöü]{4,}\b", a.get("title", ""))
        existing_keywords.update(w.lower() for w in words)
    fresh = []
    for item in items:
        words = re.findall(r"\b[A-Z][a-zäöü]{4,}\b", item["title"])
        overlap = sum(1 for w in words if w.lower() in existing_keywords)
        # Wenn weniger als 50% der wichtigen Wörter schon abgedeckt → frisch
        if not words or overlap / len(words) < 0.5:
            fresh.append(item)
    return fresh


def write_with_claude(items, recent_articles):
    """Lässt Claude einen einzigen frischen Artikel im Workuvision-Stil schreiben."""
    client = Anthropic(api_key=API_KEY)

    items_str = "\n\n".join([
        f"### {it['title']}\nQuelle: {it['source']} ({it['url']})\nZusammenfassung: {it['summary']}"
        for it in items[:8]
    ])

    recent_titles = "\n".join(f"- {a['title']}" for a in recent_articles[-10:])

    system = """Du bist Abdel Worku, FC-Bayern-Content-Creator hinter dem Blog Workuvision.de.
Stil: locker, ehrlich, eigene Meinung, Fan-Perspektive, kein Clickbait, kein Boulevard-Geschwätz.
Keine Anglizismen, wo's nicht muss. „Mia san Mia" ist okay, aber sparsam.
Du schreibst auf Deutsch.

REGELN:
- WICHTIG: Du wählst aus den Vorschlägen GENAU EINE Story, die am interessantesten ist und nicht zu nah an den letzten Artikeln liegt.
- Du schreibst NIEMALS Sätze 1:1 aus den RSS-Quellen ab — du formulierst alles in deinen eigenen Worten.
- Maximal 250 Wörter im Body. Drei kurze Absätze.
- Du nennst die Quelle namentlich („laut kicker", „die Gazzetta dello Sport berichtet…").
- Kategorie wählst du aus: tactics (Taktik/Spielanalyse), transfer (Gerüchte/Wechsel), reaction (Spielbericht/Emotion).
- Kein Clickbait-Titel. Klar und faktisch.

OUTPUT-FORMAT: ausschließlich gültiges JSON, kein Markdown drumrum, keine Erklärung. Schema:
{
  "title": "Maximal 65 Zeichen, prägnant",
  "category": "tactics" | "transfer" | "reaction",
  "excerpt": "Ein bis zwei Sätze als Anriss, max. 200 Zeichen",
  "body": "Drei Absätze, getrennt durch \\n\\n. Eigene Meinung okay.",
  "sourceUrl": "URL der Original-Quelle aus den Vorschlägen",
  "sourceName": "Name der Quelle, z.B. 'kicker' oder 'Bavarian Football Works'"
}"""

    user = f"""Letzte 10 Artikel auf Workuvision (NICHT wiederholen!):
{recent_titles}

Aktuelle News-Vorschläge zum Auswählen:
{items_str}

Wähle EINEN, schreibe deinen Take. Reines JSON zurück."""

    print("→ Claude API Call…")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    # JSON-Block extrahieren (manchmal kommen Code-Fences)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def main():
    print("=== Workuvision Auto-News ===")
    items = fetch_feed_items()
    print(f"✓ {len(items)} Items aus {len(FEEDS)} Feeds.")

    if not items:
        print("Nichts zum Verarbeiten.")
        sys.exit(0)

    content = load_content()
    articles = content.get("articles", [])

    fresh_items = is_already_covered(items, articles)
    print(f"✓ {len(fresh_items)} frische Items (nach Dedup-Check).")

    if not fresh_items:
        print("Keine ausreichend frischen Themen — nichts neu.")
        sys.exit(0)

    try:
        new_post = write_with_claude(fresh_items, articles)
    except Exception as e:
        print(f"❌ Claude-Aufruf fehlgeschlagen: {e}")
        sys.exit(0)

    cat = new_post.get("category", "tactics")
    if cat not in BADGE_MAP:
        cat = "tactics"
    badge, badge_color = BADGE_MAP[cat]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = new_post.get("title", "Workuvision Take")[:90]
    slug = f"{today}-{slugify(title)}"

    # Bild aus Pool
    pool = IMAGE_POOL.get(cat, IMAGE_POOL["tactics"])
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    thumbnail = pool[h % len(pool)]

    article = {
        "slug": slug,
        "date": today,
        "category": cat,
        "badge": badge,
        "badgeColor": badge_color,
        "thumbnail": thumbnail,
        "thumbnailAlt": title,
        "readtime": "5 Min.",
        "featured": False,
        "title": title,
        "excerpt": new_post.get("excerpt", "")[:250],
        "body": new_post.get("body", ""),
        "sourceUrl": new_post.get("sourceUrl", ""),
        "sourceName": new_post.get("sourceName", ""),
    }

    # Vorne anhängen, max. 30 Artikel total
    articles.insert(0, article)
    articles = articles[:30]

    # Dedup nach slug (für den Fall dass am gleichen Tag mehrmals läuft mit gleichem Titel)
    seen = set()
    unique = []
    for a in articles:
        if a["slug"] not in seen:
            seen.add(a["slug"])
            unique.append(a)

    content["articles"] = unique
    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_content(content)

    print(f"✓ Neuer Artikel: {article['title']}")
    print(f"   Slug: {slug}")
    print(f"   Quelle: {article['sourceName']} ({article['sourceUrl']})")


if __name__ == "__main__":
    main()
