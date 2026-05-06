"""Lineup-Update wurde DEAKTIVIERT.

Die Aufstellungs-Anzeige im Frontend ist abgeschaltet, weil die Datenquellen
(fcbayern.com, kicker.de) unzuverlässig waren — falsche Formationen, fehlende
Gegner-Aufstellungen, leere Pitches nach dem Spiel.

Stattdessen erwähnen Vorbericht-Artikel die voraussichtliche Aufstellung
im Fließtext, wenn die Information vorliegt.

Dieses Skript bleibt im Workflow als No-Op damit der Pipeline-Step nicht
fehlschlägt.
"""

def main():
    print("ℹ️  Lineup-Update deaktiviert — Frontend zeigt keine Aufstellungen mehr.")
    print("   Vorberichte erwähnen die Aufstellung im Fließtext wenn relevant.")

if __name__ == "__main__":
    main()
