"""YouTube Data API v3 Integration.

Lädt die letzten 6 Videos vom Workuvision-Channel und schreibt sie in
content.json unter "youtubeVideos". Das Frontend kann diese dann anzeigen.

Voraussetzung:
  - GitHub Secret YT_API_KEY (kostenlos in Google Cloud Console erstellen)
  - YT_HANDLE (z.B. "workuvision") als Env-Var oder Secret

Wenn der Secret nicht gesetzt ist, läuft der Workflow drumherum weiter.
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"

API_KEY = os.environ.get("YT_API_KEY", "").strip()
HANDLE = os.environ.get("YT_HANDLE", "workuvision").lstrip("@")

if not API_KEY:
    print("⚠️  YT_API_KEY nicht gesetzt — Skip YouTube-Update.")
    sys.exit(0)


def yt_get(endpoint, params):
    params["key"] = API_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def get_channel_id_from_handle(handle):
    """YouTube hat seit 2022 @-Handles. Resolve zu echter Channel-ID."""
    res = yt_get("channels", {"forHandle": "@" + handle, "part": "id"})
    items = res.get("items") or []
    if items:
        return items[0]["id"]
    # Fallback: Search-API
    res = yt_get("search", {"q": handle, "type": "channel", "part": "id", "maxResults": 1})
    items = res.get("items") or []
    if items:
        return items[0]["id"]["channelId"]
    return None


def get_recent_videos(channel_id, max_results=6):
    res = yt_get("search", {
        "channelId": channel_id,
        "type": "video",
        "part": "snippet",
        "order": "date",
        "maxResults": max_results,
    })
    videos = []
    for item in res.get("items", []):
        sn = item["snippet"]
        vid = item["id"]["videoId"]
        thumb = (sn.get("thumbnails") or {}).get("high", {}).get("url") or \
                (sn.get("thumbnails") or {}).get("default", {}).get("url") or ""
        videos.append({
            "id": vid,
            "title": sn.get("title", ""),
            "thumbnail": thumb,
            "publishedAt": sn.get("publishedAt", ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return videos


def main():
    print(f"Lade YouTube-Channel: @{HANDLE}")
    try:
        channel_id = get_channel_id_from_handle(HANDLE)
    except Exception as e:
        print(f"❌ Channel-Lookup fehlgeschlagen: {e}")
        sys.exit(0)

    if not channel_id:
        print(f"⚠️  Channel @{HANDLE} nicht gefunden.")
        sys.exit(0)

    print(f"✓ Channel-ID: {channel_id}")

    try:
        videos = get_recent_videos(channel_id, 6)
    except Exception as e:
        print(f"❌ Video-Liste konnte nicht geladen werden: {e}")
        sys.exit(0)

    print(f"✓ {len(videos)} Videos geladen.")

    if not CONTENT_FILE.exists():
        content = {}
    else:
        with CONTENT_FILE.open("r", encoding="utf-8") as f:
            content = json.load(f)

    content["youtubeVideos"] = videos
    content["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with CONTENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    print("✓ content.json mit YouTube-Videos aktualisiert.")


if __name__ == "__main__":
    main()
