#!/usr/bin/env python3
"""Repair-Script: geht durch alle Artikel mit Stock-Bild und versucht
ein passendes Subject-basiertes Bild zu finden (Bayern-Pressefoto / Wikipedia).

Wird im Auto-Update-Workflow nach aggregate_news.py ausgeführt, sodass
auch alte Artikel ohne sourceUrl nachträglich passende Bilder bekommen.

Idempotent: Artikel die schon ein img/articles/-Bild haben, werden übersprungen.
Keine API-Keys nötig — nutzt öffentliche Bayern-CDN- und Wikipedia-Endpunkte.
"""
import json
import sys
from pathlib import Path

# Image-Helper aus aggregate_news.py importieren
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    print("=== Repair Article Images ===")

    # Wir importieren erst hier, damit der Sys-Exit in aggregate_news bei
    # fehlendem ANTHROPIC_API_KEY uns nicht verhindert, weil wir den nicht
    # brauchen. Setze einen Dummy falls fehlt.
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-not-needed-for-repair")

    import aggregate_news as ag

    content_path = ROOT / "content.json"
    if not content_path.exists():
        print("Keine content.json gefunden.")
        sys.exit(0)

    with open(content_path) as f:
        content = json.load(f)

    articles = content.get("articles", [])
    print(f"✓ {len(articles)} Artikel geladen.")

    fixed_count = 0
    skipped_count = 0
    no_subjects_count = 0
    no_image_count = 0

    for article in articles:
        thumb = article.get("thumbnail", "")
        # Skip Artikel die schon ein lokales Article-Bild haben
        if thumb.startswith("img/articles/"):
            skipped_count += 1
            continue

        title = article.get("title", "")
        body = article.get("body", "")
        slug = article.get("slug", "")

        print(f"\n→ {title[:70]}")
        print(f"   aktuell: {thumb}")

        # 1) Wenn sourceUrl vorhanden, versuche og:image zuerst
        source_url = article.get("sourceUrl", "")
        og_url = None
        if source_url:
            og_url = ag.fetch_og_image_from_url(source_url)
            if og_url:
                print(f"   og:image gefunden: {og_url[:60]}")
                local = ag.download_and_optimize_image(og_url, slug, ag.ARTICLE_IMG_DIR)
                if local:
                    article["thumbnail"] = local
                    fixed_count += 1
                    print(f"   ✅ neu (og): {local}")
                    continue

        # 2) Subject-basiert (Bayern-CDN / Wikipedia)
        subjects = ag.extract_subjects(title, body)
        if subjects:
            print(f"   Subjekte: {', '.join(subjects[:5])}")
        else:
            print(f"   keine Subjekte gefunden")
            no_subjects_count += 1
            continue

        subject_url = ag.find_subject_image(title, body)
        if not subject_url:
            print(f"   kein Bild für Subjekte gefunden")
            no_image_count += 1
            continue

        local_thumb = ag.download_and_optimize_image(
            subject_url, slug, ag.ARTICLE_IMG_DIR
        )
        if local_thumb:
            article["thumbnail"] = local_thumb
            fixed_count += 1
            print(f"   ✅ neu (subject): {local_thumb}")
        else:
            no_image_count += 1
            print(f"   ⚠️  Download fehlgeschlagen")

    print(f"\n=== Zusammenfassung ===")
    print(f"  Geprüft:                       {len(articles)}")
    print(f"  Übersprungen (haben schon Bild): {skipped_count}")
    print(f"  Gefixt:                        {fixed_count}")
    print(f"  Keine Subjekte erkannt:        {no_subjects_count}")
    print(f"  Bild-Suche erfolglos:          {no_image_count}")

    if fixed_count > 0:
        with open(content_path, "w") as f:
            json.dump(content, f, indent=2, ensure_ascii=False)
        print(f"  ✓ content.json aktualisiert.")


if __name__ == "__main__":
    main()
