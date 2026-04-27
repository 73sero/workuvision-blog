# Workuvision-Blog · DSGVO-Update

## Was hat sich gegenüber dem Stand vor 3 Wochen geändert

### ✅ DSGVO-Sicherung (kritisch)
- **Google Fonts entfernt** — alle 9 Schriften (Oswald 400/600/700, Outfit 300/400/600/700, JetBrains Mono 500/700) sind jetzt lokal in `/fonts` und werden vom eigenen Server geladen. Keine Verbindung mehr zu `fonts.googleapis.com` oder `fonts.gstatic.com` → kein Abmahnrisiko mehr.
- **Newsletter-Form** mit Pflicht-Datenschutz-Checkbox erweitert. Das Netlify Forms Setup bleibt erhalten (mit Honeypot gegen Bots).
- **Vollständige Datenschutzerklärung** (14 Abschnitte) statt der dürren 6 Punkte. Deckt jetzt explizit ab: Netlify-Hosting, Newsletter via Netlify Forms, OpenLigaDB-API für Live-Daten, Decap CMS, Google Search Console, lokale Fonts, alle Betroffenenrechte mit DSGVO-Artikeln und Beschwerderecht beim HBDI.

### ✅ Content-Updates
- **Live-Ticker** auf den aktuellen Stand: PSG-Halbfinale, Mainz-Comeback (4:3), 35. Meistertitel, Goretzka-Milan, Gordon-Gerücht, Jackson-Rückkehr
- **Top-3-Artikel** aktualisiert (PSG-Vorschau, Goretzka/Gordon, Mainz-Wahnsinn)
- **JS-Fallback** für nächstes Spiel aktualisiert (PSG statt Real Madrid)

### ✅ Branding & SEO
- **og:image** (`img/og-image.jpg`, 1200×630) für WhatsApp/Facebook/Twitter-Vorschauen
- **og:url, twitter:image** ergänzt
- **YouTube-Link** in Footer-Liste, Footer-Icons (YT-Button) und Impressum
- **JSON-LD Schema** mit `sameAs` für alle Social-Profile (TikTok/IG/YouTube)
- **aria-labels** für Barrierefreiheit auf Modal-Schließen-Buttons und Social-Icons
- **Kontrast verbessert** — `--gray` von `#7B8EA1` auf `#9BAEC1` (jetzt WCAG-AA konform)

### ✅ Was bleibt unangetastet
- Decap CMS (`admin/`) inkl. Netlify Identity Login
- OpenLigaDB Live-Tabelle + Countdown bis Anpfiff
- Sitemap.xml + robots.txt + Google Search Console Verification
- Bestehender Markdown-Post in `posts/`
- Alle Original-Icons (favicon.ico, favicon-16x16.png, favicon-32x32.png, icon-192.png, icon-512.png, apple-touch-icon.png)

---

## Deploy

### Wenn du über GitHub deployst (was du tust)

1. Alle Dateien aus diesem ZIP **als Ersatz** in dein Repo committen:
   - Hochladen über GitHub-Web-UI: „Add file → Upload files" → alle Dateien reinziehen → commit
   - Oder via Terminal: `unzip` über bestehendes Repo, `git add . && git commit -m "DSGVO-Update" && git push`
2. Netlify deployt automatisch innerhalb 1–2 Minuten
3. **In Netlify Dashboard prüfen:**
   - Site Settings → **Build & deploy → Post processing → „Email obfuscation" muss OFF sein** (sonst friert der Preloader ein, hatten wir schon mal)
   - Account Settings → **Data Processing Addendum** akzeptieren (DSGVO-AVV mit Netlify)

### Test nach dem Deploy

1. `workuvision.de` im **Inkognito-Tab** öffnen
2. F12 (DevTools) → **Network**-Tab → Seite neu laden
3. Filter: „google" eingeben
4. **Es darf NICHTS auftauchen** außer evtl. `googletagmanager` (vom GSC-Verify-Tag) — wenn doch was zu `fonts.googleapis.com` o.ä. kommt: Cache leeren und nochmal probieren

---

## Optional: Phase 2 (später)

### Newsletter-Versand aktivieren
Aktuell sammeln Netlify Forms die E-Mails ein, verschicken aber nichts. Optionen:
- **Netlify-Notify**: Bei jeder Eintragung E-Mail an dich → manuell in Mailerlite/Brevo importieren
- **Brevo direkt**: Im JS `hS()` einen API-Call zu Brevo einbauen (statt Netlify Forms) — siehe altes README
- **Zapier/Make.com**: Netlify Form → Zap → Brevo Liste → automatischer Newsletter

### Posts dynamisch aus CMS laden
Aktuell sind die 6 Artikel auf der Startseite hardcoded. Dein CMS speichert neue Posts in `posts/*.md`. Damit die auf der Startseite erscheinen, bräuchtest du einen Build-Step (z.B. mit Eleventy oder per JS `fetch` einer `posts.json`). Das ist eigene Phase 3.

### Automatisierte News-Aggregation
RSS-Feeds (kicker, fcbayern.com, BFW) → GitHub Action → Claude API umschreibt → Markdown-Post → ins CMS. Kosten: ~1–2 €/Monat. Dazu im AUDIT.md mehr Details.

---

## Kontakt
Wenn was hakt — einfach mit Screenshot zurück zu Claude.
