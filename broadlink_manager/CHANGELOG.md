# Changelog

## 2.2.0

- Kino-Modus-Assistent (Szenen → „🎬 Kino-Modus“): baut die Szene aus den vorhandenen
  Geräten – Beamer einschalten (20 s warten), PlayStation auf alle Matrix-Ausgänge,
  Ton auf 5.1. Vor dem Speichern frei anpassbar

## 2.1.0

- Eingänge benennen: unter „Gerät bearbeiten“ einmal eintragen, was an HDMI 1–4 hängt –
  die Tasten aller Ausgänge werden umbenannt („Out A/B: HDMI 3“ → „Out A/B: PlayStation“)
- Szenen-Vorschläge: pro Eingang eine Szene, die alle Ausgänge auf diesen Eingang schaltet

## 2.0.0

- **Neue App-Oberfläche** mit Bereichen *Geräte*, *Szenen* und *Einstellungen*
- **Fernbedienung in der App:** Tipp auf ein Gerät öffnet eine echte Fernbedienung
  (Power, Steuerkreuz, Lautstärke-/Kanalwippe, Ziffern, Farben, Gruppen wie „Out A“)
- **Statusanzeige:** Die App merkt sich, ob ein Gerät/Ausgang an ist und welcher
  Eingang aktiv ist (z. B. „Out A: PlayStation“) – auf der Gerätekarte, in der
  Fernbedienung und in Home Assistant (eigene Status-Helfer, auch bei Bedienung aus HA).
  Ein Tipp auf den Status korrigiert ihn, falls das Gerät von Hand bedient wurde
- **Szenen:** mehrere Tasten über Geräte hinweg mit Wiederholung und Pausen,
  z. B. „Kino-Modus“ – in der App und als Skript/Dashboard-Ansicht in Home Assistant
- **Bearbeiten:** Gerät umbenennen und Symbol ändern, Tasten umbenennen, Farbe setzen,
  neu anlernen, löschen
- **Sicherung:** alle Geräte, Tasten und Szenen als Datei herunterladen und wiederherstellen
- Einstellungen-Seite mit Home-Assistant-Status, RM4-mini-Infos, YAML-Export

## 1.5.0

- Serien-Anlernen: beim Anlegen eines Geräts eine Tastenliste angeben (oder eine
  Vorlage wählen) – die App führt Taste für Taste durch, Name ist schon ausgefüllt
- Vorlagen: HDMI-Matrix 4×2, HDMI-Switch 4×1, TV, Soundbar, Beamer, LED-Strip (24 Tasten)
- Tasten im Format „Gruppe: Taste“ (z. B. „Out A: HDMI 1“) bekommen in der
  Home-Assistant-Fernbedienung eigene Bereiche
- Passende Symbole für Audio-Tasten (A↔B, ARC, 2CH, 5.1CH …)
- Behoben: Überspringen während des Wartens auf ein Signal brach das nächste Anlernen ab

## 1.4.0

- LED-Finder: testet die 42 gängigen LED-Fernbedienungs-Familien nacheinander
  („Leuchtet es rot? … grün?“) und übernimmt die passende automatisch
- Farben prüfen: die App sendet jede Farbtaste, du tippst die echte Farbe an –
  Namen werden korrigiert, Tasten ohne Reaktion entfernt
- Farbtasten zeigen ihre Farbe in der App und als echte Farbpalette in der
  Home-Assistant-Fernbedienung, Effekte (Fade, Strobe …) eigener Bereich
- Farbnamen aus der Datenbank werden übersetzt („Red“ → „Rot“, „Light Blue“ → „Hellblau“)
- Home-Assistant-Abgleich robuster: Skripte werden auch angelegt, wenn das Dashboard
  scheitert; klare Fehlermeldung, falls HA die Skripte nicht lädt

## 1.3.0

- Geräte landen automatisch in Home Assistant: ein Skript pro Taste (über die
  HA-Konfigurations-API, ohne YAML und ohne Neustart)
- Neues Dashboard „Fernbedienungen“ mit einer Universal-Fernbedienung pro Gerät
  (Power, Steuerkreuz, Lautstärke-/Kanalwippe, Ziffern, Quellen, weitere Tasten)
- Änderungen und gelöschte Geräte werden automatisch abgeglichen
- Broadlink-Fernbedienung in HA wird automatisch erkannt
- Neuer Bereich „Home Assistant“ in der App mit Status und Einstellungen

## 1.2.0

- IR-Datenbank: rund 8.900 Fernbedienungen (Flipper-IRDB) durchsuchen, jede Taste
  direkt testen und als Gerät übernehmen – z. B. Beamer, LED-Strips, Klimaanlagen
- Eigene Umsetzung der IR-Protokolle NEC, NECext, NEC42, Samsung32, SIRC, RC5, RC5X,
  RC6, Kaseikyo, RCA und Pioneer in das Broadlink-Format
- Importierte Tasten bekommen deutsche Namen und werden im SmartIR-Assistenten
  automatisch erkannt

## 1.1.0

- SmartIR-Assistent: Tasten den SmartIR-Funktionen zuordnen (Media Player, Licht),
  fehlende Funktionen direkt anlernen, Code-Datei per Knopfdruck in SmartIR installieren
- SmartIR-Datenbank: Codes vieler Marken suchen, testen und als Gerät übernehmen
- Installierte SmartIR-Dateien werden nach Änderungen und SmartIR-Updates automatisch aktualisiert
- Remote-Entität wird gespeichert
- Tasten mit mehreren Codes (aus der Datenbank) werden unterstützt
- Lange Tastenlisten werden eingeklappt
- Neues Symbol: LED-Strip

## 1.0.0

- Erste Version: Geräte anlegen, Tasten anlernen, testen, YAML-Export
