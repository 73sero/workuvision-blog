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
    if line_idx == total_lines - 1:
        return "ST"
    # Abwehr-Linie: RV/IV/LV
    if line_idx == 1:
        if line_size == 4:
            return ["LV","IV","IV","RV"][idx_in_line]
        elif line_size == 3:
            return ["LV","IV","RV"][idx_in_line]
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
        # Wenn currentLineup veraltet → leeren
        if content.get("currentLineup"):
            content["currentLineup"] = None
            content["opponentLineup"] = None
            with CONTENT_FILE.open("w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            print("  currentLineup geleert (kein anstehendes Spiel).")
        return

    print(f"  Spiel: Bayern {('vs' if bayern_match_info['is_home'] else 'bei')} {bayern_match_info['opponent_name']}")
    print(f"  Anpfiff: {bayern_match_info['kickoff'].isoformat()}")

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
