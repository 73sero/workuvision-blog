"""Match-Reporter für Workuvision.

Wird in jedem Action-Lauf aufgerufen. Erkennt anhand des aktuellen Bayern-Spiels:

  - PRE-MATCH (T-3h bis Anpfiff):
      → Aufstellung aus RSS-Feeds extrahieren (wenn vorhanden)
      → Vorbericht-Post schreiben (wenn noch nicht für dieses Spiel geschehen)
      → Setzt content.json["currentLineup"]

  - POST-MATCH (Schlusspfiff bis +3h):
      → Spielbericht aus RSS + OpenLigaDB-Endstand schreiben
      → currentLineup wird gelöscht (Spiel vorbei)

  - SONST: nichts.

Verhindert Doppel-Posts via "matchPostsTracker" in content.json.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

try:
    from anthropic import Anthropic
except ImportError:
    print("anthropic nicht installiert.")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
if not API_KEY:
    print("⚠️  ANTHROPIC_API_KEY nicht gesetzt — Skip Match-Reporter.")
    sys.exit(0)

# Aktueller Bayern-Kader 2025/26 (gleiche Whitelist wie aggregate_news)
BAYERN_KADER = [
    "Vincent Kompany",
    # Tor
    "Manuel Neuer", "Sven Ulreich", "Jonas Urbig",
    # Abwehr
    "Dayot Upamecano", "Min-Jae Kim", "Jonathan Tah", "Hiroki Itō", "Hiroki Ito",
    "Sacha Boey", "Konrad Laimer", "Josip Stanišić", "Stanisic",
    "Raphaël Guerreiro", "Guerreiro", "Alphonso Davies",
    # Mittelfeld
    "Joshua Kimmich", "Aleksandar Pavlović", "Pavlovic", "Leon Goretzka",
    "Joao Palhinha", "Tom Bischof",
    # Angriff
    "Harry Kane", "Michael Olise", "Jamal Musiala", "Serge Gnabry",
    "Luis Díaz", "Luis Diaz", "Nicolas Jackson", "Lennart Karl",
]

EHEMALIGE_NICHT_NENNEN = [
    "Choupo-Moting", "Leroy Sané", "Tuchel", "Kovač",
    "Nagelsmann", "Lewandowski",
]

NAMENS_KORREKTUREN = {
    "Compay": "Kompany", "Company": "Kompany",
    "Lennart Kstark": "Lennart Karl", "Kstark": "Karl",
    "Pavlovic": "Pavlović", "Stanisic": "Stanišić",
    "Guerriero": "Guerreiro",
}

FORBIDDEN_PHRASES = [
    "honestly", "halt besonders", "halt eben", "Kragenweite",
    "Statement setzen", "wehzutun", "ein Statement",
    "auf einem anderen Level", "fühlt sich unfair an",
    "drängelt sich die Frage", "durchgesund",
    "Zähne zusammenbeißen", "Kracher gegen", "in aller Munde",
]

FEEDS_FOR_LINEUP = [
    ("kicker", "https://newsfeed.kicker.de/news/aktuell"),
    ("Bavarian Football Works", "https://www.bavarianfootballworks.com/rss/index.xml"),
    ("Tagesschau Sport", "https://www.tagesschau.de/sport/index~rss2.xml"),
    ("NTV Sport", "https://www.n-tv.de/sport/rss"),
    ("Spiegel Sport", "https://www.spiegel.de/sport/index.rss"),
    ("Süddeutsche Sport", "https://rss.sueddeutsche.de/rss/Sport"),
]


# ============================================================================
# HELPER
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


def fetch_json(url, timeout=15):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Workuvision-Bot)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  {url}: {e}")
        return None


def auto_fix_names_text(text):
    """Behebt Namens-Schreibfehler in beliebigem Text."""
    for falsch, richtig in NAMENS_KORREKTUREN.items():
        text = re.sub(r"\b" + re.escape(falsch) + r"\b", richtig, text)
    return text


def auto_fix_names(post):
    fixed = 0
    for field in ("title", "excerpt", "body"):
        if not post.get(field):
            continue
        new = auto_fix_names_text(post[field])
        if new != post[field]:
            post[field] = new
            fixed += 1
    return fixed


def validate_post(post):
    problems = []
    full = " ".join([post.get("title",""), post.get("excerpt",""), post.get("body","")]).lower()
    full_orig = post.get("title","") + " " + post.get("excerpt","") + " " + post.get("body","")
    for falsch in NAMENS_KORREKTUREN:
        if re.search(r"\b" + re.escape(falsch) + r"\b", full_orig):
            problems.append(f"Schreibfehler: {falsch}")
    for ex in EHEMALIGE_NICHT_NENNEN:
        if ex.lower() in full:
            problems.append(f"Ex-Spieler erwähnt: {ex}")
    for phrase in FORBIDDEN_PHRASES:
        if phrase.lower() in full:
            problems.append(f"Floskel: {phrase}")
    if re.search(r"(?:^|[\.\!\?]\s+)(Honestly|Anyway|Look|Listen|Frankly)", full_orig):
        problems.append("Englischer Ausruf")
    body_words = len((post.get("body") or "").split())
    if body_words < 80 or body_words > 350:
        problems.append(f"Body-Länge {body_words}")
    return problems


# ============================================================================
# SPIELPLAN-LOGIK
# ============================================================================

def get_match_phase():
    """Liest aktuellen Bayern-Spielplan und liefert Phase + Match.

    Returns: dict with keys:
      phase: "pre" | "post" | "live" | "idle"
      match: das relevante Match-Objekt (oder None)
      match_id: str
    """
    bl_data = fetch_json("https://api.openligadb.de/getmatchdata/bl1/2025") or []
    if not bl_data:
        return {"phase": "idle", "match": None, "match_id": None}

    bayern_matches = [
        m for m in bl_data
        if "bayern" in (m.get("team1",{}).get("teamName","") + " " + m.get("team2",{}).get("teamName","")).lower()
    ]
    bayern_matches.sort(key=lambda m: m.get("matchDateTime",""))

    now = datetime.now(timezone.utc)

    for m in bayern_matches:
        try:
            kickoff = datetime.fromisoformat(m["matchDateTime"].replace("Z","+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        delta = (kickoff - now).total_seconds()
        match_id = str(m.get("matchID"))

        # PRE-MATCH: Anpfiff in 0 bis 3h
        if 0 <= delta <= 3 * 3600 and not m.get("matchIsFinished"):
            return {"phase": "pre", "match": m, "match_id": match_id, "kickoff": kickoff}

        # LIVE: Anpfiff vor 0-2.5h, nicht beendet
        if -2.5*3600 <= delta < 0 and not m.get("matchIsFinished"):
            return {"phase": "live", "match": m, "match_id": match_id, "kickoff": kickoff}

        # POST-MATCH: beendet, Anpfiff vor max. 3h (heißt: vor max. ~5h jetzt)
        if m.get("matchIsFinished") and -5*3600 <= delta < 0:
            return {"phase": "post", "match": m, "match_id": match_id, "kickoff": kickoff}

    return {"phase": "idle", "match": None, "match_id": None}


# ============================================================================
# AUFSTELLUNG AUS RSS EXTRAHIEREN
# ============================================================================

def fetch_lineup_candidates(opponent_name):
    """Sucht in RSS-Feeds nach Bayern-Aufstellungs-Items.
    Strenge Kriterien: muss Aufstellungs-Wort + mehrere Bayern-Spielernamen enthalten."""
    candidates = []
    keywords_main = ["aufstellung", "startelf", "starting xi", "starting eleven",
                     "starting lineup"]
    bayern_words = ["bayern", "fcb"]
    opp_lower = (opponent_name or "").lower()

    # Häufige Bayern-Spielernamen — wenn 3+ davon im Text auftauchen, ist's
    # wahrscheinlich eine echte Aufstellung
    player_names = [
        "neuer", "kane", "musiala", "olise", "kimmich", "upamecano",
        "pavlovic", "pavlović", "kim", "tah", "laimer", "stanisic",
        "stanišić", "guerreiro", "davies", "goretzka", "palhinha",
        "diaz", "díaz", "gnabry", "jackson", "karl", "boey", "ito", "itō",
        "urbig", "ulreich",
    ]

    for name, url in FEEDS_FOR_LINEUP:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
        for entry in feed.entries[:20]:
            title = (entry.get("title","") or "").lower()
            summary = re.sub(r"<[^>]+>","", entry.get("summary","") or "")[:2000]
            full = title + " " + summary.lower()

            has_lineup_word = any(k in full for k in keywords_main)
            has_bayern = any(b in full for b in bayern_words)
            player_count = sum(1 for p in player_names if p in full)

            mentions_opp = opp_lower and any(
                w in full for w in opp_lower.split() if len(w) > 3
            )

            # Score: Aufstellungs-Wort + Bayern + ≥3 Spielernamen → echter Aufstellungs-Post
            if has_lineup_word and has_bayern and player_count >= 3:
                score = 20 + player_count + (5 if mentions_opp else 0)
                candidates.append({
                    "score": score,
                    "source": name,
                    "title": entry.get("title",""),
                    "summary": summary[:1500],
                    "url": entry.get("link",""),
                    "published": entry.get("published",""),
                })

    candidates.sort(key=lambda c: -c["score"])
    return candidates[:5]


def extract_lineup_via_claude(candidates, match_info):
    """Schickt RSS-Lineup-Kandidaten an Claude, der die 11 Namen extrahiert."""
    if not candidates:
        return None

    client = Anthropic(api_key=API_KEY)

    items_str = "\n\n".join([
        f"### Quelle: {c['source']}\nTitel: {c['title']}\nText:\n{c['summary']}"
        for c in candidates
    ])

    opp = match_info.get("opponent_name", "")
    home = match_info.get("is_home", True)

    system = f"""Du extrahierst die Bayern-Startaufstellung aus Sport-RSS-Texten.

KONTEXT: Bayern München spielt heute {('zuhause gegen' if home else 'auswärts bei')} {opp}.

Du bekommst 1-5 RSS-Texte, die möglicherweise die Aufstellung enthalten.

REGELN:
- Du extrahierst NUR, wenn explizit eine Bayern-Startelf genannt wird (11 Spieler erkennbar).
- Wenn unklar oder nicht eindeutig 11 Spieler genannt: gib `null` zurück.
- Spieler-Namen müssen aus diesem Bayern-Kader 2025/26 stammen:
  {', '.join(BAYERN_KADER[:30])} ...
- Korrekte Schreibweisen: Vincent Kompany (NICHT Compay), Lennart Karl (NICHT Kstark), Pavlović, Díaz, Stanišić.
- Wenn ein Name nicht in der Kader-Liste steht: gib `null` zurück (kein Raten).

OUTPUT: Reines JSON, kein Markdown. Schema:

Wenn extrahierbar:
{{
  "found": true,
  "formation": "4-2-3-1" oder "4-3-3" oder "3-4-3" oder "4-4-2" usw.,
  "starters": [
    {{"position": "TW", "name": "Manuel Neuer", "number": 1}},
    {{"position": "RV", "name": "Konrad Laimer", "number": 24}},
    ... (alle 11),
  ],
  "bench": ["Sven Ulreich", "..."]  // optional, leeres Array wenn unklar
}}

Wenn nicht eindeutig:
{{"found": false, "reason": "Keine vollständige Startelf in den Quellen erkennbar"}}"""

    user = f"RSS-Texte zur Analyse:\n\n{items_str}\n\nExtrahiere die Bayern-Startelf falls eindeutig erkennbar."

    print("→ Claude API Call: Lineup-Extraktion …")
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    try:
        result = json.loads(text)
    except Exception as e:
        print(f"  ❌ JSON-Parse fehlgeschlagen: {e}")
        return None

    if not result.get("found"):
        print(f"  ⚠️  Lineup nicht eindeutig: {result.get('reason','—')}")
        return None

    # Validieren: alle Spieler im Kader?
    starters = result.get("starters") or []
    if len(starters) != 11:
        print(f"  ⚠️  Nicht 11 Spieler ({len(starters)}). Verwerfe.")
        return None
    for p in starters:
        nm = (p.get("name") or "").strip()
        if not any(k.lower() in nm.lower() or nm.lower() in k.lower() for k in BAYERN_KADER):
            print(f"  ⚠️  Spieler '{nm}' nicht im Kader. Verwerfe.")
            return None

    return result


# ============================================================================
# PRE-MATCH POST: VORBERICHT
# ============================================================================

def write_pre_match_post(match_info, lineup_data, recent_articles):
    """Vorbericht inkl. Aufstellung (wenn vorhanden)."""
    client = Anthropic(api_key=API_KEY)

    opp = match_info["opponent_name"]
    home = match_info["is_home"]
    kickoff_str = match_info["kickoff_local_str"]

    lineup_block = ""
    if lineup_data:
        starters_str = ", ".join(p["name"] for p in lineup_data.get("starters", []))
        lineup_block = f"\nBekannte Aufstellung ({lineup_data.get('formation','?')}): {starters_str}\n"

    recent_titles = "\n".join(f"- {a['title']}" for a in recent_articles[-6:])

    system = """Du bist Sport-Redakteur des FC-Bayern-Blogs Workuvision.de.
Schreibe einen kurzen, sachlichen VORBERICHT für ein Bayern-Spiel.

STIL:
- Seriöser Journalismus, KEIN Boulevard. Vergleichbar mit kicker oder Süddeutscher Sportteil.
- Dritte Person über Bayern: "die Münchner", "der FCB", "der Rekordmeister". KEIN "wir".
- Keine Floskeln (kein "Kracher", "wehtun", "Statement", "Honestly").
- Maximal 200 Wörter, drei kurze Absätze.

INHALT:
- Absatz 1: Spielansetzung (Anpfiff, Heim/Auswärts), Saison-Kontext.
- Absatz 2: Personalsituation / Aufstellung (wenn bekannt). Wer fehlt, wer kehrt zurück.
- Absatz 3: Was Bayern erwartet (kurze taktische Einordnung).

REGELN:
- Schreibe nur, was du sicher weißt. Erfinde keine Zahlen, Zitate, Spielernamen.
- Wenn Aufstellung NICHT bekannt: schreibe in Absatz 2 "Die Aufstellung steht noch aus."
- Korrekte Eigennamen: Vincent Kompany, Lennart Karl, Pavlović, Díaz.

OUTPUT: Reines JSON, kein Markdown. Schema:
{
  "title": "max. 65 Zeichen, sachlich (z.B. 'Vorbericht: Bayern empfängt Heidenheim')",
  "excerpt": "1-2 Sätze, max. 200 Zeichen",
  "body": "drei Absätze, getrennt durch \\n\\n"
}"""

    user = f"""Spiel: Bayern {('vs' if home else 'bei')} {opp}
Anpfiff: {kickoff_str}
{lineup_block}
Letzte Workuvision-Artikel (zur Stilreferenz, nicht wiederholen):
{recent_titles}

Schreibe den Vorbericht."""

    print("→ Claude API Call: Vorbericht …")
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


# ============================================================================
# POST-MATCH POST: SPIELBERICHT
# ============================================================================

def write_post_match_post(match_info, recent_articles):
    """Spielbericht mit Endergebnis + Toren + Quelle."""
    client = Anthropic(api_key=API_KEY)

    m = match_info["raw_match"]
    opp = match_info["opponent_name"]
    home = match_info["is_home"]

    # Endstand extrahieren
    results = m.get("matchResults") or []
    endresult = next((r for r in results if r.get("resultName") == "Endergebnis"), None) or (results[0] if results else None)
    if endresult:
        team1_score = endresult.get("pointsTeam1", 0)
        team2_score = endresult.get("pointsTeam2", 0)
    else:
        team1_score, team2_score = 0, 0

    bayern_score = team1_score if home else team2_score
    opp_score = team2_score if home else team1_score

    # Tore-Liste
    goals = m.get("goals") or []
    goals_text = []
    for g in goals:
        scorer = g.get("goalGetterName","?")
        minute = g.get("matchMinute","?")
        s1 = g.get("scoreTeam1", 0)
        s2 = g.get("scoreTeam2", 0)
        goals_text.append(f"{minute}': {scorer} ({s1}:{s2})")

    # Bayern-relevante News der letzten Stunden für mehr Kontext
    extra_context = []
    for fname, furl in FEEDS_FOR_LINEUP[:3]:
        try:
            f = feedparser.parse(furl)
            for entry in f.entries[:5]:
                title = entry.get("title","")
                if "bayern" in title.lower() or opp.lower() in title.lower():
                    summ = re.sub(r"<[^>]+>","", entry.get("summary","") or "")[:300]
                    extra_context.append(f"[{fname}] {title}: {summ}")
        except Exception:
            pass

    recent_titles = "\n".join(f"- {a['title']}" for a in recent_articles[-6:])

    system = """Du bist Sport-Redakteur des FC-Bayern-Blogs Workuvision.de.
Schreibe einen sachlichen SPIELBERICHT.

STIL:
- Seriöser Journalismus, KEIN Boulevard.
- Dritte Person über Bayern: "die Münchner", "der FCB". KEIN "wir".
- Keine Floskeln (kein "Kracher", "wehtun", "Statement", "Honestly").
- Maximal 250 Wörter, drei Absätze.

INHALT:
- Absatz 1: Endergebnis + grobe Spielzusammenfassung.
- Absatz 2: Schlüsselszenen (Tore, Wendepunkte, herausragende Spieler).
- Absatz 3: Einordnung (Saison-Kontext, was bedeutet das Ergebnis).

REGELN:
- Erfinde NICHTS. Nur was in den gegebenen Daten/News steht.
- Korrekte Eigennamen: Vincent Kompany, Lennart Karl, Pavlović, Díaz.
- Schreibe Tore-Schützen NUR wenn in den Goals-Daten vorhanden.

OUTPUT: Reines JSON, kein Markdown. Schema:
{
  "title": "max. 65 Zeichen, mit Ergebnis (z.B. 'FC Bayern 3:1 — Souveräner Sieg gegen Heidenheim')",
  "excerpt": "1-2 Sätze mit Endergebnis, max. 200 Zeichen",
  "body": "drei Absätze, getrennt durch \\n\\n"
}"""

    user = f"""Spiel beendet:
{m['team1']['teamName']} {team1_score} : {team2_score} {m['team2']['teamName']}

Tore (chronologisch):
{chr(10).join(goals_text) if goals_text else 'Keine Tore'}

Aus Bayern-Sicht: {('Heimsieg' if home and bayern_score > opp_score else 'Auswärtssieg' if not home and bayern_score > opp_score else 'Niederlage' if bayern_score < opp_score else 'Unentschieden')}

Begleitkontext aus Sport-News (zur Einordnung):
{chr(10).join(extra_context[:5]) if extra_context else '—'}

Letzte Workuvision-Artikel (Stilreferenz):
{recent_titles}

Schreibe den Spielbericht."""

    print("→ Claude API Call: Spielbericht …")
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


# ============================================================================
# MAIN
# ============================================================================

def add_article_to_content(content, post_data, kind, slug_suffix, source_url=None, source_name=None):
    """Fügt einen Artikel zu content.json hinzu, mit Validation."""
    auto_fix_names(post_data)
    problems = validate_post(post_data)
    if problems:
        print(f"  ❌ Validation fehlgeschlagen ({len(problems)}):")
        for p in problems[:5]:
            print(f"     • {p}")
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = post_data.get("title","")[:90]
    slug = f"{today}-{slug_suffix}"

    # Bilder pro kind — wähle das am wenigsten benutzte aus dem Pool um
    # Doppelungen mit existierenden Artikeln zu vermeiden.
    from collections import Counter
    import random as _random

    if kind == "preview":
        cat = "tactics"
        badge = "Vorbericht"
        badge_color = "bv"
        pool = ["img/stadium1.jpg", "img/tactics.jpg", "img/hero.jpg",
                "img/night.jpg", "img/football.jpg"]
    elif kind == "report":
        cat = "reaction"
        badge = "Spielbericht"
        badge_color = "brc"
        pool = ["img/fans.jpg", "img/stadium1.jpg", "img/hero.jpg",
                "img/football.jpg"]
    else:
        cat = "tactics"
        badge = "News"
        badge_color = "bt"
        pool = ["img/football.jpg", "img/tactics.jpg", "img/night.jpg",
                "img/transfer.jpg"]

    # Zähle wie oft jedes Stock-Bild bereits genutzt wird (alle Artikel)
    usage = Counter()
    for a in content.get("articles", []):
        t = a.get("thumbnail", "")
        if t and not t.startswith("img/articles/"):
            usage[t] += 1
    # Wähle das Bild mit niedrigster usage; tie-break: zufällig
    candidates = [(usage.get(p, 0), _random.random(), p) for p in pool]
    candidates.sort()
    thumb = candidates[0][2]

    article = {
        "slug": slug,
        "date": today,
        "category": cat,
        "badge": badge,
        "badgeColor": badge_color,
        "thumbnail": thumb,
        "thumbnailAlt": title,
        "readtime": "4 Min.",
        "featured": kind == "report",  # Spielbericht featured
        "title": title,
        "excerpt": post_data.get("excerpt","")[:250],
        "body": post_data.get("body",""),
    }
    # Vorbericht-Artikel kriegen ein Marker-Feld, damit das Frontend die
    # taktische Aufstellung (lineup-override.json) im Artikel anzeigen kann.
    if kind == "preview":
        article["showLineup"] = True
    if source_url:
        article["sourceUrl"] = source_url
    if source_name:
        article["sourceName"] = source_name

    articles = content.get("articles", [])

    # === DEDUP-CHECKS ===
    # 1) Genau gleicher Slug → ersetze (Update)
    existing_same_slug = next((a for a in articles if a.get("slug") == slug), None)
    if existing_same_slug:
        articles = [a for a in articles if a.get("slug") != slug]
        print(f"  ↻ Bestehenden Artikel mit Slug '{slug}' überschrieben.")

    # 2) Inhaltlich ähnlicher Titel in den letzten 15 Artikeln (verhindert
    # dass der Bot 2x leicht unterschiedliche Vorberichte zum selben Spiel
    # anlegt, z.B. wenn er mehrfach am Spieltag triggert)
    elif _is_title_dup(title, articles[:15], threshold=0.5):
        print(f"  ⚠️  Inhaltlich ähnlicher Artikel existiert bereits — übersprungen.")
        return False

    articles.insert(0, article)
    content["articles"] = articles[:30]
    return True


def _title_keywords(title):
    """Stichwörter aus Titel: Eigennamen + Wörter >=5 Zeichen ohne Stoppwörter."""
    if not title:
        return set()
    proper = re.findall(r"\b[A-ZÄÖÜ][a-zäöüß]{3,}\b", title)
    stop = {"sich","nach","trotz","über","diese","dieser","dieses","haben",
            "wieder","noch","auch","seine","seinen","seiner","ihrem","ihren",
            "ihrer","wird","werden","wurde","kann","nicht","keine","schon",
            "aber","doch","weil","damit","gegen","gegenüber","dabei","ohne",
            "bayern","fcb","münchen","muenchen","fc"}  # Bayern-Bezug ist überall
    words = re.findall(r"\b\w{5,}\b", title.lower())
    kws = set(p.lower() for p in proper)
    kws.update(w for w in words if w not in stop)
    return kws


def _is_title_dup(title, recent_articles, threshold=0.5):
    """Prüft ob title inhaltlich zu einem der recent_articles dupliziert."""
    new_kws = _title_keywords(title)
    if not new_kws:
        return False
    for a in recent_articles:
        existing_kws = _title_keywords(a.get("title", ""))
        if not existing_kws:
            continue
        intersect = len(new_kws & existing_kws)
        union = len(new_kws | existing_kws)
        if union > 0 and intersect / union >= threshold:
            print(f"     Duplikat-Match: '{title[:60]}' ↔ '{a.get('title','')[:60]}' (score={intersect/union:.2f})")
            return True
    return False


def main():
    print("=== Match-Reporter ===")
    if not CONTENT_FILE.exists():
        print("Keine content.json — Skip.")
        return

    content = load_content()
    tracker = content.setdefault("matchPostsTracker", {})  # match_id -> {"pre": True, "post": True}

    phase_info = get_match_phase()
    phase = phase_info["phase"]
    print(f"Match-Phase: {phase}")

    if phase == "idle":
        # Wenn lineup vorhanden aber kein Spiel mehr — currentLineup leeren
        if content.get("currentLineup"):
            content["currentLineup"] = None
            content["opponentLineup"] = None
            save_content(content)
            print("  Aufstellung gelöscht (kein Spiel in Reichweite).")
        return

    match = phase_info["match"]
    match_id = phase_info["match_id"]

    # Match-Info aufbereiten
    is_home = "bayern" in (match.get("team1",{}).get("teamName","")).lower()
    opp = match["team2"] if is_home else match["team1"]
    opp_name = opp.get("shortName") or opp.get("teamName") or "TBD"
    kickoff = phase_info["kickoff"]
    # In MEZ/MESZ konvertieren wäre besser, hier vereinfacht in UTC
    try:
        kickoff_local = kickoff.astimezone(timezone(timedelta(hours=2)))  # MESZ
        kickoff_local_str = kickoff_local.strftime("%A, %d.%m.%Y um %H:%M Uhr")
    except Exception:
        kickoff_local_str = kickoff.strftime("%d.%m.%Y %H:%M UTC")

    match_info = {
        "raw_match": match,
        "match_id": match_id,
        "opponent_name": opp_name,
        "is_home": is_home,
        "kickoff_local_str": kickoff_local_str,
    }

    state = tracker.setdefault(match_id, {})

    # ===== PRE-MATCH =====
    if phase == "pre":
        # Lineup-Scraper hat möglicherweise schon eine Aufstellung gesetzt —
        # in dem Fall verwenden wir die für den Vorbericht-Kontext.
        existing_lineup = content.get("currentLineup")
        scraper_lineup_present = existing_lineup and existing_lineup.get("starters") and len(existing_lineup["starters"]) == 11

        if scraper_lineup_present:
            print("  ✓ Aufstellung wurde bereits vom kicker-Scraper geladen — nutze sie.")
            lineup = {
                "formation": existing_lineup.get("formation",""),
                "starters": existing_lineup.get("starters", []),
                "bench": existing_lineup.get("bench", []),
            }
        else:
            # Fallback: RSS-basierte Lineup-Extraktion
            candidates = fetch_lineup_candidates(opp_name)
            if candidates:
                print(f"  → {len(candidates)} Lineup-Kandidaten in RSS gefunden.")
                try:
                    lineup = extract_lineup_via_claude(candidates, match_info)
                except Exception as e:
                    print(f"  ❌ Lineup-Extraktion fehlgeschlagen: {e}")
                    lineup = None
            else:
                print("  Keine Aufstellungs-Hinweise in RSS.")
                lineup = None

            # 2) Aufstellung in content.json (nur wenn RSS-Quelle was lieferte)
            if lineup:
                content["currentLineup"] = {
                    "matchTitle": f"Bayern {('vs' if is_home else 'bei')} {opp_name}",
                    "matchLabel": "Aufstellung · Startelf",
                    "matchDate": kickoff.isoformat(),
                    "publishedAt": datetime.now(timezone.utc).isoformat(),
                    "formation": lineup.get("formation", ""),
                    "starters": lineup.get("starters", []),
                    "bench": lineup.get("bench", []),
                    "coach": "Vincent Kompany",
                    "notes": "",
                    "teamName": "FC Bayern München",
                    "sourceName": "RSS-Aggregation",
                }
                print(f"  ✓ Aufstellung in content.json ({lineup.get('formation','?')}).")

        # 3) Vorbericht-Post — nur einmal pro Match
        if state.get("pre"):
            print("  Vorbericht für dieses Spiel bereits geschrieben.")
        else:
            try:
                preview = write_pre_match_post(match_info, lineup, content.get("articles", []))
                slug_suffix = f"vorbericht-bayern-{slugify(opp_name)}"
                ok = add_article_to_content(content, preview, "preview", slug_suffix)
                if ok:
                    state["pre"] = True
                    print("  ✓ Vorbericht hinzugefügt.")
            except Exception as e:
                print(f"  ❌ Vorbericht fehlgeschlagen: {e}")

        save_content(content)

    # ===== LIVE =====
    elif phase == "live":
        print("  Spiel läuft — Frontend zeigt Live-Stand. Bot wartet.")

    # ===== POST-MATCH =====
    elif phase == "post":
        # currentLineup + opponentLineup löschen — Spiel ist vorbei
        if content.get("currentLineup"):
            content["currentLineup"] = None
            print("  Aufstellung gelöscht (Spiel beendet).")
        if content.get("opponentLineup"):
            content["opponentLineup"] = None

        if state.get("post"):
            print("  Spielbericht für dieses Spiel bereits geschrieben.")
        else:
            try:
                report = write_post_match_post(match_info, content.get("articles", []))
                slug_suffix = f"spielbericht-bayern-{slugify(opp_name)}"
                ok = add_article_to_content(content, report, "report", slug_suffix)
                if ok:
                    state["post"] = True
                    print("  ✓ Spielbericht hinzugefügt.")
            except Exception as e:
                print(f"  ❌ Spielbericht fehlgeschlagen: {e}")

        save_content(content)

    # Tracker bereinigen: alte Match-IDs raus (älter als 7 Tage)
    # — vereinfacht: behalten wir einfach alle, ist nur ein paar KB

    print("=== Match-Reporter fertig ===")


if __name__ == "__main__":
    main()
