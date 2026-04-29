"""News-Aggregator v2 für Workuvision.

Sammelt FC-Bayern-News aus RSS-Feeds, lässt Claude einen sauberen, journalistischen
Artikel daraus schreiben, prüft auf typische Fehler (falsche Namen, Floskeln,
Halluzinationen) und committet erst, wenn alles passt.

Voraussetzung: GitHub Secret ANTHROPIC_API_KEY

Verbesserungen ggü. v1:
- Sonnet 4.6 statt Haiku (deutlich präziser bei Eigennamen)
- Bayern-Kader-Whitelist im Prompt → keine Halluzinationen
- Liste der Ex-Spieler, die NICHT erwähnt werden dürfen
- Forbidden-Phrases-Liste (Honestly, halt, Kragenweite, …)
- Korrekte Schreibweisen (Kompany mit K, nicht Company)
- Faktentreue: Nur Inhalt aus der Quelle, keine eigenen Zahlen
- Validation nach Generierung: Bei Fehlern → Artikel verworfen, kein Commit
"""
import hashlib
import io
import json
import os
import random
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import feedparser

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

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

# ============================================================================
# QUELLEN
# Drei Kategorien:
#   - "bayern_only":  100% Bayern-Inhalte, kein Filter
#   - "general":      allgemeine Sport-Feeds, gefiltert auf Bayern + Fußball
#   - "social":       Bluesky-Posts (kürzer, ohne klassischen Titel)
# ============================================================================
FEEDS = [
    # ── Bayern-Spezialisten (immer relevant) ──
    ("Bavarian Football Works", "https://www.bavarianfootballworks.com/rss/index.xml", "bayern_only"),
    ("Miasanrot (Fanblog)",     "https://miasanrot.de/feed/",                          "bayern_only"),

    # ── Bluesky: Insider und Journalisten ──
    # Christian Falk = DER deutsche Bayern-Insider (BILD), 100% Bayern-Posts
    ("Christian Falk (Bluesky)", "https://bsky.app/profile/cfbayern.bsky.social/rss",  "social_bayern"),
    # Fabrizio Romano = Welt-Transfers, gelegentlich Bayern, mit Filter
    ("Fabrizio Romano (Bluesky)", "https://bsky.app/profile/fabrizio-romano.bsky.social/rss", "social_general"),
    # kicker und Sport1 auf Bluesky — kommen schneller als auf Webseite
    ("kicker (Bluesky)",        "https://bsky.app/profile/kicker.de/rss",              "social_general"),
    ("Sport1 (Bluesky)",        "https://bsky.app/profile/sport1.de/rss",              "social_general"),

    # ── Großer deutscher Sport-Journalismus (klassische RSS, mit Bayern-Filter) ──
    ("kicker News",             "https://newsfeed.kicker.de/news/aktuell",             "general"),
    ("Tagesschau Sport",        "https://www.tagesschau.de/sport/index~rss2.xml",      "general"),
    ("Spiegel Sport",           "https://www.spiegel.de/sport/index.rss",              "general"),
    ("FAZ Sport",               "https://www.faz.net/rss/aktuell/sport/",              "general"),
    ("ZEIT Sport",              "https://newsfeed.zeit.de/sport/index",                "general"),
    ("NTV Sport",               "https://www.n-tv.de/sport/rss",                       "general"),
    ("Süddeutsche Sport",       "https://rss.sueddeutsche.de/rss/Sport",               "general"),
]

IMAGE_POOL = {
    "tactics": ["img/tactics.jpg", "img/stadium1.jpg", "img/football.jpg", "img/hero.jpg", "img/night.jpg"],
    "transfer": ["img/transfer.jpg", "img/night.jpg", "img/football.jpg", "img/hero.jpg", "img/stadium1.jpg"],
    "reaction": ["img/fans.jpg", "img/stadium1.jpg", "img/hero.jpg", "img/football.jpg", "img/transfer.jpg"],
}

ARTICLE_IMG_DIR = ROOT / "img" / "articles"


def pick_stock_image(category, articles):
    """Wählt das am wenigsten benutzte Stock-Bild aus dem Kategorie-Pool aus.
    So entstehen keine doppelten oder häufig wiederholten Stock-Bilder.
    """
    from collections import Counter
    pool = list(IMAGE_POOL.get(category, IMAGE_POOL["tactics"]))
    # Zähle wie oft jedes Stock-Bild aktuell genutzt wird (alle Kategorien)
    usage = Counter()
    for a in articles:
        thumb = a.get("thumbnail", "")
        if thumb and not thumb.startswith("img/articles/"):
            usage[thumb] += 1
    # Bevorzuge das Bild aus dem Pool das am seltensten genutzt wird;
    # bei Gleichstand zufällig wählen
    pool_with_counts = [(usage.get(p, 0), p) for p in pool]
    pool_with_counts.sort(key=lambda x: (x[0], random.random()))
    return pool_with_counts[0][1]

BADGE_MAP = {
    "tactics": ("Taktik-Analyse", "bt"),
    "transfer": ("Gerüchteküche", "bx"),
    "reaction": ("Reaktion", "brc"),
    "preview": ("Vorschau", "bv"),
}

# ============================================================================
# FAKTEN-WHITELIST: aktueller Bayern-Kader 2025/26
# ============================================================================
BAYERN_KADER = {
    "trainer": ["Vincent Kompany"],
    "torhueter": ["Manuel Neuer", "Sven Ulreich", "Jonas Urbig"],
    "abwehr": [
        "Dayot Upamecano", "Min-Jae Kim", "Jonathan Tah", "Hiroki Itō",
        "Sacha Boey", "Konrad Laimer", "Josip Stanišić", "Raphaël Guerreiro",
        "Alphonso Davies",
    ],
    "mittelfeld": [
        "Joshua Kimmich", "Aleksandar Pavlović", "Leon Goretzka",
        "Joao Palhinha", "Tom Bischof", "Serge Gnabry",
    ],
    "angriff": [
        "Harry Kane", "Michael Olise", "Jamal Musiala", "Kingsley Coman",
        "Luis Díaz", "Nicolas Jackson", "Lennart Karl",
    ],
}

# Ex-Spieler / Namen, die NICHT mehr im aktuellen Kader sind
EHEMALIGE_NICHT_NENNEN = [
    "Eric Maxim Choupo-Moting", "Choupo-Moting",
    "Leroy Sané",
    "Thomas Tuchel",
    "Niko Kovač",
    "Julian Nagelsmann",
    "Robert Lewandowski",
]

# Häufige Schreibfehler → korrekte Form
NAMENS_KORREKTUREN = {
    "Compay": "Kompany",
    "Company": "Kompany",
    "Kstark": "Karl",
    "Lennart Kstark": "Lennart Karl",
    "Pavlovic": "Pavlović",
    "Stanisic": "Stanišić",
    "Guerriero": "Guerreiro",
}

# Floskeln & Anglizismen, die als rote Flagge gelten
FORBIDDEN_PHRASES = [
    "honestly",
    "halt besonders",
    "halt eben",
    "Kragenweite",
    "Statement setzen",
    "wehzutun",
    "ein Statement",
    "auf einem anderen Level",
    "fühlt sich unfair an",
    "drängelt sich die Frage",
    "durchgesund",
    "Zähne zusammenbeißen",
    "Kracher gegen",
    "in aller Munde",
]

# ============================================================================
# HELFER
# ============================================================================

def slugify(s, max_len=50):
    s = s.lower()
    s = re.sub(r"[äöüß]", lambda m: {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}[m.group()], s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].strip("-")


def load_content():
    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_content(data):
    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_feed_items():
    """Sammelt Items aus allen Feeds.
    - 'bayern_only':    alles wird übernommen
    - 'social_bayern':  Bluesky von Bayern-Insidern, alles wird übernommen
    - 'general':        klassische Sport-Feeds, mit Bayern+Fußball-Filter
    - 'social_general': Bluesky-Posts mit Bayern+Fußball-Filter
    """
    items = []

    # Eindeutige Bayern-FC-Indikatoren
    bayern_strong = [
        "fc bayern", "fcb ", "fcb,", "fcb.", "bayern münchen", "bayern muenchen",
        "bayern munich", "rekordmeister", "an der säbener", "die münchner",
        "kompany", "harry kane", "musiala", "olise", "kimmich", "neuer",
        "upamecano", "pavlovi", "alphonso davies", "luis díaz", "luis diaz",
        "bayern-trainer", "bayern-coach", "bayern-stürmer", "bayern-keeper",
        "münchner trainer", "münchner star", "münchner abwehr",
        # Bluesky/Romano-spezifisch (englisch)
        "fc bayern's", "bayern's", "@fcbayern",
    ]
    bayern_weak = ["bayern"]
    football_terms = [
        "spiel", "sieg", "niederlage", "tor", "mannschaft", "bundesliga",
        "champions league", "halbfinale", "finale", "training", "transfer",
        "trainer", "stürmer", "keeper", "abwehr", "mittelfeld", "fußball",
        "fussball", "klub", "verein", "spieler", "kader",
        "match", "win", "loss", "goal", "team", "deal", "contract", "loan",
    ]

    for feed_def in FEEDS:
        if len(feed_def) == 2:
            name, url = feed_def
            ftype = "general"
        else:
            name, url, ftype = feed_def

        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"⚠️  Feed {name} ({url}) nicht ladbar: {e}")
            continue

        items_added = 0
        # Bluesky liefert oft viele kurze Posts, klassische Feeds wenige längere
        max_items = 30 if "social" in ftype else 12

        for entry in feed.entries[:max_items]:
            # Klassische Feeds: title + summary
            # Bluesky: title leer/automatisch, content im description/summary
            title = (entry.get("title") or "").strip()
            summary_raw = (entry.get("summary") or entry.get("description") or "").strip()
            summary_clean = re.sub(r"<[^>]+>", "", summary_raw)
            summary_clean = re.sub(r"\s+", " ", summary_clean).strip()[:800]

            # Für Bluesky: title ist oft leer oder generisch — verwende den Anfang des Posts
            if "social" in ftype:
                if not summary_clean:
                    continue
                # Wenn title fehlt oder generisch ist, nimm Anfang des Posts als "title"
                if not title or len(title) < 10:
                    title = summary_clean[:140].rstrip(".,!? ") + ("…" if len(summary_clean) > 140 else "")

            if not title and not summary_clean:
                continue

            # Filter
            if ftype in ("general", "social_general"):
                haystack = (title + " " + summary_clean).lower()
                strong_match = any(kw in haystack for kw in bayern_strong)
                weak_match = (any(kw in haystack for kw in bayern_weak) and
                              any(t in haystack for t in football_terms))
                if not (strong_match or weak_match):
                    continue

            # Bild-URL aus dem RSS-Entry extrahieren (verschiedene Felder probieren)
            image_url = extract_image_from_entry(entry)

            items.append({
                "source": name,
                "url": entry.get("link", ""),
                "title": title,
                "summary": summary_clean,
                "published": entry.get("published", ""),
                "is_social": "social" in ftype,
                "image_url": image_url,
            })
            items_added += 1

        if items_added:
            print(f"  + {items_added:>2d} aus {name}")

    return items


def extract_image_from_entry(entry):
    """Sucht in einem feedparser-Entry nach einer Bild-URL.
    Probiert: media_thumbnail, media_content, enclosures (links type=image),
    img-Tag im summary, og:image-artige URLs."""
    # 1) media_thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    # 2) media_content
    if hasattr(entry, "media_content") and entry.media_content:
        for mc in entry.media_content:
            url = mc.get("url")
            if url and not url.endswith(".mp4"):
                return url
    # 3) Enclosure-Links
    if hasattr(entry, "links"):
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                href = link.get("href")
                if href:
                    return href
    # 4) <img>-Tag im summary
    summary = entry.get("summary", "") + entry.get("description", "")
    if summary:
        m = re.search(r'<img[^>]+src="([^"]+)"', summary)
        if m:
            return m.group(1)
    return None


def fetch_og_image_from_url(article_url):
    """Lädt die Artikel-Seite und extrahiert og:image (oder twitter:image als Fallback).
    Wird als Fallback verwendet wenn der RSS-Feed kein Bild bereitstellt.
    Gibt None zurück bei Fehler oder wenn kein Bild gefunden.
    """
    if not article_url:
        return None
    try:
        req = urllib.request.Request(article_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            # Lese max 200KB — head/meta steht ganz oben
            html = r.read(200_000).decode("utf-8", errors="ignore")
        # 1) og:image
        m = re.search(
            r'<meta\s+(?:property|name)="og:image"[^>]*content="([^"]+)"',
            html, re.IGNORECASE
        )
        if not m:
            m = re.search(
                r'<meta\s+content="([^"]+)"[^>]*(?:property|name)="og:image"',
                html, re.IGNORECASE
            )
        # 2) twitter:image fallback
        if not m:
            m = re.search(
                r'<meta\s+(?:property|name)="twitter:image"[^>]*content="([^"]+)"',
                html, re.IGNORECASE
            )
        if m:
            url = m.group(1).strip()
            # HTML-Entity-Decoding (z.B. &amp; → &)
            url = url.replace("&amp;", "&").replace("&#x2F;", "/")
            if url.startswith("//"):
                url = "https:" + url
            return url
    except Exception as e:
        print(f"     og:image scrape fehlgeschlagen ({article_url[:60]}): {e}")
    return None


def download_and_optimize_image(image_url, slug, target_dir):
    """Lädt ein Bild herunter, skaliert es auf 1200x675 und speichert als JPEG.
    Gibt den lokalen Pfad zurück (relativ zum Repo) oder None bei Fehlern."""
    if not image_url or not PILLOW_AVAILABLE:
        return None
    target_path = target_dir / f"{slug}.jpg"
    try:
        # Mit Browser-Headers — manche Server blocken Default-User-Agent
        req = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Workuvision-Bot/1.0)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://workuvision.de/",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 2000:  # zu klein → vermutlich kein echtes Bild
            return None
        img = Image.open(io.BytesIO(data))
        # In RGB konvertieren (wegen PNG/RGBA und JPEG-Inkompatibilität)
        if img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        # Auf 1200×675 (16:9) zuschneiden — center-crop
        target_w, target_h = 1200, 675
        src_w, src_h = img.size
        if src_w / src_h > target_w / target_h:
            # zu breit, nach Höhe skalieren
            new_h = target_h
            new_w = int(src_w * (target_h / src_h))
        else:
            new_w = target_w
            new_h = int(src_h * (target_w / src_w))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        # Center crop
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        # Speichern
        target_dir.mkdir(parents=True, exist_ok=True)
        img.save(target_path, "JPEG", quality=82, optimize=True)
        return f"img/articles/{slug}.jpg"
    except Exception as e:
        print(f"     Bild-Download fehlgeschlagen ({image_url[:60]}): {e}")
        return None


def _title_keywords(title):
    """Extrahiert vergleichbare Stichworte aus einem Titel.
    Nutzt sowohl Großbuchstabenwörter als auch normale Wörter (>= 5 Zeichen).
    """
    if not title:
        return set()
    # Großbuchstabenwörter (Eigennamen)
    proper = re.findall(r"\b[A-ZÄÖÜ][a-zäöüß]{3,}\b", title)
    # Generelle Stichworte: alle Wörter >= 5 Zeichen, ohne Stoppwörter
    stop = {"sich", "nach", "trotz", "über", "diese", "dieser", "dieses",
            "haben", "wieder", "noch", "auch", "seine", "seinen", "seiner",
            "ihrem", "ihren", "ihrer", "wird", "werden", "wurde", "kann",
            "nicht", "keine", "schon", "aber", "doch", "weil", "damit",
            "gegen", "gegenüber", "dabei", "ohne"}
    words = re.findall(r"\b\w{5,}\b", title.lower())
    keywords = set(p.lower() for p in proper)
    keywords.update(w for w in words if w not in stop)
    return keywords


def _is_similar_title(title_a, title_b, threshold=0.55):
    """True wenn zwei Titel inhaltlich sehr ähnlich sind."""
    ka = _title_keywords(title_a)
    kb = _title_keywords(title_b)
    if not ka or not kb:
        return False
    # Jaccard-Ähnlichkeit
    intersect = len(ka & kb)
    union = len(ka | kb)
    if union == 0:
        return False
    similarity = intersect / union
    return similarity >= threshold


def is_already_covered(items, articles):
    """Prüft welche RSS-Items inhaltlich schon in den letzten Artikeln abgedeckt sind.
    WICHTIG: articles ist absteigend sortiert (neueste zuerst, durch insert(0,...)).
    Daher articles[:20] = die 20 NEUESTEN, nicht articles[-20:].
    """
    # Stichworte aus den 20 NEUESTEN Artikeln (oben in der Liste)
    recent_keyword_sets = []
    for a in articles[:20]:
        kws = _title_keywords(a.get("title", ""))
        if kws:
            recent_keyword_sets.append(kws)

    fresh = []
    for item in items:
        item_kws = _title_keywords(item.get("title", ""))
        if not item_kws:
            fresh.append(item)
            continue
        # Check gegen jeden recent Artikel
        is_dup = False
        for existing_kws in recent_keyword_sets:
            intersect = len(item_kws & existing_kws)
            union = len(item_kws | existing_kws)
            if union > 0 and intersect / union >= 0.55:
                is_dup = True
                break
        if not is_dup:
            fresh.append(item)
    return fresh


# ============================================================================
# VALIDIERUNG
# ============================================================================

def validate_article(post):
    """Prüft den generierten Artikel auf typische Fehler.
    Gibt eine Liste von Problemen zurück. Wenn leer → OK."""
    problems = []
    full_text = " ".join([
        post.get("title", ""),
        post.get("excerpt", ""),
        post.get("body", ""),
    ])
    full_lower = full_text.lower()

    # 1. Namens-Schreibfehler (nach Auto-Fix sollten die weg sein)
    for falsch, richtig in NAMENS_KORREKTUREN.items():
        if re.search(r"\b" + re.escape(falsch) + r"\b", full_text):
            problems.append(f"Schreibfehler: '{falsch}' (richtig: '{richtig}')")

    # 2. Ehemalige Spieler/Trainer
    for ex in EHEMALIGE_NICHT_NENNEN:
        if ex.lower() in full_lower:
            problems.append(f"Ex-Spieler/Trainer erwähnt: '{ex}' (nicht mehr im Kader 2025/26)")

    # 3. Floskeln
    for phrase in FORBIDDEN_PHRASES:
        if phrase.lower() in full_lower:
            problems.append(f"Floskel: '{phrase}'")

    # 4. Englischer Ausruf am Satzanfang
    if re.search(r"(?:^|[\.\!\?]\s+)(Honestly|Anyway|Look|Listen|Frankly)", full_text):
        problems.append("Englischer Ausruf am Satzanfang")

    # 5. Body zu kurz/lang
    body_words = len((post.get("body") or "").split())
    if body_words < 80:
        problems.append(f"Body zu kurz ({body_words} Wörter, Minimum 80)")
    if body_words > 350:
        problems.append(f"Body zu lang ({body_words} Wörter, Maximum 350)")

    # 6. Pflichtfelder
    if not post.get("title"):
        problems.append("Titel fehlt")
    if not post.get("sourceUrl"):
        problems.append("sourceUrl fehlt")
    if post.get("category") not in ("tactics", "transfer", "reaction"):
        problems.append(f"Ungültige Kategorie: {post.get('category')}")

    return problems


def auto_fix_names(post):
    """Behebt automatisch eindeutige Schreibfehler in Namen."""
    fixed = 0
    for field in ("title", "excerpt", "body"):
        if field not in post or not post[field]:
            continue
        text = post[field]
        for falsch, richtig in NAMENS_KORREKTUREN.items():
            new_text = re.sub(r"\b" + re.escape(falsch) + r"\b", richtig, text)
            if new_text != text:
                fixed += 1
                text = new_text
        post[field] = text
    return fixed


# ============================================================================
# CLAUDE-PROMPT
# ============================================================================

def build_kader_string():
    parts = []
    parts.append(f"Trainer: {', '.join(BAYERN_KADER['trainer'])}")
    parts.append(f"Torhüter: {', '.join(BAYERN_KADER['torhueter'])}")
    parts.append(f"Abwehr: {', '.join(BAYERN_KADER['abwehr'])}")
    parts.append(f"Mittelfeld: {', '.join(BAYERN_KADER['mittelfeld'])}")
    parts.append(f"Angriff: {', '.join(BAYERN_KADER['angriff'])}")
    return "\n".join(parts)


SYSTEM_PROMPT = f"""Du bist ein deutschsprachiger Sport-Redakteur für den FC-Bayern-Blog Workuvision.de.
Deine Aufgabe: aus einer oder mehreren RSS-Quellen einen sauberen, knappen, journalistisch fundierten Bayern-Artikel verfassen.

—— STIL ——
- Seriöser Journalismus mit Fan-Perspektive (vergleichbar mit kicker.de oder Süddeutscher Sportteil), KEIN Boulevard.
- Klare deutsche Sätze. Keine Anglizismen, keine Umgangssprache, keine Füllwörter.
- Keine eigene Meinung im Body — die Quelle wird sachlich wiedergegeben. Eine kleine Einordnung am Ende ist erlaubt.
- Maximal 220 Wörter. Drei Absätze.

—— FAKTEN ——
Aktueller Bayern-Kader Saison 2025/26 (NUR diese Spieler dürfen erwähnt werden):
{build_kader_string()}

NICHT mehr im Kader (Erwähnung verboten — sind weg):
{', '.join(EHEMALIGE_NICHT_NENNEN)}

KORREKTE SCHREIBWEISEN (häufige Fallen):
- Vincent Kompany (mit K, NICHT Company oder Compay)
- Lennart Karl (NICHT Kstark oder Stark)
- Aleksandar Pavlović (mit ć)
- Luis Díaz (mit í)
- Min-Jae Kim
- Hiroki Itō
- Josip Stanišić

—— REGELN ——
1. Schreibe AUSSCHLIESSLICH, was direkt aus der Quelle hervorgeht. Erfinde keine Zahlen, keine Spielernamen, keine Zitate. Wenn die Quelle „at least four players" sagt, schreib „mindestens vier Spieler" — nicht „vier bis fünf".
2. Nenne die Quelle namentlich („laut Bavarian Football Works", „der kicker berichtet", „Christian Falk berichtet auf Bluesky").
3. Du kannst auch ZWEI Quellen kombinieren, wenn sie unabhängig dasselbe Thema behandeln. Aber wähle nur EIN Thema!
4. Du bist Redakteur, nicht Fan: Schreib in der dritten Person über Bayern (NICHT „wir", NICHT „unsere"). Bayern ist „der FCB", „die Münchner", „der Rekordmeister", „die Bayern".
5. KEIN Slang. KEINE Floskeln. Verboten sind unter anderem:
   {', '.join(repr(p) for p in FORBIDDEN_PHRASES[:8])} — und ähnliche.
6. KEIN englischer Ausruf am Satzanfang („Honestly", „Look" usw.).
7. Wenn du dir bei einem Spielernamen unsicher bist, lass den Namen weg statt zu raten.

—— SOCIAL-MEDIA-QUELLEN ——
Manche Quellen kommen von Bluesky (Christian Falk, Fabrizio Romano, kicker, Sport1).
Diese sind kürzer und oft englisch. Behandle sie so:
- Christian Falk ist DER Bayern-Insider der BILD. Seine Posts sind verlässlich, oft mit „TRUE✅" / „NOT TRUE❌" markiert. Übernimm das als „bestätigt" oder „dementiert".
- Fabrizio Romano ist Welt-Transfer-Insider. Seine Aussagen kannst du als „laut Transfer-Experte Fabrizio Romano" einordnen, aber wenn er „here we go!" schreibt, ist das so gut wie offiziell.
- Bei Posts in englischer Sprache: übersetze sachlich, lass die englischen Zitate raus.
- Achte auf das Datum: Social-Posts sind oft taggleich; sehr aktuelle Themen.

—— AUSWAHL DES THEMAS ——
Bevorzuge in dieser Reihenfolge:
A. Transfer-Gerüchte und Wechsel (besonders spannend für Bayern-Fans, oft auf Bluesky)
B. Taktik-Analysen / Spielanalysen (Miasanrot ist hier oft eine gute Quelle)
C. Spieltagsberichte / Reaktionen (wenn etwas Bemerkenswertes passierte)
Vermeide: Allgemeine Fußball-News ohne starken Bayern-Bezug.

—— OUTPUT-FORMAT ——
Ausschließlich gültiges JSON (kein Markdown drumrum, keine Erklärung). Schema:
{{
  "title": "max. 65 Zeichen, sachlich",
  "category": "tactics" | "transfer" | "reaction",
  "excerpt": "ein bis zwei Sätze, max. 200 Zeichen, sachlich",
  "body": "drei Absätze, getrennt durch \\n\\n",
  "sourceUrl": "URL der Hauptquelle",
  "sourceName": "Name der Hauptquelle, z.B. 'Christian Falk (Bluesky)' oder 'Bavarian Football Works'"
}}"""


def write_with_claude(items, recent_articles, problems_from_previous=None):
    client = Anthropic(api_key=API_KEY)

    items_str = "\n\n".join([
        f"### {it['title']}\nQuelle: {it['source']} ({it['url']})\nVeröffentlicht: {it.get('published','')}\nInhalt:\n{it['summary']}"
        for it in items[:15]
    ])

    recent_titles = "\n".join(f"- {a['title']}" for a in recent_articles[-10:]) if recent_articles else "(keine)"

    user = f"""Letzte Artikel auf Workuvision (NICHT wiederholen):
{recent_titles}

Aktuelle Bayern-News-Vorschläge (wähle EINE Story aus):
{items_str}

Wähle die spannendste Story, die nicht zu nah an den letzten Artikeln liegt.
Schreibe einen Artikel im Workuvision-Stil. Reines JSON zurück."""

    if problems_from_previous:
        user += f"\n\n— HINWEIS — Beim letzten Versuch traten diese Probleme auf:\n"
        for p in problems_from_previous:
            user += f"  • {p}\n"
        user += "Schreibe den Artikel nochmal komplett, vermeide diese Fehler."

    print("→ Claude API Call (claude-sonnet-4-6)…")
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=== Workuvision Auto-News v2 ===")
    items = fetch_feed_items()
    print(f"✓ {len(items)} Items aus {len(FEEDS)} Feeds geladen.")

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

    # === Bis zu 2 Versuche: erst generieren, dann ggf. nachbessern ===
    new_post = None
    problems = None
    for attempt in (1, 2):
        try:
            print(f"\n— Versuch {attempt} —")
            candidate = write_with_claude(fresh_items, articles, problems_from_previous=problems)
        except Exception as e:
            print(f"❌ Claude-Aufruf fehlgeschlagen: {e}")
            sys.exit(0)

        # Auto-Fix für eindeutige Namens-Schreibfehler
        fixed = auto_fix_names(candidate)
        if fixed:
            print(f"  ✓ {fixed} Namens-Schreibfehler automatisch korrigiert.")

        problems = validate_article(candidate)
        if not problems:
            new_post = candidate
            print(f"  ✓ Validation OK.")
            break
        else:
            print(f"  ⚠️  {len(problems)} Probleme im Artikel:")
            for p in problems:
                print(f"     • {p}")
            if attempt == 2:
                print("❌ Zwei Versuche fehlgeschlagen — kein Commit.")
                sys.exit(0)
            print("  → Generiere neu mit Hinweis auf Probleme.")

    # === Fertigen Artikel ins content.json packen ===
    cat = new_post.get("category", "tactics")
    if cat not in BADGE_MAP:
        cat = "tactics"
    badge, badge_color = BADGE_MAP[cat]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = new_post.get("title", "Workuvision Take")[:90]
    slug = f"{today}-{slugify(title)}"

    # === POST-GENERATION DEDUP-CHECK ===
    # Claude hat einen Titel generiert. Prüfe ob inhaltlich schon ein sehr
    # ähnlicher Artikel in den letzten 20 existiert. Falls ja: nicht hinzufügen.
    for existing in articles[:20]:
        if existing.get("slug") == slug:
            print(f"  ⚠️  Slug existiert bereits ({slug}) — übersprungen.")
            sys.exit(0)
        if _is_similar_title(title, existing.get("title", ""), threshold=0.5):
            print(f"  ⚠️  Inhaltliches Duplikat erkannt:")
            print(f"     Neu:        '{title}'")
            print(f"     Existiert:  '{existing.get('title','')}'")
            print(f"  → Artikel verworfen.")
            sys.exit(0)

    # Bild-Auswahl:
    # 1) Versuche, das Bild aus dem RSS-Item zu laden, das Claude als Quelle gewählt hat
    # 2) Wenn das nicht klappt: zufällig aus Stock-Pool (vermeidet Wiederholungen)
    thumbnail = None
    source_url = new_post.get("sourceUrl", "")
    chosen_item = next((it for it in items if it.get("url") == source_url), None)
    # Fallback: matchen auf sourceName, falls URL nicht exakt gleich
    if not chosen_item:
        source_name = new_post.get("sourceName", "")
        chosen_item = next((it for it in items if source_name and source_name in it.get("source", "")), None)

    if chosen_item and chosen_item.get("image_url"):
        local_thumb = download_and_optimize_image(
            chosen_item["image_url"], slug, ARTICLE_IMG_DIR
        )
        if local_thumb:
            thumbnail = local_thumb
            print(f"  📷 RSS-Bild von {chosen_item.get('source','?')} gespeichert: {thumbnail}")

    # Fallback 1: og:image direkt von der Artikel-URL scrapen
    if not thumbnail:
        article_url = new_post.get("sourceUrl", "")
        if article_url:
            og_url = fetch_og_image_from_url(article_url)
            if og_url:
                local_thumb = download_and_optimize_image(og_url, slug, ARTICLE_IMG_DIR)
                if local_thumb:
                    thumbnail = local_thumb
                    print(f"  📷 og:image von {article_url[:50]} gescraped: {thumbnail}")

    # Fallback 2: Stock-Bild aus Pool (nur wenn vorher nichts ging)
    if not thumbnail:
        thumbnail = pick_stock_image(cat, content.get("articles", []))
        print(f"  📷 Stock-Bild verwendet: {thumbnail}")

    article = {
        "slug": slug,
        "date": today,
        "category": cat,
        "badge": badge,
        "badgeColor": badge_color,
        "thumbnail": thumbnail,
        "thumbnailAlt": title,
        "readtime": "4 Min.",
        "featured": False,
        "title": title,
        "excerpt": new_post.get("excerpt", "")[:250],
        "body": new_post.get("body", ""),
        "sourceUrl": new_post.get("sourceUrl", ""),
        "sourceName": new_post.get("sourceName", ""),
    }

    articles.insert(0, article)
    articles = articles[:30]

    seen = set()
    unique = []
    for a in articles:
        if a["slug"] not in seen:
            seen.add(a["slug"])
            unique.append(a)

    content["articles"] = unique
    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_content(content)

    print(f"\n✓ Neuer Artikel committed:")
    print(f"   {article['title']}")
    print(f"   Slug: {slug}")
    print(f"   Quelle: {article['sourceName']} — {article['sourceUrl']}")


if __name__ == "__main__":
    main()
