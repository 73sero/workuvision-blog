"""kicker.de Lineup-Scraper für Workuvision.

Holt die Aufstellung des nächsten Bayern-Spiels von kicker.de —
sowohl Bayern als auch den Gegner.

Workflow:
  1. Bayern's nächstes/laufendes Spiel finden (OpenLigaDB für BL, kicker für CL)
  2. kicker-Spielplan crawlen → Spiel-URL extrahieren
  3. /aufstellung-Seite holen
  4. Beide Mannschaften parsen (top/left + bottom/right)
  5. In content.json.currentLineup (Bayern) und .opponentLineup (Gegner) speichern
  6. Bei Erfolg → Lineup-Sektion auf Frontend wird sichtbar
"""
import html as html_lib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

KICKER_BASE = "https://www.kicker.de"

# Position-Mapping basierend auf Linien-Index nach hinten-vorne
# (für Notwendigkeit, Position-Strings wie "TW", "RV", "ZM" zuzuordnen)
def position_from_layout(line_idx, total_lines, idx_in_line, line_size):
    """Gibt Position-String basierend auf Spielfeld-Position."""
    if line_idx == 0:
        return "TW"
    # Sturmreihe (vorderste Reihe, line_size 1-3)
    if line_idx == total_lines - 1:
        if line_size == 1:
            return "ST"
        if line_size == 2:
            return ["LS", "RS"][idx_in_line]
        if line_size == 3:
            return ["LS", "ST", "RS"][idx_in_line]
        return "ST"
    # Abwehr-Linie: RV/IV/LV
    if line_idx == 1:
        if line_size == 4:
            return ["LV","IV","IV","RV"][idx_in_line]
        elif line_size == 3:
            return ["LIV","IV","RIV"][idx_in_line]
        elif line_size == 5:
            return ["LV","IV","IV","IV","RV"][idx_in_line]
        return "AB"
    # Mittelfeld
    if line_idx < total_lines - 1:
        if line_size == 1:
            return "ZM"
        if line_size == 2:
            return ["ZDM","ZDM"][idx_in_line]
        if line_size == 3:
            return ["LM","ZM","RM"][idx_in_line]
        if line_size == 4:
            return ["LM","ZM","ZM","RM"][idx_in_line]
        if line_size == 5:
            return ["LM","ZM","ZM","ZM","RM"][idx_in_line]
        return "MF"
    return "?"


def fetch(url, timeout=20):
    """Ruft eine URL mit Browser-ähnlichen Headern auf — robust gegen kicker-403."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = r.read()
        # gzip transparent decoden falls Server's so liefert
        if content[:2] == b'\x1f\x8b':
            import gzip
            content = gzip.decompress(content)
        return content.decode("utf-8", errors="replace")


# ============================================================================
# FCBAYERN.COM Scraper (primäre Quelle — nutzt Opta Sports Daten)
# ============================================================================

def find_fcbayern_match_url(kickoff_dt, opponent_name):
    """Konstruiert die fcbayern.com Aufstellungs-URL für ein bestimmtes Spiel.

    Pattern:
      https://fcbayern.com/de/spiele/profis/{liga}/{season}/{slug}/aufstellung

    Beispiel:
      https://fcbayern.com/de/spiele/profis/bundesliga/2025-2026/
      fc-bayern-muenchen-1-fc-heidenheim-1846-02-05-2026/aufstellung

    Wir leiten den Slug aus opponent_name + Datum ab.
    """
    # Saison ermitteln (Bundesliga-Saison startet im August)
    year = kickoff_dt.year
    if kickoff_dt.month >= 7:
        season = f"{year}-{year+1}"
    else:
        season = f"{year-1}-{year}"

    # Datum DD-MM-YYYY
    date_str = kickoff_dt.strftime("%d-%m-%Y")

    # Opponent-Slug bauen — fcbayern nutzt Volltexte mit Bindestrich
    opp_clean = opponent_name.lower()
    # Bekannte Mappings für korrekte Slugs
    opp_slug_map = {
        "1. fc heidenheim 1846": "1-fc-heidenheim-1846",
        "1. fc heidenheim": "1-fc-heidenheim-1846",
        "fc heidenheim": "1-fc-heidenheim-1846",
        "heidenheim": "1-fc-heidenheim-1846",
        "borussia dortmund": "borussia-dortmund",
        "bvb": "borussia-dortmund",
        "bayer 04 leverkusen": "bayer-04-leverkusen",
        "bayer leverkusen": "bayer-04-leverkusen",
        "leverkusen": "bayer-04-leverkusen",
        "rb leipzig": "rb-leipzig",
        "leipzig": "rb-leipzig",
        "eintracht frankfurt": "eintracht-frankfurt",
        "frankfurt": "eintracht-frankfurt",
        "vfb stuttgart": "vfb-stuttgart",
        "stuttgart": "vfb-stuttgart",
        "vfl wolfsburg": "vfl-wolfsburg",
        "wolfsburg": "vfl-wolfsburg",
        "borussia mönchengladbach": "borussia-monchengladbach",
        "mönchengladbach": "borussia-monchengladbach",
        "gladbach": "borussia-monchengladbach",
        "1. fc union berlin": "1-fc-union-berlin",
        "union berlin": "1-fc-union-berlin",
        "tsg hoffenheim": "tsg-hoffenheim",
        "hoffenheim": "tsg-hoffenheim",
        "fc augsburg": "fc-augsburg",
        "augsburg": "fc-augsburg",
        "sv werder bremen": "sv-werder-bremen",
        "werder bremen": "sv-werder-bremen",
        "bremen": "sv-werder-bremen",
        "1. fsv mainz 05": "1-fsv-mainz-05",
        "mainz": "1-fsv-mainz-05",
        "1. fc köln": "1-fc-koln",
        "köln": "1-fc-koln",
        "fc st. pauli": "fc-st-pauli",
        "st. pauli": "fc-st-pauli",
        "hamburger sv": "hamburger-sv",
        "hsv": "hamburger-sv",
        "sc freiburg": "sc-freiburg",
        "freiburg": "sc-freiburg",
    }
    opp_slug = opp_slug_map.get(opp_clean, opp_clean.replace(" ", "-").replace(".", ""))

    bayern_slug = "fc-bayern-muenchen"
    full_slug = f"{bayern_slug}-{opp_slug}-{date_str}"
    return f"https://fcbayern.com/de/spiele/profis/bundesliga/{season}/{full_slug}/aufstellung"


def parse_fcbayern_lineup(html_text):
    """Parst die fcbayern.com /aufstellung-Seite.

    Returns: dict mit teamA, teamB:
      { "fc bayern": {"team_name":..., "formation":..., "players":[...]},
        "heidenheim": ... }
    """
    teams_out = {}

    # Section-Header finden: "Aufstellung {TEAMNAME}" 
    matches = list(re.finditer(
        r'Aufstellung\s+([^<\n]{3,40}?)\s*<', html_text
    ))

    sections = []
    valid_team_keywords = ["FC Bayern", "Heidenheim", "Borussia", "Leverkusen",
                            "Leipzig", "Eintracht", "VfB", "VfL", "Union",
                            "Hoffenheim", "Augsburg", "Werder", "Mainz",
                            "Köln", "Pauli", "Hamburger", "Freiburg",
                            "Stuttgart", "Wolfsburg", "Mönchengladbach",
                            "Heidenheim 1846"]

    for i, m in enumerate(matches):
        team_name = m.group(1).strip()
        if not any(kw in team_name for kw in valid_team_keywords):
            continue
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(html_text)
        sect = html_text[start:end]
        # Schneide bei "Die taktische Aufstellung" ab
        cut = sect.find("Die taktische Aufstellung")
        if cut > 0:
            sect = sect[:cut]
        sections.append((team_name, sect))

    # De-Dup: dasselbe Team kann mehrfach auftauchen (weil fcbayern die Liste
    # zweimal zeigt). Nimm die erste vollständige Sektion.
    seen_team_keys = set()
    for team_name, sect_html in sections:
        team_key = team_name.lower()
        if team_key in seen_team_keys:
            continue

        # Formation extrahieren
        form_m = re.search(r'(\d)\s*-\s*(\d)\s*-\s*(\d)(?:\s*-\s*(\d))?', sect_html)
        formation = "-".join(g for g in form_m.groups() if g) if form_m else None
        if not formation:
            continue

        expected_count = 1 + sum(int(d) for d in formation.split("-"))

        # Spieler extrahieren — Reihenfolge im HTML beibehalten!
        # fcbayern listet: erst <a href="/de/teams/profis/...">Player</a>,
        # zwischendrin (für Spieler ohne Profil): <li>Player</li> ohne <a>
        # Wir parsen sequenziell durch <li>-Items.

        players = []
        # Strategie: Tags strippen aber List-Item-Grenzen markieren
        # In fcbayern's HTML stehen Players in <li>...</li>. Innerhalb:
        # <a href="/de/teams/profis/SLUG">{img}{number}{name}</a>
        # ODER plain: {img}{number}{name}

        # Schrittweise: alle <li>-Blöcke finden
        # Unterschiedliche List-Layouts; einfacher: zerlege an <li> Tags
        items = re.split(r'<li[^>]*>', sect_html)
        for item in items[1:]:  # erstes Element vor <li> ignorieren
            # Inneren Text bis </li> nehmen
            end_li = item.find('</li>')
            if end_li > 0:
                item = item[:end_li]

            # Zwei Fälle: mit <a href="/de/teams/profis/.."> oder ohne
            inner_text = re.sub(r'<[^>]+>', ' ', item)
            inner_text = re.sub(r'\s+', ' ', inner_text).strip()
            # Pattern: "{nummer} {name}" oder "{name} {nummer}" — meist Erstes
            m_player = re.match(r'^(\d{1,2})\s+([A-ZÀ-ÿ][\w.\'\-šžćčľń]*(?:\s+[A-ZÀ-ÿ][\w.\'\-šžćčľń]*)?)$', inner_text)
            if m_player:
                num = int(m_player.group(1))
                name = m_player.group(2).strip()
                if 1 <= num <= 99 and len(name) >= 3:
                    players.append({"number": num, "name": name})
                    if len(players) >= expected_count:
                        break

        # Fallback: wenn keine <li>-Struktur, regex über text
        if len(players) < expected_count:
            text = re.sub(r'<[^>]+>', ' ', sect_html)
            text = re.sub(r'\s+', ' ', text).strip()
            # Nimm "(\d{1,2})\s+([A-ZÄÖÜ][\w...])"
            seen_nums = {p["number"] for p in players}
            for m_p in re.finditer(
                r'(\d{1,2})\s+([A-ZÀ-ÿ][\w.\'\-šžćčľń]+(?:\s+[A-ZÀ-ÿ][\w.\'\-šžćčľń]+)?)',
                text
            ):
                num = int(m_p.group(1))
                name = m_p.group(2).strip()
                if num in seen_nums or num < 1 or num > 99:
                    continue
                if len(name) < 3:
                    continue
                # Filter: schließe Worte aus die offenbar Header sind
                if name.lower() in ("aufstellung","fc","sv","tv","spieltag"):
                    continue
                players.append({"number": num, "name": name})
                seen_nums.add(num)
                if len(players) >= expected_count:
                    break

        # Auf expected_count limitieren
        players = players[:expected_count]

        teams_out[team_key] = {
            "team_name": team_name,
            "formation": formation,
            "players": players,
        }
        seen_team_keys.add(team_key)

    return teams_out


def assign_positions_from_formation(players, formation_str):
    """Weist jedem Spieler eine Position-String basierend auf seiner Reihenfolge
    in der Aufstellung und der Formation zu.

    fcbayern listet Spieler in Reihen-Reihenfolge: TW, dann Verteidigung,
    dann Mittelfeld, dann Sturm — innerhalb einer Reihe von links nach rechts
    (oder nach Trikotnummer? meist top→bottom auf der grafischen Darstellung,
    bei fcbayern aber meistens links→rechts in der textuellen Liste).
    """
    if not formation_str:
        return players

    parts = list(map(int, formation_str.split("-")))
    expected_total = 1 + sum(parts)
    if len(players) != expected_total:
        # Anzahl passt nicht zu Formation → keine Zuweisung
        return players

    out = []
    # TW
    out.append({**players[0], "position": "TW"})
    idx = 1
    for line_idx, line_size in enumerate(parts, start=1):
        for in_line_idx in range(line_size):
            pos = position_from_layout(line_idx, len(parts) + 1, in_line_idx, line_size)
            out.append({**players[idx], "position": pos})
            idx += 1
    return out


def fetch_fcbayern_lineup(match_info):
    """Versucht die Aufstellung von fcbayern.com zu holen.

    Returns: (bayern_lineup_dict, opponent_lineup_dict, source_url) oder (None, None, None)
    """
    url = find_fcbayern_match_url(match_info["kickoff"], match_info["opponent_name"])
    print(f"  → fcbayern.com URL: {url}")
    try:
        html = fetch(url)
    except Exception as e:
        print(f"  ⚠️  fcbayern.com nicht ladbar: {e}")
        return None, None, None

    teams = parse_fcbayern_lineup(html)
    print(f"  fcbayern.com gefundene Teams: {list(teams.keys())}")

    if len(teams) < 2:
        print(f"  ⚠️  fcbayern.com hat nur {len(teams)} Teams — Aufstellung evtl. noch nicht da.")
        return None, None, None

    # Bayern-Team finden
    bayern_key = next((k for k in teams if "bayern" in k.lower()), None)
    if not bayern_key:
        print("  ⚠️  Bayern nicht in fcbayern-Daten gefunden.")
        return None, None, None

    opp_key = next((k for k in teams if k != bayern_key), None)
    if not opp_key:
        return None, None, None

    bayern = teams[bayern_key]
    opp = teams[opp_key]

    # Mindestens 11 Spieler pro Team
    if len(bayern["players"]) < 11 or len(opp["players"]) < 11:
        print(f"  ⚠️  Unvollständig: Bayern {len(bayern['players'])}, Gegner {len(opp['players'])}")
        return None, None, None

    # Positionen zuweisen
    bayern_with_pos = assign_positions_from_formation(bayern["players"][:11], bayern["formation"])
    opp_with_pos = assign_positions_from_formation(opp["players"][:11], opp["formation"])

    bayern_lineup = {
        "active": True,
        "teamName": bayern["team_name"],
        "formation": bayern["formation"],
        "starters": bayern_with_pos,
        "bench": [],  # fcbayern listet Bank separat; kann im 2. Pass befüllt werden
        "coach": "Vincent Kompany",
        "sourceName": "fcbayern.com (Opta Sports)",
        "sourceUrl": url,
    }
    opp_lineup = {
        "teamName": opp["team_name"],
        "formation": opp["formation"],
        "starters": opp_with_pos,
        "sourceName": "fcbayern.com (Opta Sports)",
        "sourceUrl": url,
    }
    return bayern_lineup, opp_lineup, url


def find_kicker_match_url(opponent_hint=None, league="bundesliga"):
    """Sucht in kicker.de's Bayern-Spielplan die nächste Spiel-URL."""
    spielplan_url = f"{KICKER_BASE}/fc-bayern-muenchen/spielplan/2025-26"
    try:
        html = fetch(spielplan_url)
    except Exception as e:
        print(f"  ⚠️  kicker-Spielplan nicht erreichbar: {e}")
        return None

    # Extrahiere alle Bayern-Spiel-URLs in chronologischer Reihenfolge
    # Pattern: /bayern-gegen-XXX oder /XXX-gegen-bayern (Heim/Auswärts)
    matches = re.findall(
        r'href="(/[^"]*(?:bayern-gegen|gegen-bayern)[^"]+\d{4}-[a-z-]+-\d+)/[a-z]+"',
        html,
        re.IGNORECASE,
    )
    # Liga-Filter
    matches = [m for m in matches if league in m or "champions-league" in m]

    # Dedup, behalte Reihenfolge
    seen = set()
    unique = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)

    if opponent_hint:
        opp_lc = opponent_hint.lower()
        for m in unique:
            if opp_lc in m.lower():
                return KICKER_BASE + m + "/aufstellung"

    # Fallback: erstes ungespieltes — wir wissen aber nicht welches gespielt ist
    # Nimm das Spiel, das nicht "/analyse" als URL hat (Analyse erscheint nach Spiel)
    return KICKER_BASE + unique[0] + "/aufstellung" if unique else None


def parse_lineup_html(html):
    """Parst eine kicker-Aufstellungsseite und gibt {team_slug: [players]} zurück."""
    teams = {}

    def extract(coord1, coord2, tactical, href, inner, is_bottom):
        # Trikotnummer
        num_m = re.search(r'shirt-number">\s*(\d+)\s*<', inner)
        # Name (Kurzname)
        name_m = re.search(r'lineup-player-card__name">\s*([^<]+?)\s*</span>', inner, re.DOTALL)
        # Voller Name (alt-Text vom Bild)
        alt_m = re.search(r'alt="([^"]+)"', inner)

        if name_m:
            short_name = html_lib.unescape(name_m.group(1).strip())
        elif alt_m:
            short_name = html_lib.unescape(alt_m.group(1).strip())
        else:
            slug = href.lstrip('/').split('/')[0]
            short_name = slug.replace('-', ' ').title()

        full_name = html_lib.unescape(alt_m.group(1).strip()) if alt_m else short_name

        # Bottom-Cards spiegeln
        if is_bottom:
            top = 100 - coord1
            left = 100 - coord2
        else:
            top = coord1
            left = coord2

        team_slug = href.strip('/').split('/')[-1]
        return team_slug, {
            'tactical': tactical,
            'top': top, 'left': left,
            'number': int(num_m.group(1)) if num_m else None,
            'name': short_name,
            'fullName': full_name,
            'href': href,
        }

    # TOP team (heim oder auswärts, je nach kicker-Layout)
    pattern_top = re.compile(
        r'<a class="kick__lineup-player-card"\s+'
        r'style="top:\s*([-\d.]+)%;\s*left:\s*([-\d.]+)%;"\s+'
        r'data-tactical="(\d+)"\s+href="([^"]+)">(.*?)</a>',
        re.DOTALL,
    )
    for m in pattern_top.finditer(html):
        team, p = extract(float(m.group(1)), float(m.group(2)),
                          int(m.group(3)), m.group(4), m.group(5),
                          is_bottom=False)
        teams.setdefault(team, []).append(p)

    # BOTTOM team
    pattern_bottom = re.compile(
        r'<a class="kick__lineup-player-card"\s+'
        r'style="bottom:\s*([-\d.]+)%;\s*right:\s*([-\d.]+)%;"\s+'
        r'data-tactical="(\d+)"\s+href="([^"]+)">(.*?)</a>',
        re.DOTALL,
    )
    for m in pattern_bottom.finditer(html):
        team, p = extract(float(m.group(1)), float(m.group(2)),
                          int(m.group(3)), m.group(4), m.group(5),
                          is_bottom=True)
        teams.setdefault(team, []).append(p)

    return teams


def to_lineup_dict(team_slug, players, team_display_name, opponent_display_name=None):
    """Konvertiert in das content.json-currentLineup-Schema.
    Sortierung: vom eigenen Tor (TW) nach vorne (ST).

    Heuristik für TW-Erkennung:
    - kicker setzt TW auf `top: -5%` (top half) oder `top: 105%` (bottom half nach Spiegel).
    - Heißt: TW ist die Linie mit dem extremsten top-Wert (kleinster ODER größter).
    - Der Sturm liegt im inneren (zwischen 20% und 80%).
    """
    if not players:
        return None

    by_top_asc = sorted(players, key=lambda x: x['top'])

    # Cluster
    def cluster_lines(sorted_players):
        if not sorted_players:
            return []
        lines = [[sorted_players[0]]]
        for p in sorted_players[1:]:
            if abs(p['top'] - lines[-1][-1]['top']) < 8:
                lines[-1].append(p)
            else:
                lines.append([p])
        return lines

    lines_asc = cluster_lines(by_top_asc)

    # Welches Ende ist TW?
    # Top-Half-Mannschaft: TW bei kleinstem top (-5%), ST bei größtem top (~73%)
    # Bottom-Half-Mannschaft (gespiegelt): TW bei größtem top (105%), ST bei kleinstem (~27%)
    # Entscheidung: TW-Linie hat top-Wert außerhalb 0-100
    first_line_top = lines_asc[0][0]['top']
    last_line_top = lines_asc[-1][0]['top']

    # Wenn die letzte Linie weiter außerhalb (z.B. 105) liegt als die erste (z.B. 27),
    # dann ist die letzte Linie der TW → umkehren.
    if last_line_top > 100 and first_line_top >= 0:
        ordered_lines = list(reversed(lines_asc))
    elif first_line_top < 0 and last_line_top <= 100:
        # Top-Half: erste Linie ist TW (-5%) — schon richtig
        ordered_lines = lines_asc
    else:
        # Fallback: extremerer Wert vorne
        if abs(first_line_top - 50) > abs(last_line_top - 50):
            ordered_lines = lines_asc
        else:
            ordered_lines = list(reversed(lines_asc))

    formation = '-'.join(str(len(l)) for l in ordered_lines[1:]) if len(ordered_lines) > 1 else ''

    starters = []
    for line_idx, line in enumerate(ordered_lines):
        line_sorted = sorted(line, key=lambda x: x['left'])
        for idx_in_line, p in enumerate(line_sorted):
            pos = position_from_layout(line_idx, len(ordered_lines), idx_in_line, len(line))
            starters.append({
                "position": pos,
                "name": p['fullName'] or p['name'],
                "number": p['number'],
            })

    return {
        "teamSlug": team_slug,
        "teamName": team_display_name,
        "opponent": opponent_display_name or "",
        "formation": formation,
        "starters": starters,
        "bench": [],
        "coach": "Vincent Kompany" if "bayern" in team_slug.lower() else "",
    }


def detect_formation(players):
    """Wird nicht mehr separat gebraucht — to_lineup_dict macht's intern."""
    return ""


def find_bayern_team_slug(teams_dict):
    for slug in teams_dict:
        if "bayern" in slug.lower():
            return slug
    return None


def main():
    print("=== kicker Lineup-Scraper ===")

    if not CONTENT_FILE.exists():
        print("Keine content.json — Skip.")
        return

    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        content = json.load(f)

    # 1) Bayern's nächstes Spiel finden (BL via OpenLigaDB ODER CL via kicker-Spielplan)
    bayern_match_info = None
    try:
        bl_data = json.loads(fetch("https://api.openligadb.de/getmatchdata/bl1/2025"))
        now = datetime.now(timezone.utc)
        upcoming = []
        for m in bl_data:
            t1 = m.get("team1", {}).get("teamName", "")
            t2 = m.get("team2", {}).get("teamName", "")
            if "bayern" not in (t1+t2).lower():
                continue
            try:
                kickoff = datetime.fromisoformat(m["matchDateTime"].replace("Z","+00:00"))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
            except:
                continue
            delta = (kickoff - now).total_seconds()
            # Aufstellung interessiert uns nur 6h vor bis 1h nach Anpfiff
            if -1*3600 <= delta <= 6*3600 and not m.get("matchIsFinished"):
                upcoming.append((kickoff, m))
        upcoming.sort(key=lambda x: x[0])
        if upcoming:
            kickoff, match = upcoming[0]
            is_home = "bayern" in match.get("team1",{}).get("teamName","").lower()
            opp = match["team2"] if is_home else match["team1"]
            bayern_match_info = {
                "opponent_name": opp.get("shortName") or opp.get("teamName") or "TBD",
                "kickoff": kickoff,
                "is_home": is_home,
                "competition": "Bundesliga",
            }
    except Exception as e:
        print(f"  ⚠️  OpenLigaDB-Check fehlgeschlagen: {e}")

    if not bayern_match_info:
        print("  Kein Bayern-Spiel in den nächsten 6 Stunden — kein Lineup-Update.")
        # Aufstellung nur leeren wenn wir wirklich eine vom Bot drin haben
        # (also matchDate liegt in der Vergangenheit oder nicht gesetzt)
        cur = content.get("currentLineup")
        should_clear = False
        if cur and cur.get("matchDate"):
            try:
                from datetime import datetime as _dt, timezone as _tz
                match_date = _dt.fromisoformat(cur["matchDate"].replace("Z","+00:00"))
                if match_date.tzinfo is None:
                    match_date = match_date.replace(tzinfo=_tz.utc)
                # Wenn das Spiel mehr als 5 Stunden her ist → leeren
                hours_since = (_dt.now(_tz.utc) - match_date).total_seconds() / 3600
                if hours_since > 5:
                    should_clear = True
            except Exception:
                pass
        if should_clear:
            content["currentLineup"] = None
            content["opponentLineup"] = None
            with CONTENT_FILE.open("w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            print("  currentLineup geleert (Spiel vorbei, kein neues in Reichweite).")
        return

    print(f"  Spiel: Bayern {('vs' if bayern_match_info['is_home'] else 'bei')} {bayern_match_info['opponent_name']}")
    print(f"  Anpfiff: {bayern_match_info['kickoff'].isoformat()}")

    # === 2a) PRIMÄRE QUELLE: fcbayern.com (Opta Sports) ===
    # Bei Heimspielen in der Bundesliga ist fcbayern.com die autoritative Quelle —
    # die Daten stammen direkt von Opta Sports. Bei Auswärts/CL evtl. nicht
    # verfügbar, dann fällt es auf kicker zurück.
    if bayern_match_info["competition"].lower() == "bundesliga":
        print("\n  → Versuche fcbayern.com (Opta Sports)…")
        bayern_lu, opp_lu, src_url = fetch_fcbayern_lineup(bayern_match_info)
        if bayern_lu and opp_lu:
            # Match-Daten ergänzen
            bayern_lu["matchTitle"] = f"Bayern {('vs' if bayern_match_info['is_home'] else 'bei')} {bayern_match_info['opponent_name']}"
            bayern_lu["matchLabel"] = "Aufstellung · Startelf"
            bayern_lu["matchDate"] = bayern_match_info["kickoff"].isoformat()
            bayern_lu["matchTime"] = bayern_match_info["kickoff"].isoformat()
            bayern_lu["publishedAt"] = datetime.now(timezone.utc).isoformat()

            content["currentLineup"] = bayern_lu
            content["opponentLineup"] = opp_lu

            # lineup-override.json überschreiben (Frontend nutzt diese Datei direkt)
            override_path = ROOT / "lineup-override.json"
            with override_path.open("w", encoding="utf-8") as f:
                json.dump(bayern_lu, f, ensure_ascii=False, indent=2)
            print(f"  ✓ lineup-override.json aus fcbayern.com geschrieben.")

            with CONTENT_FILE.open("w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            print(f"  ✅ Aufstellung von fcbayern.com (Opta) übernommen.")
            print(f"     Bayern: {bayern_lu['formation']} · {len(bayern_lu['starters'])} Spieler")
            print(f"     Gegner: {opp_lu['formation']} · {len(opp_lu['starters'])} Spieler")
            return
        print("  ⚠️  fcbayern.com nicht erfolgreich — versuche kicker.de als Fallback.")

    # === 2b) FALLBACK: kicker.de ===
    # 2) kicker-Aufstellungs-URL finden
    opp_hint = bayern_match_info["opponent_name"].lower().split()[0]  # "1. fsv mainz" → "fsv"
    if opp_hint in ("1.", "fsv"):
        opp_hint = bayern_match_info["opponent_name"].lower().split()[1] if len(bayern_match_info["opponent_name"].split()) > 1 else opp_hint

    lineup_url = find_kicker_match_url(opp_hint, league=bayern_match_info["competition"].lower())
    if not lineup_url:
        print("  ⚠️  Konnte kicker-Aufstellungs-URL nicht finden.")
        return
    print(f"  Aufstellungs-URL: {lineup_url}")

    # 3) Aufstellungs-Seite holen
    try:
        page = fetch(lineup_url)
    except Exception as e:
        print(f"  ⚠️  Aufstellungs-Seite nicht ladbar: {e}")
        return

    # 4) Beide Mannschaften parsen
    teams = parse_lineup_html(page)
    print(f"  Geparste Teams: {list(teams.keys())}")

    if len(teams) < 2:
        print("  ⚠️  Weniger als 2 Mannschaften gefunden — wahrscheinlich noch keine Aufstellung gepostet.")
        return

    bayern_slug = find_bayern_team_slug(teams)
    if not bayern_slug:
        print("  ⚠️  Bayern nicht in den extrahierten Teams gefunden.")
        return

    bayern_players = teams[bayern_slug]
    if len(bayern_players) != 11:
        print(f"  ⚠️  Bayern hat {len(bayern_players)} Spieler statt 11 — verwerfe.")
        return

    # Gegner ist das andere Team
    opp_slug = next((s for s in teams if s != bayern_slug), None)
    opp_players = teams.get(opp_slug, []) if opp_slug else []

    # 5) In content.json schreiben
    bayern_lineup = to_lineup_dict(
        bayern_slug, bayern_players,
        team_display_name="FC Bayern München",
        opponent_display_name=bayern_match_info["opponent_name"]
    )
    bayern_lineup["matchTitle"] = f"Bayern {('vs' if bayern_match_info['is_home'] else 'bei')} {bayern_match_info['opponent_name']}"
    bayern_lineup["matchLabel"] = "Aufstellung · Startelf"
    bayern_lineup["matchDate"] = bayern_match_info["kickoff"].isoformat()
    bayern_lineup["publishedAt"] = datetime.now(timezone.utc).isoformat()
    bayern_lineup["sourceUrl"] = lineup_url
    bayern_lineup["sourceName"] = "kicker.de"

    content["currentLineup"] = bayern_lineup

    if opp_players and len(opp_players) == 11:
        opp_lineup = to_lineup_dict(
            opp_slug, opp_players,
            team_display_name=bayern_match_info["opponent_name"],
            opponent_display_name="FC Bayern München"
        )
        content["opponentLineup"] = opp_lineup
        print(f"  ✓ Beide Aufstellungen gespeichert ({bayern_lineup['formation']} vs {opp_lineup['formation']})")
    else:
        content["opponentLineup"] = None
        print(f"  ✓ Nur Bayern-Aufstellung ({bayern_lineup['formation']}), Gegner unvollständig.")

    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    print("=== Fertig ===")


if __name__ == "__main__":
    main()
