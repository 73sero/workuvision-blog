"""Generiert die Ticker-Items in content.json aus aktuellen Inhalten:
- die letzten 5 Artikel-Titel (gekürzt)
- Bayerns nächster BL-Gegner aus OpenLigaDB (wenn binnen 7 Tagen)
- 1-2 fixe Brand-Items
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"

UA = "Mozilla/5.0 (Workuvision-Bot)"


def fetch_json(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠️  {url} nicht ladbar: {e}")
        return None


def shorten(title, limit=85):
    if len(title) <= limit:
        return title
    return title[:limit - 1].rstrip(" ,;:.-—") + "…"


def get_next_match_blurb():
    """Wenn ein Bayern-BL-Spiel in den nächsten 7 Tagen ist: Ticker-Text bauen."""
    data = fetch_json("https://api.openligadb.de/getmatchdata/bl1/2025") or fetch_json("https://api.openligadb.de/getmatchdata/bl1")
    if not data or not isinstance(data, list):
        return None
    now = datetime.now(timezone.utc)
    next_match = None
    for m in data:
        if m.get("matchIsFinished"):
            continue
        t1 = (m.get("team1") or {}).get("teamName", "").lower()
        t2 = (m.get("team2") or {}).get("teamName", "").lower()
        if "bayern" not in t1 and "bayern" not in t2:
            continue
        dt_str = m.get("matchDateTime") or m.get("matchDateTimeUTC")
        if not dt_str:
            continue
        try:
            match_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if match_dt.tzinfo is None:
                match_dt = match_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        delta = (match_dt - now).total_seconds()
        if delta < 0 or delta > 7 * 86400:
            continue
        if not next_match or match_dt < next_match[1]:
            next_match = (m, match_dt)
    if not next_match:
        return None
    m, match_dt = next_match
    is_home = "bayern" in (m.get("team1") or {}).get("teamName", "").lower()
    opp = (m.get("team2") if is_home else m.get("team1")) or {}
    opp_name = opp.get("shortName") or opp.get("teamName") or "TBD"
    delta = (match_dt - now).total_seconds()
    days = int(delta // 86400)
    if days == 0:
        when = "heute"
    elif days == 1:
        when = "morgen"
    else:
        when = f"in {days} Tagen"
    prefix = "Heim gegen" if is_home else "Auswärts bei"
    return f"{prefix} {opp_name} {when} — alle Augen auf den FCB"


def main():
    if not CONTENT_FILE.exists():
        print("Keine content.json — Skip.")
        return
    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        content = json.load(f)

    items = []

    blurb = get_next_match_blurb()
    if blurb:
        items.append(blurb)
        print(f"  + Spiel-Blurb: {blurb}")

    articles = content.get("articles", [])
    sorted_articles = sorted(articles, key=lambda a: a.get("date", ""), reverse=True)
    for a in sorted_articles[:5]:
        title = a.get("title")
        if not title:
            continue
        items.append(shorten(title))

    items.append("Mia san Mia · Workuvision — Bayern-Content ohne Filter")
    items.append("Folge @workuvision auf TikTok, Instagram & YouTube")

    if len(items) < 6:
        items.extend([
            "FC Bayern München · Saison 2025/26",
            "Workuvision · Taktik · Reaktionen · Transfer-News",
        ])

    content["ticker"] = items
    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    print(f"✓ Ticker mit {len(items)} Items aktualisiert.")


if __name__ == "__main__":
    main()
