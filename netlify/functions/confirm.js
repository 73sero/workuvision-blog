// Netlify Function: Newsletter-Anmeldung, Schritt 2 von 2 (Double-Opt-In).
//
// Wird über den Link in der Bestätigungsmail aufgerufen (GET ?token=…).
// Prüft die Signatur und Gültigkeit des Tokens und fügt die Adresse erst
// dann der Brevo-Kontaktliste hinzu. Danach Weiterleitung auf die
// Bestätigungsseite. So ist die Einwilligung nachweisbar (DSGVO / § 7 UWG).
//
// Benötigte Environment-Variablen:
//   BREVO_API_KEY      – API-Schlüssel aus Brevo
//   BREVO_LIST_ID      – ID der Kontaktliste (Zahl)
//   NEWSLETTER_SECRET  – dasselbe Secret wie in subscribe.js

const crypto = require("crypto");

const SITE = "https://workuvision.de";
const SUCCESS_URL = `${SITE}/newsletter-bestaetigt.html`;

exports.handler = async (event) => {
  const token = (event.queryStringParameters && event.queryStringParameters.token) || "";
  const secret = process.env.NEWSLETTER_SECRET;
  const apiKey = process.env.BREVO_API_KEY;
  const listId = parseInt(process.env.BREVO_LIST_ID, 10);

  if (!secret || !apiKey || !listId) {
    console.error("Konfiguration unvollständig (Env-Variablen fehlen).");
    return page(500, "Newsletter ist noch nicht vollständig konfiguriert.");
  }

  const email = verifyToken(token, secret);
  if (!email) {
    return page(
      400,
      "Dieser Bestätigungslink ist ungültig oder abgelaufen. Bitte melde dich erneut an."
    );
  }

  try {
    const r = await fetch("https://api.brevo.com/v3/contacts", {
      method: "POST",
      headers: {
        "api-key": apiKey,
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        email,
        listIds: [listId],
        updateEnabled: true, // bereits vorhandene Kontakte zur Liste hinzufügen
        attributes: { DOI_CONFIRMED_AT: new Date().toISOString() },
      }),
    });

    // 201 = neu angelegt, 204 = aktualisiert. Beides = bestätigt.
    if (r.status === 201 || r.status === 204) {
      return redirect(SUCCESS_URL);
    }
    const body = await r.json().catch(() => ({}));
    // Schon in der Liste / schon Kontakt → ebenfalls als Erfolg behandeln.
    if (body && body.code === "duplicate_parameter") {
      return redirect(SUCCESS_URL);
    }
    console.error("Brevo-Kontakt anlegen fehlgeschlagen:", r.status, body);
    return page(502, "Die Bestätigung konnte nicht gespeichert werden. Bitte versuche es später erneut.");
  } catch (e) {
    console.error("Brevo-Request fehlgeschlagen:", e);
    return page(502, "Dienst aktuell nicht erreichbar. Bitte versuche es später erneut.");
  }
};

function verifyToken(token, secret) {
  if (!token || token.indexOf(".") === -1) return null;
  const [body, sig] = token.split(".");
  if (!body || !sig) return null;

  const expected = crypto.createHmac("sha256", secret).update(body).digest("base64url");
  // zeitkonstanter Vergleich
  const a = Buffer.from(sig);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;

  let payload;
  try {
    payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (!payload || !payload.e || !payload.x) return null;
  if (Math.floor(Date.now() / 1000) > payload.x) return null; // abgelaufen
  return String(payload.e).toLowerCase();
}

function redirect(location) {
  return { statusCode: 302, headers: { Location: location }, body: "" };
}

function page(statusCode, message) {
  const html = `<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="robots" content="noindex"><title>Newsletter | Workuvision</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:2rem;background:#060A10;color:#F2F2F2;font-family:Arial,Helvetica,sans-serif;line-height:1.7}.c{max-width:520px}h1{font-size:1.6rem;text-transform:uppercase;letter-spacing:1px;margin:0 0 1rem}p{color:#D6DDE6;margin:0 0 1.5rem}a{display:inline-block;padding:.8rem 1.6rem;background:#DC052D;color:#fff;text-decoration:none;font-size:.8rem;font-weight:bold;letter-spacing:2px;text-transform:uppercase;border-radius:3px}</style>
</head><body><div class="c"><h1>Newsletter</h1><p>${message}</p><a href="${SITE}/">Zur Startseite</a></div></body></html>`;
  return { statusCode, headers: { "Content-Type": "text/html; charset=utf-8" }, body: html };
}
