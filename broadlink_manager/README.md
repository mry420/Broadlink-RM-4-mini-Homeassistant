# Broadlink IR Manager

Home-Assistant-App (früher „Add-on“) zum Anlernen, Testen und Verwalten von IR-Codes
mit einem **Broadlink RM4 mini** – direkt in der HA-Seitenleiste.

- Beliebig viele Geräte (TV, LED-Strip, Beamer, Soundbar, Klimaanlage …) als Karten
- „Taste anlernen“ mit Countdown, danach Namen vergeben und speichern
- „Senden“-Button neben jeder Taste zum direkten Testen
- YAML-Export: fertige Skripte (`remote.send_command`) und `switch`-Entitäten
- **SmartIR-Assistent:** macht aus deinen Tasten echte HA-Geräte (Media Player, Licht)
  und installiert die Code-Datei per Knopfdruck
- **IR-Datenbank mit rund 8.900 Fernbedienungen** (TV, Beamer, Soundbar, LED, Klima,
  Ventilator …): Hersteller suchen, jede Taste testen, übernehmen – ohne Anlernen
- **SmartIR-Datenbank:** fertige Codes vieler Marken suchen, testen und übernehmen
- Läuft komplett lokal (kein Cloud-Dienst, kein CDN)

## Installation

1. **Einstellungen → Apps → App-Store** öffnen.
2. Oben rechts **⋮ → Repositories** und diese URL hinzufügen:
   `https://github.com/mry420/Broadlink-RM-4-mini-Homeassistant`
3. **Broadlink IR Manager** auswählen und installieren.
4. Im Tab **Konfiguration** IP- und MAC-Adresse eintragen, speichern, App starten.
5. **In Seitenleiste anzeigen** aktivieren – fertig.

## Konfiguration

| Option          | Beispiel            | Beschreibung |
|-----------------|---------------------|--------------|
| `broadlink_ip`  | `192.168.1.50`      | IP des RM4 mini. Am besten im Router fest vergeben. |
| `broadlink_mac` | `AA:BB:CC:DD:EE:FF` | MAC des RM4 mini. Fallback für die Verbindung und für den Switch-Export. |

IP und MAC findest du in der Broadlink-App (Gerät → Eigenschaften) oder im Router.

> **Wichtig:** In der Broadlink-App muss **„Gerät sperren“ deaktiviert** sein, sonst
> lehnt der RM4 mini lokale Verbindungen ab.

## Bedienung

1. **Neues Gerät anlegen** → Name (z. B. „Beamer“) und Symbol wählen.
2. In der Gerätekarte **Taste anlernen** klicken.
3. Innerhalb von 30 Sekunden die Taste der Original-Fernbedienung drücken
   (aus 5–10 cm Abstand direkt auf den RM4 mini zielen).
4. Nach „Signal empfangen“ optional **Testen**, dann Namen eingeben
   (z. B. „Power“, „Volume Up“, „Heller“) und **Speichern**.
5. Mit **Senden** neben jeder Taste prüfen, ob das Gerät reagiert.

Eine Taste mit gleichem Namen wird beim erneuten Anlernen überschrieben.

## YAML-Export

Über **YAML-Export** erzeugt die App Code für Home Assistant. Trage oben die
Entität deines RM4 mini ein (z. B. `remote.rm4_mini`, zu finden unter
Einstellungen → Geräte & Dienste → Broadlink). Voraussetzung ist, dass der
RM4 mini über die **Broadlink-Integration** in HA eingerichtet ist.

- **Skripte** – eins pro Taste, sendet den Code per `remote.send_command`
  mit `b64:`-Code. Einfügen in `scripts.yaml` (ohne die Zeile `script:`).
- **Schalter** – pro Gerät ein `switch` über die Broadlink-Plattform. Genutzt werden
  die Tasten **An/Aus** (bzw. On/Off); gibt es nur **Power**, wird sie für beide
  Richtungen verwendet. Einfügen in `configuration.yaml`, danach HA neu starten.

Die Skripte lassen sich danach in Dashboards (Button-Karte) oder Automationen verwenden.

## IR-Datenbank – Fernbedienungen ohne Anlernen

Unter **Neues Gerät anlegen → IR-Datenbank** findest du rund 8.900 Fernbedienungen
aus der freien [Flipper-IRDB](https://github.com/Lucaslhm/Flipper-IRDB) (CC0).

1. Kategorie wählen (z. B. **Beamer**) und Hersteller suchen (z. B. „Acer“).
2. Fernbedienung antippen – alle Tasten erscheinen mit einem **▶**-Testknopf.
3. Probieren, ob dein Gerät reagiert. Wenn nicht: nächsten Eintrag desselben Herstellers testen.
4. Gewünschte Tasten anhaken, Namen vergeben, **Übernehmen**.

Die App übersetzt die Codes der Datenbank (NEC, Samsung, Sony SIRC, RC5, RC6,
Panasonic/Kaseikyo, RCA, Pioneer und Rohdaten) automatisch in das Format des RM4 mini.
Signale mit 56 kHz Trägerfrequenz (sehr selten) kann der RM4 mini nicht senden – sie
werden ausgegraut. Das Verzeichnis ist in der App enthalten, die einzelnen Fernbedienungen
werden bei Bedarf von GitHub geladen (Internetzugang nötig) und zwischengespeichert.

> Die Fernbedienungs-Datenbank der Broadlink-App selbst ist ein geschlossener Cloud-Dienst
> des Herstellers ohne öffentliche Schnittstelle und kann deshalb nicht eingebunden werden.

## SmartIR – echte Geräte statt einzelner Tasten

[SmartIR](https://github.com/smartHomeHub/SmartIR) macht aus IR-Codes richtige
Home-Assistant-Entitäten, z. B. `media_player.tv` mit Ein/Aus, Lautstärke und
Quellenwahl oder `light.led_strip` mit Helligkeit. Diese App liefert die Codes dafür.

**Einmalig SmartIR installieren** – es steht nicht in der normalen HACS-Suche:

1. **HACS** öffnen → **⋮** oben rechts → **Benutzerdefinierte Repositories**
2. Repository: `https://github.com/smartHomeHub/SmartIR`, Typ: **Integration** → **Hinzufügen**
3. In HACS nach **SmartIR** suchen → **Herunterladen**
4. Home Assistant neu starten

### Gerät aus der Datenbank übernehmen

1. **Neues Gerät anlegen → Aus SmartIR-Datenbank**
2. Typ wählen (TV & Media / Licht), Hersteller suchen, Eintrag antippen.
3. Mit **An / Aus** testen – reagiert das Gerät, **Übernehmen**.
   Fehlt eine Taste, einfach in der Karte nachlernen.

### Eigenes Gerät zu SmartIR machen

1. In der Gerätekarte auf **SmartIR** tippen.
2. Typ wählen: **Media Player** (TV, Soundbar, Beamer, Receiver) oder **Licht** (LED).
3. Die App ordnet Tasten anhand ihrer Namen automatisch zu (z. B. „Power“ → Ein/Aus,
   „Lauter“ → Lautstärke +). Zuordnungen per Auswahlliste ändern, fehlende Funktionen
   direkt mit **Anlernen** ergänzen.
4. **In Home Assistant installieren** – die Code-Datei landet in
   `custom_components/smartir/codes/<typ>/<code>.json`.
5. Den angezeigten YAML-Block in `configuration.yaml` einfügen und HA neu starten.

Später neu angelernte Tasten werden automatisch in die installierte Datei übernommen.
Löscht ein SmartIR-Update über HACS die Datei, stellt die App sie innerhalb von
10 Minuten wieder her.

Eigene Codes bekommen Nummern ab **90001**, damit sie nie mit der
SmartIR-Datenbank (1000–7999) kollidieren.

> Die App braucht dafür Schreibzugriff auf den HA-Konfigurationsordner
> (`map: homeassistant_config`). Sie schreibt dort ausschließlich in
> `custom_components/smartir/codes/`.

Klimaanlagen werden noch nicht unterstützt: Ihre Fernbedienungen senden bei jedem
Tastendruck den kompletten Zustand, dafür kommt später ein eigener Assistent.

## Hinweise

- Der RM4 mini kann nur **Infrarot** (kein 433-MHz-Funk). LED-Strips, Beamer,
  TVs, Klimaanlagen usw. funktionieren, sofern ihre Fernbedienung IR nutzt.
- Daten liegen in `/data/devices.json` und sind Teil der HA-Backups.
- `armhf`, `armv7` und `i386` sind wie gewünscht eingetragen; Home Assistant
  unterstützt diese Architekturen offiziell nicht mehr.

## REST-API

| Methode | Pfad | Zweck |
|---------|------|-------|
| GET    | `/`                       | Dashboard |
| GET    | `/api/status`             | Verbindungsstatus RM4 mini |
| GET    | `/api/devices`            | Geräte und Tasten |
| POST   | `/api/devices`            | Gerät anlegen `{name, icon}` |
| DELETE | `/api/devices/{id}`       | Gerät löschen |
| DELETE | `/api/devices/{id}/commands/{cid}` | Taste löschen |
| POST   | `/api/learn`              | Lernmodus, wartet bis zu `timeout` s auf ein Signal |
| POST   | `/api/learn/cancel`       | Lernvorgang abbrechen |
| POST   | `/api/save-command`       | `{device_id, name, learn_id}` oder `{device_id, name, code}` speichern |
| POST   | `/api/send`               | `{device_id, command_id}` oder `{code}` senden |
| GET    | `/api/export/yaml?remote_entity=remote.xyz` | HA-YAML |
| GET/PUT | `/api/settings`          | Remote-Entität speichern |
| GET/PUT | `/api/devices/{id}/smartir` | SmartIR-Zuordnung (GET mit `?type=` liefert Vorschlag) |
| POST   | `/api/devices/{id}/smartir/install` | Code-Datei in SmartIR schreiben |
| GET    | `/api/smartir/catalog?type=media_player` | SmartIR-Datenbank (nur Broadlink) |
| POST   | `/api/smartir/test`       | Code aus der Datenbank testweise senden |
| POST   | `/api/smartir/import`     | Code aus der Datenbank als Gerät übernehmen |
| GET    | `/api/irdb/categories`    | Kategorien der IR-Datenbank |
| GET    | `/api/irdb/search?q=&category=` | Fernbedienungen suchen |
| GET    | `/api/irdb/remote?path=`  | Tasten einer Fernbedienung |
| POST   | `/api/irdb/send`          | Taste aus der Datenbank testweise senden |
| POST   | `/api/irdb/import`        | Fernbedienung (oder Auswahl) als Gerät übernehmen |

## Entwicklung

Das Verzeichnis der IR-Datenbank (`app/data/irdb_index.json.gz`) wird mit
`tools/irdb/build_index.py` erzeugt (Anleitung im Skript).

Das Frontend nutzt lokal kompiliertes Tailwind (`app/static/tailwind.css`).
Nach Änderungen an Klassen in `index.html`/`app.js` im Repo-Root neu bauen:

```bash
npx tailwindcss@3.4.19 -c tools/tailwind/tailwind.config.js -i tools/tailwind/input.css -o broadlink_manager/app/static/tailwind.css --minify
```
