"""TikTok Public-Profile-Scraper (ohne API-Key, ohne Login).

TikTok rendert auf der Profilseite ein <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">
mit allen User-Stats und einer Liste der letzten Videos. Wir laden die HTML-Seite,
extrahieren das JSON und schreiben Stats + 6 neueste Videos in content.json.

Hinweis: TikTok ändert ab und zu die Struktur. Wenn der Scraper kaputtgeht,
behalten wir die alten Werte (kein Reset auf 0).
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"
HANDLE = os.environ.get("TIKTOK_HANDLE", "workuvision")
URL = f"https://www.tiktok.com/@{HANDLE}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def load_content():
    if not CONTENT_FILE.exists():
        return {}
    with CONTENT_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_content(data):
    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_profile_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_data_blob(html):
    """Findet das JSON in <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">"""
    m = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        # Fallback: SIGI_STATE (alte Variante)
        m = re.search(r'<script[^>]*id="SIGI_STATE"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def find_user_info(blob):
    """Sucht im verschachtelten JSON nach den User-Stats."""
    if not isinstance(blob, dict):
        return None, None
    # Pfad 1: __DEFAULT_SCOPE__/webapp.user-detail
    scope = blob.get("__DEFAULT_SCOPE__") or {}
    detail = scope.get("webapp.user-detail") or {}
    user_info = detail.get("userInfo") or {}
    if user_info.get("user") and user_info.get("stats"):
        return user_info.get("user"), user_info.get("stats")

    # Fallback: Suche rekursiv
    found = {"user": None, "stats": None}

    def walk(obj):
        if isinstance(obj, dict):
            if "stats" in obj and isinstance(obj["stats"], dict) and "followerCount" in obj["stats"]:
                if not found["stats"]:
                    found["stats"] = obj["stats"]
                    found["user"] = obj.get("user") or obj.get("userInfo", {}).get("user")
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(blob)
    return found["user"], found["stats"]


def find_video_list(blob):
    """Sucht eine Liste der zuletzt geposteten Videos."""
    if not isinstance(blob, dict):
        return []
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            # itemList = Liste von Video-Objekten
            if "itemList" in obj and isinstance(obj["itemList"], list):
                for it in obj["itemList"]:
                    if isinstance(it, dict) and it.get("id") and it.get("desc") is not None:
                        found.append(it)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(blob)
    # Dedupe by id
    seen = set()
    unique = []
    for v in found:
        if v.get("id") not in seen:
            seen.add(v.get("id"))
            unique.append(v)
    return unique


def format_count(n):
    """1234 → '1.234', 23400 → '23.4K', 1500000 → '1.5M'"""
    if n is None:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n/1000:.0f}K"
    if n >= 1000:
        return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)


def format_views_de(n):
    """Deutsche Tausender-Trennung mit Punkt"""
    if n is None:
        return "0"
    return f"{int(n):,}".replace(",", ".")


def main():
    print(f"Lade TikTok-Profil: {URL}")
    try:
        html = fetch_profile_html(URL)
    except Exception as e:
        print(f"❌ Konnte Profil-Seite nicht laden: {e}")
        sys.exit(0)  # Soft-fail damit der Workflow weiterläuft

    blob = extract_data_blob(html)
    if not blob:
        print("⚠️  Konnte JSON-Blob nicht extrahieren — TikTok-Layout hat sich evtl. geändert. Skip.")
        sys.exit(0)

    user, stats = find_user_info(blob)
    videos = find_video_list(blob)

    if not stats:
        print("⚠️  Keine Stats gefunden — Skip.")
        sys.exit(0)

    print(f"✓ Gefunden: {stats.get('followerCount')} Follower, "
          f"{stats.get('heartCount')} Likes, {stats.get('videoCount')} Videos, "
          f"{len(videos)} Video-Items.")

    content = load_content()

    # Stats updaten
    content["tiktokStats"] = {
        "followers": int(stats.get("followerCount") or 0),
        "likes": format_count(stats.get("heartCount") or stats.get("heart") or 0),
        "videos": int(stats.get("videoCount") or 0),
    }

    # Videos updaten (max 6, neueste zuerst, nur wenn was gefunden)
    if videos:
        # createTime = Unix-Timestamp, neueste zuerst
        videos.sort(key=lambda v: v.get("createTime", 0), reverse=True)
        new_video_list = []
        for v in videos[:6]:
            video_id = v.get("id")
            cover = (v.get("video") or {}).get("cover") or (v.get("video") or {}).get("dynamicCover")
            stats_v = v.get("stats") or v.get("statsV2") or {}
            views = stats_v.get("playCount") or stats_v.get("play_count") or 0
            try:
                views = int(views)
            except (ValueError, TypeError):
                views = 0
            new_video_list.append({
                "thumbnail": cover or "img/logo.jpg",
                "title": (v.get("desc") or "Workuvision Video")[:120],
                "views": format_views_de(views),
                "url": f"https://www.tiktok.com/@{HANDLE}/video/{video_id}" if video_id else f"https://www.tiktok.com/@{HANDLE}",
            })
        content["tiktokVideos"] = new_video_list
        print(f"✓ {len(new_video_list)} Videos in content.json eingetragen.")

    from datetime import datetime, timezone
    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    save_content(content)
    print("✓ content.json gespeichert.")


if __name__ == "__main__":
    main()
