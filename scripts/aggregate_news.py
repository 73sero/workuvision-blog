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
import urllib.parse
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


# ============================================================================
# BAYERN-SPIELER-DATABASE für intelligente Bild-Auswahl
# Wird genutzt um aus Artikel-Titeln Spielernamen zu extrahieren und passende
# Bilder zu finden (Bayern-Pressefotos vom offiziellen CDN, Wikipedia-Fallback).
# Das Spielerfoto wird genutzt, wenn aus dem RSS/og:image kein Bild kommt.
# ============================================================================

# Bekannte Bayern-Spieler 2025/26 + Kebab-Case-Slug für fcbayern.com CDN.
# Pattern: img.fcbayern.com/.../players/spielerportraits/ganzkoerper/{slug}.png
BAYERN_PLAYERS = {
    # Tor
    "Manuel Neuer":      "manuel-neuer",
    "Neuer":             "manuel-neuer",
    "Sven Ulreich":      "sven-ulreich",
    "Jonas Urbig":       "jonas-urbig",
    # Abwehr
    "Dayot Upamecano":   "dayot-upamecano",
    "Upamecano":         "dayot-upamecano",
    "Jonathan Tah":      "jonathan-tah",
    "Tah":               "jonathan-tah",
    "Min-Jae Kim":       "minjae-kim",
    "Kim Min-jae":       "minjae-kim",
    "Hiroki Itō":        "hiroki-ito",
    "Hiroki Ito":        "hiroki-ito",
    "Josip Stanišić":    "josip-stanisic",
    "Stanišić":          "josip-stanisic",
    "Stanisic":          "josip-stanisic",
    "Alphonso Davies":   "alphonso-davies",
    "Davies":            "alphonso-davies",
    "Raphaël Guerreiro": "raphael-guerreiro",
    "Guerreiro":         "raphael-guerreiro",
    "Sacha Boey":        "sacha-boey",
    # Mittelfeld
    "Joshua Kimmich":    "joshua-kimmich",
    "Kimmich":           "joshua-kimmich",
    "Aleksandar Pavlović": "aleksandar-pavlovic",
    "Pavlović":          "aleksandar-pavlovic",
    "Pavlovic":          "aleksandar-pavlovic",
    "Leon Goretzka":     "leon-goretzka",
    "Goretzka":          "leon-goretzka",
    "Joao Palhinha":     "joao-palhinha",
    "Palhinha":          "joao-palhinha",
    "Konrad Laimer":     "konrad-laimer",
    "Laimer":            "konrad-laimer",
    # Offensive
    "Jamal Musiala":     "jamal-musiala",
    "Musiala":           "jamal-musiala",
    "Michael Olise":     "michael-olise",
    "Olise":             "michael-olise",
    "Luis Díaz":         "luis-diaz",
    "Luis Diaz":         "luis-diaz",
    "Díaz":              "luis-diaz",
    "Diaz":              "luis-diaz",
    "Serge Gnabry":      "serge-gnabry",
    "Gnabry":            "serge-gnabry",
    "Harry Kane":        "harry-kane",
    "Kane":              "harry-kane",
    "Nicolas Jackson":   "nicolas-jackson",
    "Jackson":           "nicolas-jackson",
    "Lennart Karl":      "lennart-karl",
    # ⚠️  "Karl" als Nachnamen-Match nicht hinzugefügt — ist zu generisch
    # (auch Vorname/normales Wort), würde False Positives erzeugen
    "Cassiano Ndiaye":   "cassiano-ndiaye",
    "Ndiaye":            "cassiano-ndiaye",
    "Tom Bischof":       "tom-bischof",
    "Bischof":           "tom-bischof",
    "Adam Pavic":        "adam-pavic",
    "Pavic":             "adam-pavic",
    # Trainer
    "Vincent Kompany":   "vincent-kompany",
    "Kompany":           "vincent-kompany",
}

# Wikipedia-Fallback: für Bayern-Spieler PLUS andere relevante Personen
# (Trainer, Bosse, Transferziele) als deutscher Wikipedia-Artikel-Titel.
WIKIPEDIA_SUBJECTS = {
    # Bayern alle wie oben — plus extras:
    "Anthony Gordon":   "Anthony Gordon",
    "Gordon":           "Anthony Gordon",
    "Max Eberl":        "Max Eberl",
    "Eberl":            "Max Eberl",
    "Uli Hoeneß":       "Uli Hoeneß",
    "Hoeneß":           "Uli Hoeneß",
    "Christoph Freund": "Christoph Freund",
    "Freund":           "Christoph Freund",
    "Alexander Nübel":  "Alexander Nübel",
    "Nübel":            "Alexander Nübel",
    "Daniel Peretz":    "Daniel Peretz",
    "Peretz":           "Daniel Peretz",
    # Bayern (vollständige Namen für WP)
    "Harry Kane":            "Harry Kane",
    "Manuel Neuer":          "Manuel Neuer",
    "Joshua Kimmich":        "Joshua Kimmich",
    "Jamal Musiala":         "Jamal Musiala",
    "Michael Olise":         "Michael Olise",
    "Vincent Kompany":       "Vincent Kompany",
    "Aleksandar Pavlović":   "Aleksandar Pavlović",
    "Leon Goretzka":         "Leon Goretzka",
    "Luis Díaz":             "Luis Díaz (Fußballspieler, 1997)",
    "Dayot Upamecano":       "Dayot Upamecano",
    "Alphonso Davies":       "Alphonso Davies",
    "Jonathan Tah":          "Jonathan Tah",
    "Konrad Laimer":         "Konrad Laimer",
    "Joao Palhinha":         "João Palhinha",
    "Serge Gnabry":          "Serge Gnabry",
}


def extract_subjects(title, body=""):
    """Extrahiert Bayern-Spielernamen / bekannte Personen aus dem Titel/Body.
    Gibt eine Liste der gefundenen Subjekte zurück, sortiert nach Länge
    (längere Namen zuerst, also vollständige vor Nachnamen).
    """
    text = (title or "") + " " + (body or "")
    found = []
    # Sort by length DESC um vollständige Namen zuerst zu matchen
    candidates = sorted(BAYERN_PLAYERS.keys(), key=len, reverse=True)
    used_slugs = set()
    for name in candidates:
        # word-boundary match (name endet/startet an Wortgrenze)
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text):
            slug = BAYERN_PLAYERS[name]
            if slug not in used_slugs:
                found.append(name)
                used_slugs.add(slug)
    # Auch andere bekannte Subjekte
    for name in sorted(WIKIPEDIA_SUBJECTS.keys(), key=len, reverse=True):
        if name in BAYERN_PLAYERS:
            continue
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text):
            if name not in found:
                found.append(name)
    return found


def fetch_bayern_cdn_image(player_slug):
    """Versucht das offizielle Bayern-Pressefoto vom fcbayern.com CDN zu laden.
    Format: ganzkoerper-Pose, 16:9 zugeschnitten, kein Hintergrund-Background.
    Returns image-URL string oder None.
    """
    if not player_slug:
        return None
    # Bayern's Cloudinary CDN — width 1200, aspect 16:9
    url = (f"https://img.fcbayern.com/image/upload/"
           f"f_auto/q_auto/ar_16:9,c_fill,g_custom,w_1200/"
           f"v1691827799/cms/public/images/fcbayern-com/"
           f"players/spielerportraits/ganzkoerper/{player_slug}.png")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Safari/605.1.15",
            "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Referer": "https://fcbayern.com/",
        })
        # HEAD-like check: einfach öffnen, kurz lesen um zu sehen ob da ist
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                # Erste Bytes prüfen — ist es ein gültiges Bild?
                head = r.read(16)
                if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") \
                        or head.startswith(b"RIFF"):  # PNG/JPEG/WebP
                    return url
        return None
    except Exception:
        return None


def fetch_wikipedia_image(subject_name):
    """Holt das Hauptbild eines Wikipedia-Artikels via REST API.
    DE Wikipedia bevorzugt, EN als Fallback. Funktioniert ohne API-Key.
    Returns image-URL oder None.
    """
    if not subject_name:
        return None
    # WP-Titel kann gemappt sein (z.B. "Luis Díaz" → "Luis Díaz (Fußballspieler, 1997)")
    wp_title = WIKIPEDIA_SUBJECTS.get(subject_name, subject_name)

    for lang in ("de", "en"):
        url = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
               f"{urllib.parse.quote(wp_title.replace(' ', '_'))}")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "WorkuvisionBot/1.0 "
                              "(https://workuvision.de; bot@workuvision.de)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            # Bevorzuge originalimage (höhere Auflösung), fallback thumbnail
            img = (data.get("originalimage", {}).get("source") or
                   data.get("thumbnail", {}).get("source"))
            if img:
                return img
        except Exception:
            continue
    return None


def find_subject_image(title, body=""):
    """Hauptfunktion: findet ein passendes Bild basierend auf Titel/Body.

    Strategie:
      1) Wenn 3+ Bayern-Spielernamen im Titel → Multi-Spieler-Thema, ein
         einzelnes Spielerbild wäre irreführend → return None (Caller fällt
         auf Stock zurück, was generischer aber thematisch passender ist)
      2) Bayern-CDN: aktuelle offizielle Bayern-Pressefotos (immer korrektes
         Bayern-Trikot)
      3) Wikipedia: NUR für nicht-Bayern-Personen (Berater, andere Trainer
         etc.). Wikipedia hat oft veraltete Bilder mit altem Vereinstrikot
         (Beispiel: Guerreiro auf Wikipedia oft noch im BVB-Trikot)

    Returns: Bild-URL oder None.
    """
    subjects = extract_subjects(title, body)
    if not subjects:
        return None

    # Multi-Spieler-Thema → einzelnes Spielerbild ist nicht repräsentativ
    bayern_subjects = [s for s in subjects if s in BAYERN_PLAYERS]
    if len(bayern_subjects) >= 2:
        print(f"     ⚠️  {len(bayern_subjects)} Bayern-Spieler im Titel "
              f"({', '.join(bayern_subjects[:5])}) — Stock-Fallback statt Einzelbild")
        return None

    primary = subjects[0]

    # 1) Bayern-CDN für Bayern-Spieler/Trainer
    if primary in BAYERN_PLAYERS:
        slug = BAYERN_PLAYERS[primary]
        url = fetch_bayern_cdn_image(slug)
        if url:
            print(f"     🏟️  Bayern-CDN für '{primary}': gefunden")
            return url
        # Wenn Bayern-CDN nichts hat: KEIN Wikipedia-Fallback weil Wikipedia
        # oft Bilder vom ehemaligen Verein zeigt (z.B. Guerreiro im BVB-Trikot)
        print(f"     ⚠️  '{primary}' im Bayern-CDN nicht gefunden — "
              f"kein Wikipedia-Fallback (Vermeidung falscher Vereinstrikots)")

    # 2) Wikipedia: NUR für nicht-Bayern-Personen
    if primary not in BAYERN_PLAYERS and primary in WIKIPEDIA_SUBJECTS:
        url = fetch_wikipedia_image(primary)
        if url:
            print(f"     📖 Wikipedia für '{primary}' (kein Bayern-Spieler): gefunden")
            return url

    # 3) Sekundäre Subjekte (nur Bayern-CDN)
    for s in subjects[1:3]:
        if s in BAYERN_PLAYERS:
            url = fetch_bayern_cdn_image(BAYERN_PLAYERS[s])
            if url:
                print(f"     🏟️  Bayern-CDN für '{s}' (sekundär): gefunden")
                return url

    return None


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

    # ── Quellenvielfalt sicherstellen ──
    # Statt items in Feed-Reihenfolge zurückzugeben (BFW dominiert weil zuerst in
    # der Liste), interleaven wir per Round-Robin: pro Durchgang ein Item aus
    # jeder Quelle, dann nochmal usw. Dadurch sieht Claude eine bunte Mischung
    # aus allen Feeds in den Top-15 Slots.
    from collections import defaultdict
    by_source = defaultdict(list)
    for item in items:
        by_source[item["source"]].append(item)

    print(f"\n  Items pro Quelle:")
    for src, lst in sorted(by_source.items(), key=lambda x: -len(x[1])):
        print(f"    {len(lst):>3d}× {src}")

    # Round-Robin: ein Item pro Quelle, dann nächste Runde
    interleaved = []
    max_per_source = max(len(lst) for lst in by_source.values()) if by_source else 0
    for round_idx in range(max_per_source):
        for src in by_source:
            if round_idx < len(by_source[src]):
                interleaved.append(by_source[src][round_idx])

    return interleaved


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


def _upgrade_image_url_resolution(url):
    """Versucht eine Bild-URL auf höhere Auflösung umzubiegen, wenn das
    URL-Pattern Größen-Parameter enthält. Kommt sehr häufig bei CMS-URLs vor.
    Beispiele:
      .../w_400/...         → .../w_1600/...
      ?w=400&h=300          → ?w=1600&h=1200
      ?width=400            → ?width=1600
      .../400x300/...       → .../1600x1200/...
      .../small.jpg         → .../large.jpg
    """
    if not url:
        return url
    original = url
    # Cloudinary/Bavarian Football Works style: /w_400/  or  /w_400,h_300/
    url = re.sub(r'(/w_)(\d{2,4})', lambda m: m.group(1) + '1600', url)
    url = re.sub(r'([/,]h_)(\d{2,4})', lambda m: m.group(1) + '1200', url)
    # WordPress: /img.jpg?w=400&h=300
    url = re.sub(r'([?&]w=)(\d{2,4})', lambda m: m.group(1) + '1600', url)
    url = re.sub(r'([?&]width=)(\d{2,4})', lambda m: m.group(1) + '1600', url)
    url = re.sub(r'([?&]h=)(\d{2,4})', lambda m: m.group(1) + '1200', url)
    url = re.sub(r'([?&]height=)(\d{2,4})', lambda m: m.group(1) + '1200', url)
    # NTV/Sport1-style: /Img_16_9/1200/foo.jpg → /Img_16_9/1920/foo.jpg
    # Match: /<digits>/  unmittelbar vor dem Dateinamen
    url = re.sub(
        r'/(\d{3,4})/([^/]+\.(?:jpg|jpeg|png|webp))',
        lambda m: ('/1920/' if int(m.group(1)) < 1920 else f'/{m.group(1)}/') + m.group(2),
        url, flags=re.IGNORECASE
    )
    # Pfad-style: /400x300/
    url = re.sub(r'/(\d{3,4})x(\d{3,4})/', '/1600x1200/', url)
    # NTV/RND-style mit ?_v=mobile
    url = re.sub(r'imageVersion=mobile', 'imageVersion=2-3', url)
    # WAZ/FUNKE Bauanleitung: -16-9.jpg vs -1-1.jpg etc bei "w=" Parameter
    return url


def fetch_bluesky_image(bsky_url):
    """Holt das embedded Bild aus einem Bluesky-Post via offizieller Bsky-API.

    Bluesky-Posts haben verschiedene Embed-Typen:
    - app.bsky.embed.images#view: Spieler/Foto direkt im Post (priorität 1)
    - app.bsky.embed.external#view: Link zu Artikel mit thumb (priorität 2)

    Die Bsky-API ist öffentlich (kein Auth-Key nötig).

    Returns: tuple (image_url, fallback_article_url) oder (None, None)
        image_url: direkte Bild-URL die geladen werden kann
        fallback_article_url: URL des verlinkten Artikels (wenn external embed),
            damit der Caller stattdessen ein hochwertigeres og:image scrapen kann
    """
    if not bsky_url or "bsky.app" not in bsky_url:
        return None, None
    m = re.match(r'https?://bsky\.app/profile/([^/]+)/post/([^/?#]+)', bsky_url)
    if not m:
        return None, None
    handle, post_id = m.group(1), m.group(2)

    try:
        # Schritt 1: DID des Profils holen
        prof_url = f"https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={urllib.parse.quote(handle)}"
        req = urllib.request.Request(prof_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 Safari/605.1.15",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            profile = json.loads(r.read())
        did = profile.get("did")
        if not did:
            return None, None

        # Schritt 2: Post-Thread holen
        at_uri = f"at://{did}/app.bsky.feed.post/{post_id}"
        thread_url = (f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
                      f"?uri={urllib.parse.quote(at_uri)}")
        req = urllib.request.Request(thread_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 Safari/605.1.15",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            thread = json.loads(r.read())

        post = thread.get("thread", {}).get("post", {})
        embed = post.get("embed") or post.get("record", {}).get("embed", {})
        if not embed:
            return None, None

        # 1) Direkt eingebettete Bilder im Post (höchste Qualität)
        if "images" in embed:
            for img in embed.get("images", []):
                full = img.get("fullsize")
                if full:
                    return full, None
                thumb = img.get("thumb")
                if thumb:
                    return thumb, None

        # 2) External-Embed (Link zu anderer Seite)
        if "external" in embed:
            ext = embed.get("external", {})
            ext_thumb = ext.get("thumb")
            ext_uri = ext.get("uri")
            return ext_thumb, ext_uri

        return None, None
    except Exception as e:
        print(f"     Bsky-API fehlgeschlagen ({bsky_url[:60]}): {e}")
        return None, None


def _resolve_redirect_url(url):
    """Folgt HTTP-Redirects (z.B. dlvr.it Linktree) zur finalen URL.
    Returns die aufgelöste URL oder None bei Fehler."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 Safari/605.1.15",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.url
    except Exception:
        return None


def fetch_og_image_from_url(article_url):
    """Lädt das beste verfügbare Hauptbild für eine Artikel-URL.

    Strategie:
    - Bei Bluesky-URLs: Bsky-API benutzen
        * Direkt eingebettete Bilder im Post (höchste Qualität)
        * Bei external-Embed: erst og:image vom verlinkten Artikel holen
          (oft hochauflösender als Bsky-Thumb), dann Bsky-Thumb als Fallback
    - Bei normalen URLs: og:image-Tags + JSON-LD aus dem HTML extrahieren
    - In allen Fällen: URL nach Möglichkeit auf höhere Auflösung hochsetzen
    """
    if not article_url:
        return None

    # === Bluesky special case ===
    if "bsky.app" in article_url.lower():
        bsky_img, fallback_url = fetch_bluesky_image(article_url)
        # Wenn external embed mit Link: erst og:image vom verlinkten Artikel holen
        if fallback_url:
            resolved = _resolve_redirect_url(fallback_url) or fallback_url
            print(f"     Bluesky-External → og:image vom verlinkten Artikel ({resolved[:70]})")
            ext_og = _fetch_og_from_html(resolved)
            if ext_og:
                return ext_og
        # Fallback: Bsky-Thumb selbst
        if bsky_img:
            print(f"     Bluesky-Thumb als Bild verwendet")
            return _upgrade_image_url_resolution(bsky_img)
        return None

    # === Normale URLs ===
    return _fetch_og_from_html(article_url)


def _fetch_og_from_html(article_url):
    """Interner Helper: og:image aus HTML einer normalen Artikel-Seite scrapen."""
    if not article_url:
        return None
    try:
        req = urllib.request.Request(article_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            # Lese mehr (300KB) — manche Seiten haben Meta-Tags weiter unten
            html = r.read(300_000).decode("utf-8", errors="ignore")

        # Sammle ALLE Bild-Kandidaten in Prioritätsreihenfolge
        candidates = []

        # 1) og:image:secure_url (höchste Priorität, oft hi-res)
        for m in re.finditer(
            r'<meta\s+(?:property|name)="og:image:secure_url"[^>]*content="([^"]+)"',
            html, re.IGNORECASE):
            candidates.append(m.group(1))

        # 2) og:image (Standard)
        for m in re.finditer(
            r'<meta\s+(?:property|name)="og:image"[^>]*content="([^"]+)"',
            html, re.IGNORECASE):
            candidates.append(m.group(1))
        for m in re.finditer(
            r'<meta\s+content="([^"]+)"[^>]*(?:property|name)="og:image"',
            html, re.IGNORECASE):
            candidates.append(m.group(1))

        # 3) twitter:image (Backup)
        for m in re.finditer(
            r'<meta\s+(?:property|name)="twitter:image(?::src)?"[^>]*content="([^"]+)"',
            html, re.IGNORECASE):
            candidates.append(m.group(1))

        # 4) JSON-LD: schema.org Article hat oft "image" Feld mit vollständiger URL
        for m in re.finditer(
            r'"image"\s*:\s*(?:\[\s*)?["{]([^"]+(?:\.jpg|\.jpeg|\.png|\.webp))',
            html, re.IGNORECASE):
            url_candidate = m.group(1).split('"')[0].split("?")[0] + (
                "?" + m.group(1).split("?")[1] if "?" in m.group(1) else ""
            )
            candidates.append(m.group(1))

        if not candidates:
            return None

        # Erste gültige URL nehmen, dafür auf hohe Auflösung upgraden
        for url in candidates:
            url = url.strip()
            if not url:
                continue
            # HTML-Entity-Decoding
            url = url.replace("&amp;", "&").replace("&#x2F;", "/").replace("&quot;", "")
            if url.startswith("//"):
                url = "https:" + url
            if not url.startswith("http"):
                continue
            # URL-Auflösung hochsetzen wo möglich
            url = _upgrade_image_url_resolution(url)
            return url
    except Exception as e:
        print(f"     og:image scrape fehlgeschlagen ({article_url[:60]}): {e}")
    return None


def download_and_optimize_image(image_url, slug, target_dir):
    """Lädt ein Bild herunter und speichert als JPEG in optimaler Qualität.

    Strategie für maximale Qualität:
    - Kein Hochskalieren (würde unscharf werden) — wenn Original kleiner als
      1200x675, nutzt das Original und schreibt eine Warnung
    - 16:9 Center-Crop wenn das Original größer ist
    - High-Quality LANCZOS Resampling
    - JPEG quality=88, progressive=True (kleinere Dateien bei besserer Qualität)
    - Mindestgröße 600px Breite — sonst wird das Bild verworfen (zu klein
      für Header-Verwendung). Der Caller fällt dann auf andere Quelle zurück.

    Gibt den lokalen Pfad zurück (relativ zum Repo) oder None bei Fehlern."""
    if not image_url or not PILLOW_AVAILABLE:
        return None
    target_path = target_dir / f"{slug}.webp"
    try:
        req = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Safari/605.1.15",
            "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Referer": "https://workuvision.de/",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 5000:  # < 5KB → wahrscheinlich Tracking-Pixel oder Platzhalter
            print(f"     ⚠️  Bild zu klein ({len(data)/1024:.1f}KB) — vermutlich Platzhalter")
            return None
        img = Image.open(io.BytesIO(data))
        src_w, src_h = img.size

        # Mindestauflösung: 600x400. Kleiner = nicht header-tauglich
        # → Caller fällt auf nächsten Fallback zurück (z.B. Bayern-CDN/Wikipedia)
        if src_w < 600 or src_h < 400:
            print(f"     ⚠️  Bild zu klein ({src_w}x{src_h}) für Header — überspringe")
            return None

        # In RGB konvertieren
        if img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg

        # Ziel: 16:9 (1200x675), aber NIE hochskalieren
        target_w, target_h = 1200, 675
        target_ratio = target_w / target_h

        # Wenn Original kleiner als Ziel: Original-Größe nutzen und 16:9 croppen
        if src_w < target_w or src_h < target_h:
            # Auf 16:9 zuschneiden, ohne zu skalieren
            src_ratio = src_w / src_h
            if src_ratio > target_ratio:
                # Zu breit — oben/unten haben volle Höhe, links/rechts wegschneiden
                new_w = int(src_h * target_ratio)
                left = (src_w - new_w) // 2
                img = img.crop((left, 0, left + new_w, src_h))
            else:
                # Zu hoch — links/rechts volle Breite, oben/unten wegschneiden
                new_h = int(src_w / target_ratio)
                top = (src_h - new_h) // 2
                img = img.crop((0, top, src_w, top + new_h))
            print(f"     ℹ️  Original {src_w}x{src_h} kleiner als Ziel — kein Upscale, nur Crop auf {img.size}")
        else:
            # Original ist größer: skalieren auf 16:9 (target) mit Center-Crop
            src_ratio = src_w / src_h
            if src_ratio > target_ratio:
                # Höhe zu klein → nach Höhe skalieren, links/rechts cropen
                scale = target_h / src_h
                new_w = int(src_w * scale)
                new_h = target_h
                img = img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - target_w) // 2
                img = img.crop((left, 0, left + target_w, target_h))
            else:
                # Breite zu klein → nach Breite skalieren, oben/unten cropen
                scale = target_w / src_w
                new_w = target_w
                new_h = int(src_h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                top = (new_h - target_h) // 2
                img = img.crop((0, top, target_w, top + target_h))

        # Speichern: WebP lossy quality=82 ≈ JPEG quality 88 visuell, aber ~40% kleiner.
        # method=6 = bester Kompressor, langsamer aber im 3×-täglich-Workflow egal.
        target_dir.mkdir(parents=True, exist_ok=True)
        img.save(target_path, "WEBP", quality=82, method=6)
        size_kb = target_path.stat().st_size / 1024
        print(f"     ✓ {target_path.name} {img.size[0]}x{img.size[1]} ({size_kb:.0f}KB)")
        return f"img/articles/{slug}.webp"
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

    Drei Schichten:
      1) URL-Match: gleiche sourceUrl → schon abgedeckt
      2) Jaccard ≥ 0.45 → ähnlicher Titel
      3) Top-3-Stichworte-Heuristik bei kurzen Titeln
    """
    # Existierende sourceUrls und Stichworte aus den 25 NEUESTEN Artikeln
    recent_source_urls = set()
    recent_keyword_sets = []
    for a in articles[:25]:
        src = (a.get("sourceUrl", "") or "").strip()
        if src:
            recent_source_urls.add(src)
        kws = _title_keywords(a.get("title", ""))
        if kws:
            recent_keyword_sets.append(kws)

    fresh = []
    for item in items:
        # 1) sourceUrl-Match
        item_url = (item.get("url", "") or "").strip()
        if item_url and item_url in recent_source_urls:
            # Schon mal verarbeitet — überspringen ohne Logging-Spam
            continue

        item_kws = _title_keywords(item.get("title", ""))
        if not item_kws:
            fresh.append(item)
            continue

        # 2+3) Inhalts-Match
        is_dup = False
        for existing_kws in recent_keyword_sets:
            intersect = len(item_kws & existing_kws)
            union = len(item_kws | existing_kws)
            smaller = min(len(item_kws), len(existing_kws))
            # Jaccard ≥ 0.45 ODER 3+ gemeinsame Stichworte (60% des kleineren)
            if union > 0 and intersect / union >= 0.45:
                is_dup = True
                break
            if intersect >= 3 and smaller > 0 and intersect / smaller >= 0.6:
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

    # 5. Body zu kurz/lang — Google's NewsArticle Quality-Signal liegt bei 300+ Wörtern,
    #    deshalb Minimum 250 (kleine Toleranz für sehr knappe Quellen).
    body_words = len((post.get("body") or "").split())
    if body_words < 250:
        problems.append(f"Body zu kurz ({body_words} Wörter, Minimum 250)")
    if body_words > 480:
        problems.append(f"Body zu lang ({body_words} Wörter, Maximum 480)")

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
- Zielumfang 320–420 Wörter. Vier bis fünf Absätze. Tiefe ist erwünscht — solange jedes Wort durch die Quelle gedeckt ist.
- Erlaubt zur Tiefenerweiterung (sofern die Quelle es hergibt): Einordnung in die laufende Saison, Bezug zu vorherigen Spielen oder Transfers, kurze taktische Einordnung, konkrete Statistiken aus der Quelle, sachlicher Vergleich mit Konkurrenz.
- NICHT erlaubt zur Tiefenerweiterung: erfundene Zahlen, Spekulation ohne Beleg, doppelte Wiederholung derselben Aussage, Floskeln, generische Phrasen wie "es bleibt spannend".

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
  "body": "vier bis fünf Absätze, getrennt durch \\n\\n, Zielumfang 320–420 Wörter",
  "sourceUrl": "URL der Hauptquelle",
  "sourceName": "Name der Hauptquelle, z.B. 'Christian Falk (Bluesky)' oder 'Bavarian Football Works'"
}}"""


def write_with_claude(items, recent_articles, problems_from_previous=None):
    client = Anthropic(api_key=API_KEY)

    # Mehr Items zeigen (25 statt 15) damit Claude eine echte Auswahl hat.
    # Dank Round-Robin-Interleaving sehen die Top-25 alle Quellen
    items_str = "\n\n".join([
        f"### {it['title']}\nQuelle: {it['source']} ({it['url']})\nVeröffentlicht: {it.get('published','')}\nInhalt:\n{it['summary']}"
        for it in items[:25]
    ])

    recent_titles = "\n".join(f"- {a['title']}" for a in recent_articles[-10:]) if recent_articles else "(keine)"

    # Letzte Quellen sammeln um Quellenvielfalt zu fördern
    from collections import Counter
    recent_sources = Counter()
    for a in recent_articles[-10:]:
        src = a.get("sourceName", "")
        if src:
            recent_sources[src] += 1
    sources_hint = ""
    if recent_sources:
        most_used = recent_sources.most_common(1)[0]
        if most_used[1] >= 3:
            sources_hint = (f"\n\nHINWEIS Quellenvielfalt: Die letzten Artikel kamen häufig "
                            f"von '{most_used[0]}' ({most_used[1]}x in den letzten 10). "
                            f"Bevorzuge diesmal eine andere Quelle — Miasanrot (Taktik), "
                            f"Christian Falk/Fabrizio Romano (Insider-Transfers), "
                            f"oder kicker/Spiegel/SZ (klassischer Sport-Journalismus).")

    user = f"""Letzte Artikel auf Workuvision (NICHT wiederholen):
{recent_titles}

Aktuelle Bayern-News-Vorschläge (wähle EINE Story aus):
{items_str}

Wähle die spannendste Story, die nicht zu nah an den letzten Artikeln liegt.
Schreibe einen Artikel im Workuvision-Stil. Reines JSON zurück.{sources_hint}"""

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
    # Claude hat einen Titel generiert. Drei Schichten von Duplikatsprüfung:
    #   1) Slug exakt gleich → übersprungen
    #   2) Gleiche sourceUrl wie ein existierender Artikel → übersprungen
    #      (verhindert dass derselbe Quell-Artikel 2x verarbeitet wird,
    #      auch wenn Claude leicht andere Titel generiert hat)
    #   3) Inhaltlich ähnlicher Titel (Jaccard ≥ 0.4 oder ≥3 Kern-Stichworte
    #      identisch) → übersprungen
    new_source_url = new_post.get("sourceUrl", "").strip()
    new_kws = _title_keywords(title)

    for existing in articles[:25]:
        if existing.get("slug") == slug:
            print(f"  ⚠️  Slug existiert bereits ({slug}) — übersprungen.")
            sys.exit(0)
        # Gleiche Quell-URL: harter Block
        ex_src = existing.get("sourceUrl", "").strip()
        if new_source_url and ex_src and new_source_url == ex_src:
            print(f"  ⚠️  Selbe sourceUrl bereits verarbeitet:")
            print(f"     {new_source_url[:80]}")
            print(f"     existiert als: '{existing.get('title','')}'")
            print(f"  → Artikel verworfen.")
            sys.exit(0)
        # Inhalts-Dedup
        if _is_similar_title(title, existing.get("title", ""), threshold=0.4):
            print(f"  ⚠️  Inhaltliches Duplikat erkannt (Jaccard ≥ 0.4):")
            print(f"     Neu:        '{title}'")
            print(f"     Existiert:  '{existing.get('title','')}'")
            print(f"  → Artikel verworfen.")
            sys.exit(0)
        # Zusätzliche Heuristik: Wenn 3+ Kern-Stichworte überlappen, ist das
        # bei kurzen Titeln meist ein Duplikat unabhängig vom Jaccard-Score
        if new_kws:
            ex_kws = _title_keywords(existing.get("title", ""))
            if ex_kws:
                overlap = len(new_kws & ex_kws)
                # Bei kleinen Sets: mind. 3 gemeinsame Wörter UND mind. 60%
                # Überlappung mit dem kleineren Titel
                smaller = min(len(new_kws), len(ex_kws))
                if overlap >= 3 and smaller > 0 and overlap / smaller >= 0.6:
                    print(f"  ⚠️  Stichwort-Überlappung zu hoch ({overlap} gemeinsam, {overlap}/{smaller}):")
                    print(f"     Neu:        '{title}'")
                    print(f"     Existiert:  '{existing.get('title','')}'")
                    print(f"     Gemeinsam:  {new_kws & ex_kws}")
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

    # Fallback 2: Subjekt-basiertes Bild (Bayern-CDN / Wikipedia)
    # Schaut nach Bayern-Spielernamen im Titel/Body und holt offizielle
    # Pressefotos. So bekommen auch Artikel ohne sourceUrl ein passendes Bild.
    if not thumbnail:
        subject_url = find_subject_image(
            new_post.get("title", ""),
            new_post.get("body", "")
        )
        if subject_url:
            local_thumb = download_and_optimize_image(subject_url, slug, ARTICLE_IMG_DIR)
            if local_thumb:
                thumbnail = local_thumb
                print(f"  📷 Subjekt-Bild gespeichert: {thumbnail}")

    # Fallback 3: Stock-Bild aus Pool (nur wenn vorher nichts ging)
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
