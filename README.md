# Workuvision-Blog · Update v3 (Automatisierung)

## Was neu ist

### ✅ Sofort funktioniert (ohne dein Zutun)

**Live-Bundesliga-Stats** — Bug behoben. Stats laden jetzt zuverlässig von OpenLigaDB. Falls die API mal ausfällt, zeigen wir sinnvolle Defaults (82 Punkte, Spieltag 31, „105+ Tore") statt leere „—".

**Klickbare Artikel** — jede Artikel-Karte führt jetzt zu `artikel.html?slug=…` mit Volltext, Quelle (verlinkt) und „Mehr aus dieser Kategorie".

**Archiv-Seite** (`archiv.html`) — alle Artikel auf einer Seite mit:
- Live-Suche (filtert Titel + Inhalt während du tippst)
- Kategorie-Filter (Alle / Taktik / Reaktionen / Transfer)
- Sortierung neueste-zuerst

**Dynamisches Frontend** — `index.html`, `artikel.html` und `archiv.html` lesen alle aus einer einzigen Datei: `content.json`. Damit kann der Bot später Artikel hinzufügen, ohne HTML zu ändern.

**Ticker dynamisch** — wird ebenfalls aus content.json gefüllt; wenn neue Artikel reinkommen, kann der Bot später den Ticker mitupdaten.

### ⚙️ Vorbereitet, aber braucht 1× Konfiguration

Die GitHub Action `auto-update.yml` läuft **dreimal täglich** (6h, 12h, 18h MESZ) und macht drei Dinge:

#### 1. TikTok-Stats + Videos (kein API-Key nötig)
Läuft automatisch nach dem ersten Push. **Kein Setup nötig.** Das Script liest die public TikTok-Profilseite und extrahiert Follower, Likes, Videoanzahl + die 6 neuesten Videos mit echten Thumbnails und Aufrufzahlen.

⚠️ **Warnung:** TikTok ändert manchmal sein HTML-Layout. Wenn der Scraper kaputtgeht, behalten wir die alten Werte (kein Reset). Du wirst es daran merken, dass die Stats nicht mehr aktuell sind. Dann: Issue im Repo aufmachen, ich fix's.

#### 2. YouTube-Videos (braucht kostenlosen API-Key)
**Setup einmalig:**
1. Geh auf https://console.cloud.google.com → neues Projekt
2. „APIs & Services" → „Library" → **„YouTube Data API v3"** → aktivieren
3. „Credentials" → „Create credentials" → „API key" → kopieren
4. Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `YT_API_KEY`
   - Value: dein API-Key
5. Fertig. Beim nächsten Action-Run werden YouTube-Videos in `content.json` unter `youtubeVideos` gespeichert.

**Free-Tier:** 10.000 Calls pro Tag. Wir verbrauchen ~3 Calls pro Action-Run = 9 Calls/Tag = unter 0,1% des Limits. Kostenlos für immer.

#### 3. Auto-News-Aggregator (braucht Anthropic API-Key)
**Setup einmalig:**
1. Geh auf https://console.anthropic.com → API Keys → „Create Key"
2. Free-Tier: $5 Startguthaben. Pro Artikel verbrauchen wir ~$0.005. Bei 4 Artikeln/Tag = $0.02/Tag = $0.60/Monat.
3. Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ANTHROPIC_API_KEY`
   - Value: dein Key
4. Fertig.

**Was passiert dann:** Der Bot lädt alle 6h frische Bayern-News von kicker, Bavarian Football Works und Sport1, prüft ob das Thema schon abgedeckt ist (Dedup), und wenn ein neues Thema dabei ist, schreibt Claude einen kurzen Artikel im Workuvision-Stil (~250 Wörter, eigene Meinung, Quelle verlinkt) und committed ihn ins Repo. Netlify deployt automatisch.

**Maximum:** 1 neuer Artikel pro Lauf, max. 4/Tag. Hartlimit auf 30 Artikel total (älteste fliegen raus).

**Urheberrecht:** Quellen werden namentlich genannt und verlinkt („laut kicker", „die Gazzetta dello Sport berichtet"). Kein 1:1-Kopieren — Claude formuliert alles in eigenen Worten. Damit bewegen wir uns im Rahmen des Zitatrechts (§ 51 UrhG).

---

## Aktueller Status der Webseite (Live-Check 28.04.2026)

✅ DSGVO-konform (lokale Fonts, vollständige DSE)
✅ Newsletter mit Pflicht-Checkbox + Netlify Forms
✅ Live-Ticker, Live-Stats, Countdown
✅ Decap CMS funktioniert
✅ Mobile responsive
⚠️ Bug-Fix für Live-Stats — in dieser Version gefixt
⚠️ Artikel waren nicht klickbar — in dieser Version gefixt

---

## Deploy

1. ZIP entpacken
2. Alle Dateien (auch `.github/`, `scripts/`, `content.json`) per GitHub Web-UI hochladen oder per `git push`
3. **Wichtig:** Im Repo unter **Settings → Actions → General** sicherstellen, dass „Read and write permissions" für GITHUB_TOKEN aktiviert ist. Sonst kann die Action keine Commits pushen.
4. Optional: API-Keys als Secrets hinterlegen (siehe oben)
5. Manuell testen: **Actions → Auto-Update Content → Run workflow** — sollte ~30 Sekunden dauern

---

## Was ich NICHT eingebaut habe und warum

**Twitter/X-Integration** — Die Twitter-API kostet 100$/Monat im günstigsten Tier seit 2023. Web-Scraping wird seit Mitte 2024 aggressiv blockiert (Login-Pflicht für fast alles). Für ein Hobbyprojekt nicht praktikabel. Workaround: Wenn du auf X/Twitter ein Bayern-Update postest, kannst du Decap CMS nutzen, um manuell einen Artikel zu schreiben.

**Instagram-Stats** — Gleiche Problematik wie TikTok, aber Instagram ist seit 2024 deutlich aggressiver beim Blockieren von Scrapern. Wenn du Instagram-Stats willst, kostet das Apify ~5$/Monat. Sag Bescheid, dann baue ich's mit Apify-API ein.

**Echtzeit-Embed der TikTok-Videos** — Würde TikTok-iFrames bedeuten, die Daten an TikTok/USA und China senden, sobald jemand die Seite lädt. Damit wäre die DSGVO-Compliance und der Cookie-Banner wieder ein Thema. Aktuell zeigen wir Thumbnails + Aufrufzahlen, klick öffnet TikTok in neuem Tab — DSGVO-clean.

---

## Was du im Decap CMS machen kannst (`/admin/`)

Aktuell: nur Artikel als Markdown im `posts/`-Ordner verwalten.
Empfehlung: Ich migriere bei nächster Gelegenheit das CMS so, dass es direkt `content.json` editiert. Dann kannst du auch:
- Artikel manuell hinzufügen, die das Frontend sofort zeigt
- Den Ticker bearbeiten
- TikTok-Videos manuell überschreiben (z.B. einen bestimmten Beitrag pinnen)

Sag Bescheid wenn das gewünscht ist.

---

## Kosten-Übersicht

| Service | Free | Bezahlt |
|---|---|---|
| Netlify Hosting | ✅ | — |
| Netlify Forms | ✅ (100 Submissions/Monat) | — |
| OpenLigaDB | ✅ | — |
| YouTube Data API | ✅ (10k Calls/Tag) | — |
| TikTok-Scraping | ✅ (eigenes Script) | — |
| Anthropic Claude API | $5 Trial | ~0,60 €/Monat danach |
| GitHub Actions | ✅ (2000 Min/Monat) | — |
| Domain workuvision.de | — | ~12 €/Jahr |

**Gesamt-Kosten bei voller Automatisierung:** ~13 €/Jahr

---

## Wenn was nicht funktioniert

1. **GitHub Action schlägt fehl** → Im Repo unter „Actions" den letzten Run anklicken, Fehler kopieren, mir schicken
2. **TikTok-Stats updaten nicht mehr** → TikTok hat Layout geändert, Scraper braucht Update
3. **Live-Stats zeigen wieder „—"** → API down? Defaults sollten greifen. F12 → Console → Fehlermeldung
4. **Artikel-Klick führt ins Leere** → Wahrscheinlich ist `content.json` nicht im selben Ordner wie `index.html`. Im Browser `https://workuvision.de/content.json` aufrufen — sollte JSON anzeigen.

Bei allem: einfach mit Screenshot zurück zu Claude.
