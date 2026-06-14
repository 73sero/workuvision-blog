// Netlify Function: Newsletter-Anmeldung, Schritt 1 von 2 (Double-Opt-In).
//
// Nimmt die E-Mail vom Formular entgegen, erzeugt ein signiertes
// Bestätigungs-Token und verschickt über Brevo (transaktional) eine
// Bestätigungsmail mit Aktivierungslink auf die confirm-Funktion.
// Erst nach Klick wird die Adresse der Liste hinzugefügt (siehe confirm.js).
//
// Wir nutzen bewusst den transaktionalen Versand statt Brevos
// doubleOptinConfirmation-Endpoint, weil letzterer in diesem Konto
// "ok" meldet, ohne tatsächlich zu versenden.
//
// Benötigte Environment-Variablen (Netlify → Environment variables):
//   BREVO_API_KEY       – API-Schlüssel aus Brevo
//   NEWSLETTER_SECRET   – Zufalls-Secret zum Signieren der Tokens
// Optional:
//   NEWSLETTER_SENDER_EMAIL / NEWSLETTER_SENDER_NAME – Absender (Default: Outlook/Workuvision)

const crypto = require("crypto");

const SITE = "https://workuvision.de";
const TOKEN_TTL_SECONDS = 60 * 60 * 24 * 3; // 3 Tage gültig

exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return resp(405, { error: "Method not allowed" });
  }

  let data;
  try {
    data = JSON.parse(event.body || "{}");
  } catch {
    return resp(400, { error: "Ungültige Anfrage." });
  }

  // Honeypot: von Bots ausgefülltes Feld → wir tun so, als sei alles ok.
  if (data.company) {
    return resp(200, { ok: true });
  }

  const email = String(data.email || "").trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return resp(400, { error: "Bitte eine gültige E-Mail-Adresse angeben." });
  }

  const apiKey = process.env.BREVO_API_KEY;
  const secret = process.env.NEWSLETTER_SECRET;
  if (!apiKey || !secret) {
    console.error("Konfiguration unvollständig (BREVO_API_KEY/NEWSLETTER_SECRET fehlen).");
    return resp(500, { error: "Newsletter ist noch nicht konfiguriert." });
  }

  const senderEmail = process.env.NEWSLETTER_SENDER_EMAIL || "serdar.saglam@outlook.de";
  const senderName = process.env.NEWSLETTER_SENDER_NAME || "Workuvision";

  const token = makeToken(email, secret);
  const confirmUrl = `${SITE}/.netlify/functions/confirm?token=${encodeURIComponent(token)}`;

  try {
    const r = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: {
        "api-key": apiKey,
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        sender: { name: senderName, email: senderEmail },
        to: [{ email }],
        subject: "Bitte bestätige deine Newsletter-Anmeldung ⚽",
        htmlContent: confirmEmailHtml(confirmUrl),
        tags: ["newsletter-doi"],
      }),
    });

    if (r.status === 201 || r.status === 200) {
      return resp(200, { ok: true });
    }
    const body = await r.json().catch(() => ({}));
    console.error("Brevo-Versand fehlgeschlagen:", r.status, body);
    return resp(502, { error: "Bestätigungsmail konnte nicht versendet werden." });
  } catch (e) {
    console.error("Brevo-Request fehlgeschlagen:", e);
    return resp(502, { error: "Dienst aktuell nicht erreichbar." });
  }
};

function makeToken(email, secret) {
  const payload = { e: email, x: Math.floor(Date.now() / 1000) + TOKEN_TTL_SECONDS };
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const sig = crypto.createHmac("sha256", secret).update(body).digest("base64url");
  return `${body}.${sig}`;
}

function confirmEmailHtml(url) {
  return `<!DOCTYPE html><html lang="de"><body style="margin:0;padding:0;background:#060A10;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#060A10;padding:32px 16px;"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
<tr><td style="padding:24px 0;text-align:center;font-family:Arial,Helvetica,sans-serif;"><span style="font-size:22px;font-weight:bold;letter-spacing:4px;color:#F2F2F2;text-transform:uppercase;">WORKU<span style="color:#DC052D;">VISION</span></span></td></tr>
<tr><td style="background:#0B1622;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:36px 32px;font-family:Arial,Helvetica,sans-serif;color:#F2F2F2;">
<h1 style="margin:0 0 16px;font-size:24px;line-height:1.2;text-transform:uppercase;letter-spacing:1px;">Fast geschafft!</h1>
<p style="margin:0 0 24px;font-size:15px;line-height:1.7;color:#D6DDE6;">Danke f&uuml;r deine Anmeldung zum Workuvision-Newsletter. Best&auml;tige jetzt deine E-Mail-Adresse, um Bayern-Taktik, Transfer-News und Spieltags-Takes direkt ins Postfach zu bekommen.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto 24px;"><tr><td style="background:#DC052D;border-radius:4px;"><a href="${url}" style="display:inline-block;padding:14px 32px;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;color:#FFFFFF;text-decoration:none;">Anmeldung best&auml;tigen</a></td></tr></table>
<p style="margin:0 0 8px;font-size:12px;line-height:1.6;color:#9BAEC1;">Falls der Button nicht funktioniert, kopiere diesen Link in deinen Browser:</p>
<p style="margin:0;font-size:11px;line-height:1.5;color:#9BAEC1;word-break:break-all;"><a href="${url}" style="color:#9BAEC1;">${url}</a></p>
<p style="margin:20px 0 0;font-size:12px;line-height:1.6;color:#9BAEC1;">Du hast dich nicht angemeldet? Dann ignoriere diese E-Mail einfach &mdash; ohne Best&auml;tigung wird deine Adresse nicht in den Verteiler aufgenommen.</p>
</td></tr>
<tr><td style="padding:20px 8px;text-align:center;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.6;color:#9BAEC1;">Workuvision &middot; Abdel Worku &middot; Hansaallee 139a &middot; 60320 Frankfurt am Main<br><a href="${SITE}/impressum.html" style="color:#9BAEC1;">Impressum</a> &middot; <a href="${SITE}/datenschutz.html" style="color:#9BAEC1;">Datenschutz</a></td></tr>
</table></td></tr></table></body></html>`;
}

function resp(statusCode, obj) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": SITE,
    },
    body: JSON.stringify(obj),
  };
}
