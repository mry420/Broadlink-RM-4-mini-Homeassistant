# Changelog

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
