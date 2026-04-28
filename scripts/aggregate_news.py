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

# ============================================================================
# QUELLEN
# ============================================================================
FEEDS = [
    ("kicker FC Bayern", "https://newsfeed.kicker.de/team/fcbayernmuenchen"),
    ("Bavarian Football Works", "https://www.bavarianfootballworks.com/rss/index.xml"),
    ("Sport1 Topnews", "https://www.sport1.de/news.rss"),
]

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
        "Joao Palhinha", "Tom Bischof",
    ],
    "angriff": [
        "Harry Kane", "Michael Olise", "Jamal Musiala", "Serge Gnabry",
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
            if "Sport1" in name or "Topnews" in name:
                if "bayern" not in (title + " " + entry.get("summary", "")).lower():
                    continue
            items.append({
                "source": name,
                "url": entry.get("link", ""),
                "title": title,
                "summary": re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:800].strip(),
                "published": entry.get("published", ""),
            })
    return items


def is_already_covered(items, articles):
    existing_keywords = set()
    for a in articles[-15:]:
        words = re.findall(r"\b[A-Z][a-zäöü]{4,}\b", a.get("title", ""))
        existing_keywords.update(w.lower() for w in words)
    fresh = []
    for item in items:
        words = re.findall(r"\b[A-Z][a-zäöü]{4,}\b", item["title"])
        overlap = sum(1 for w in words if w.lower() in existing_keywords)
        if not words or overlap / len(words) < 0.5:
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
Deine Aufgabe: aus einer RSS-Quelle einen sauberen, knappen, journalistisch fundierten Bayern-Artikel verfassen.

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
2. Nenne die Quelle namentlich („laut Bavarian Football Works", „der kicker berichtet").
3. Du bist Redakteur, nicht Fan: Schreib in der dritten Person über Bayern (NICHT „wir", NICHT „unsere"). Bayern ist „der FCB", „die Münchner", „der Rekordmeister", „die Bayern".
4. KEIN Slang. KEINE Floskeln. Verboten sind unter anderem:
   {', '.join(repr(p) for p in FORBIDDEN_PHRASES[:8])} — und ähnliche.
5. KEIN englischer Ausruf am Satzanfang („Honestly", „Look" usw.).
6. Wenn du dir bei einem Spielernamen unsicher bist, lass den Namen weg statt zu raten.

—— OUTPUT-FORMAT ——
Ausschließlich gültiges JSON (kein Markdown drumrum, keine Erklärung). Schema:
{{
  "title": "max. 65 Zeichen, sachlich",
  "category": "tactics" | "transfer" | "reaction",
  "excerpt": "ein bis zwei Sätze, max. 200 Zeichen, sachlich",
  "body": "drei Absätze, getrennt durch \\n\\n",
  "sourceUrl": "URL aus den Vorschlägen",
  "sourceName": "z.B. 'kicker' oder 'Bavarian Football Works'"
}}"""


def write_with_claude(items, recent_articles, problems_from_previous=None):
    client = Anthropic(api_key=API_KEY)

    items_str = "\n\n".join([
        f"### {it['title']}\nQuelle: {it['source']} ({it['url']})\nVeröffentlicht: {it.get('published','')}\nZusammenfassung der Quelle:\n{it['summary']}"
        for it in items[:6]
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
