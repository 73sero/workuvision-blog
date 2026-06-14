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
  const F = "-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,Helvetica,sans-serif";
  return `<!DOCTYPE html>
<html lang="de" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>Newsletter bestätigen</title>
</head>
<body style="margin:0;padding:0;background:#060A10;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#060A10;font-size:1px;line-height:1px;">Nur noch ein Klick und du bist dabei — bestätige deine Anmeldung zum Workuvision-Newsletter. Mia san Mia.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#060A10;">
<tr><td align="center" style="padding:0 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;margin:0 auto;">

  <tr><td style="height:5px;background:#DC052D;font-size:0;line-height:0;">&nbsp;</td></tr>

  <tr><td align="center" style="padding:34px 24px 6px;font-family:${F};">
    <span style="font-size:25px;font-weight:800;letter-spacing:5px;color:#F2F2F2;text-transform:uppercase;">WORKU<span style="color:#DC052D;">VISION</span></span>
    <div style="margin-top:9px;font-size:10px;font-weight:700;letter-spacing:3px;color:#7d8ea1;text-transform:uppercase;">FC Bayern · Taktik · Transfers</div>
  </td></tr>

  <tr><td style="padding:22px 16px 10px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#0B1622;border:1px solid #1b2a3c;border-radius:10px;">
      <tr><td style="padding:42px 38px;font-family:${F};">
        <div style="font-size:11px;font-weight:800;letter-spacing:3px;color:#DC052D;text-transform:uppercase;margin-bottom:16px;">Newsletter · Bestätigung</div>
        <h1 style="margin:0;font-size:32px;line-height:1.1;color:#F2F2F2;text-transform:uppercase;letter-spacing:1px;font-weight:800;">Nur noch<br>ein Klick</h1>
        <div style="width:52px;height:3px;background:#DC052D;margin:16px 0 24px;font-size:0;line-height:0;">&nbsp;</div>
        <p style="margin:0 0 30px;font-size:15px;line-height:1.75;color:#D6DDE6;">Schön, dass du dabei sein willst! Bestätige jetzt deine E-Mail-Adresse — dann bekommst du Bayern-Taktik, Transfer-Gerüchte und Spieltags-Takes direkt ins Postfach. Ehrlich, ohne Clickbait.</p>

        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;">
          <tr><td align="center" style="background:#DC052D;border-radius:5px;box-shadow:0 6px 18px rgba(220,5,45,0.35);">
            <a href="${url}" target="_blank" style="display:inline-block;padding:17px 40px;font-family:${F};font-size:14px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#FFFFFF;text-decoration:none;">Anmeldung bestätigen &rarr;</a>
          </td></tr>
        </table>

        <p style="margin:0 0 6px;font-size:12px;line-height:1.6;color:#7d8ea1;">Button geht nicht? Kopiere diesen Link in deinen Browser:</p>
        <p style="margin:0 0 26px;font-size:12px;line-height:1.5;word-break:break-all;"><a href="${url}" style="color:#9BAEC1;text-decoration:underline;">${url}</a></p>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #1b2a3c;padding-top:20px;">
          <p style="margin:0;font-size:12px;line-height:1.65;color:#7d8ea1;">Du hast dich nicht angemeldet? Dann ignoriere diese E-Mail einfach — ohne Bestätigung wird deine Adresse <strong style="color:#9BAEC1;">nicht</strong> gespeichert und du bekommst keine weiteren Mails.</p>
        </td></tr></table>
      </td></tr>
    </table>
  </td></tr>

  <tr><td align="center" style="padding:10px 24px 38px;font-family:${F};">
    <div style="font-size:13px;font-weight:800;letter-spacing:5px;color:#4f5d6e;text-transform:uppercase;margin-bottom:14px;">Mia san Mia</div>
    <div style="font-size:11px;line-height:1.8;color:#5d6b7c;">
      Workuvision · Abdel Worku · Hansaallee 139a · 60320 Frankfurt am Main<br>
      <a href="${SITE}/impressum.html" style="color:#9BAEC1;text-decoration:none;">Impressum</a> &nbsp;·&nbsp;
      <a href="${SITE}/datenschutz.html" style="color:#9BAEC1;text-decoration:none;">Datenschutz</a> &nbsp;·&nbsp;
      <a href="https://www.tiktok.com/@workuvision" style="color:#9BAEC1;text-decoration:none;">TikTok</a> &nbsp;·&nbsp;
      <a href="https://www.instagram.com/workuvision" style="color:#9BAEC1;text-decoration:none;">Instagram</a>
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>`;
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
