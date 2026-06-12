// Netlify Function: Newsletter-Anmeldung mit Double-Opt-In über Brevo.
//
// Das Formular auf der Website schickt die E-Mail hierher; diese Funktion ruft
// die Brevo-API auf, die eine Bestätigungsmail (DOI) verschickt. Erst nach Klick
// auf den Link in dieser Mail wird die Adresse der Liste hinzugefügt.
//
// Benötigte Environment-Variablen (Netlify → Site settings → Environment variables):
//   BREVO_API_KEY            – API-Schlüssel aus Brevo (SMTP & API → API Keys)
//   BREVO_LIST_ID            – ID der Kontaktliste in Brevo (Zahl)
//   BREVO_DOI_TEMPLATE_ID    – ID der Double-Opt-In-Vorlage in Brevo (Zahl)
// Optional:
//   DOI_REDIRECT_URL         – Zielseite nach Klick (Default: /newsletter-bestaetigt.html)

const SITE = "https://workuvision.de";

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
  const listId = parseInt(process.env.BREVO_LIST_ID, 10);
  const templateId = parseInt(process.env.BREVO_DOI_TEMPLATE_ID, 10);
  const redirectionUrl =
    process.env.DOI_REDIRECT_URL || `${SITE}/newsletter-bestaetigt.html`;

  if (!apiKey || !listId || !templateId) {
    console.error("Brevo-Konfiguration unvollständig (Env-Variablen fehlen).");
    return resp(500, { error: "Newsletter ist noch nicht konfiguriert." });
  }

  try {
    const r = await fetch(
      "https://api.brevo.com/v3/contacts/doubleOptinConfirmation",
      {
        method: "POST",
        headers: {
          "api-key": apiKey,
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify({
          email,
          includeListIds: [listId],
          templateId,
          redirectionUrl,
        }),
      }
    );

    // 201/204 = DOI-Mail verschickt. Brevo gibt bei bereits angemeldeten
    // Kontakten teils 400 zurück — das behandeln wir bewusst als Erfolg,
    // um keine Information über vorhandene Adressen preiszugeben.
    if (r.status === 201 || r.status === 204 || r.status === 200) {
      return resp(200, { ok: true });
    }

    const body = await r.json().catch(() => ({}));
    if (
      body &&
      (body.code === "duplicate_parameter" ||
        (body.message && /already|exist/i.test(body.message)))
    ) {
      return resp(200, { ok: true });
    }

    console.error("Brevo DOI-Fehler:", r.status, body);
    return resp(502, { error: "Anmeldung konnte nicht verarbeitet werden." });
  } catch (e) {
    console.error("Brevo-Request fehlgeschlagen:", e);
    return resp(502, { error: "Dienst aktuell nicht erreichbar." });
  }
};

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
