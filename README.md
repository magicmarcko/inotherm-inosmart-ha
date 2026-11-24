# Inotherm Inosmart (BT 640) – Home Assistant & BLE Integration

Lokale Steuerung von **Inotherm Haustüren mit Inosmart BT 640** über **Bluetooth Low Energy (BLE)** – wahlweise direkt in **Home Assistant** (über einen ESP32 Bluetooth Proxy in Türnähe) oder per **Standalone-CLI** unter Linux.

> **Kein Cloud-Gateway, kein Hardware-Eingriff an der Tür:** Die Kommunikation läuft vollständig lokal über das native BLE-Nordic-UART-Protokoll der Tür (Challenge-Response mit RC6-32/20/16 und SHA-1).

---

## Architektur

```text
Inotherm Inosmart Tür (BLE)  <──>  ESP32 Bluetooth Proxy (WLAN)  <──>  Home Assistant (custom_components/inosmart)
```

- **Home Assistant Custom Component:** [`custom_components/inosmart/`](custom_components/inosmart/)
- **ESP32 Bluetooth Proxy Konfiguration:** [`esphome/inosmart-bluetooth-proxy.yaml`](esphome/inosmart-bluetooth-proxy.yaml)
- **Standalone CLI (Linux/BlueZ):** [`inosmart_ble.py`](inosmart_ble.py)
- **Protokoll-Dokumentation:** [`PROTOCOL.md`](PROTOCOL.md)

### Warum der mitgelieferte ESPHome-Proxy-Patch nötig ist
Die Inosmart-Tür sendet unmittelbar nach dem BLE-Verbindungsaufbau ein unaufgefordertes SMP Security Request (`ESP_GAP_BLE_SEC_REQ_EVT`).
- **Linux BlueZ (`bleak`)** ignoriert dies bei unverschlüsselten GATT-Services einfach und kommuniziert direkt über Nordic UART (`6e400001-...`).
- **ESPHome Bluedroid** beantwortet dieses Request standardmäßig mit `true` (`esp_ble_gap_security_rsp`), wodurch die Tür das unerwartete SMP-Pairing mit `HCI 0x05` (`Authentication Failure`) / `auth fail reason=102` sofort abbricht.

Das lokale Override [`esphome/custom_components/bluetooth_connection/`](esphome/custom_components/bluetooth_connection/) setzt diese Antwort auf `false`, sodass die BLE-GATT-Verbindung über den ESP32 stabil aufgebaut wird (die eigentliche Verschlüsselung findet wie bei der offiziellen App in der Anwendungsschicht über RC6/SHA-1 statt).

---

## Voraussetzungen

1. **Inotherm Tür** mit Inosmart-Steuerung (z. B. BT 640).
2. **ESP32-Board** (z. B. ESP32-DevKit / `esp32dev`) in einer Steckdose im Flur nahe der Türinnenseite *(empfohlenes Signal besser als -82 dBm)*.
3. **Home Assistant** (mit aktiver `bluetooth`- / `esphome`-Integration).
4. Zum Öffnen wird **kein Admin-Passwort** benötigt – lediglich eine einmalige Kopplung an der Tür per **Taste 1 + Admin-Finger**.

---

## Schritt 1: ESP32 Bluetooth Proxy flashen

1. `esphome/secrets.yaml` aus der Vorlage anlegen:
   ```bash
   cp esphome/secrets.yaml.example esphome/secrets.yaml
   ```
   und dort dein **2,4-GHz-WLAN** (`wifi_ssid` und `wifi_password`) eintragen.
2. ESP32 per USB anschließen und mit ESPHome kompilieren & flashen (danach sind Updates auch kabellos per OTA möglich):
   ```bash
   esphome run esphome/inosmart-bluetooth-proxy.yaml
   ```
3. ESP32 in Türnähe an ein USB-Netzteil stecken.
4. In Home Assistant unter **Einstellungen → Geräte & Dienste** den automatisch entdeckten **Haustür Bluetooth Proxy** hinzufügen (*Konfigurieren → Absenden*).
   > **Tipp:** In den Optionen des ESPHome-Eintrags den Bluetooth-Scanmodus auf **`active`** stellen (empfohlen, damit der Gerätename `iN00...` und die Advertising-UUID zuverlässig empfangen werden).

---

## Schritt 2: Home-Assistant-Integration installieren

### Variante A: Über HACS (Benutzerdefiniertes Repository)
1. In Home Assistant **HACS → Integrationen → ⋮ → Benutzerdefinierte Repositories**.
2. URL dieses Repositories einfügen, Kategorie **Integration** wählen → **Hinzufügen** → **Herunterladen**.
3. Home Assistant neu starten.

### Variante B: Manuell kopieren
Den Ordner [`custom_components/inosmart`](custom_components/inosmart) in dein Home-Assistant-Verzeichnis `/config/custom_components/inosmart` kopieren und Home Assistant neu starten.

---

## Schritt 3: Tür einmalig koppeln & einbinden

1. **An der Tür (Steuereinheit im Türflügel):**
   - **Taste 1** ca. 2 Sekunden drücken und außen mit dem **Admin-Finger** bestätigen → **beide LEDs am Scanner leuchten weiß** (Kopplungsmodus aktiv).
2. **In Home Assistant:**
   - Unter **Einstellungen → Geräte & Dienste** erscheint die gefundene Inosmart-Tür automatisch (oder über **+ Integration hinzufügen → Inotherm Inosmart BLE**).
   - Die vorausgefüllte **15-stellige Client-IMEI** notieren (falls du sie später wiederverwenden möchtest), den Schalter **„Sofort über BLE koppeln“** aktiv lassen und auf **Absenden** klicken.
   - *(Hinweis: Ist eine IMEI bereits gekoppelt, den Schalter „Sofort über BLE koppeln“ beim Hinzufügen deaktivieren und die vorhandene IMEI eintragen.)*

---

## Verfügbare Entitäten & Services in Home Assistant

| Typ | Funktion | Hinweis |
| :--- | :--- | :--- |
| `lock` | Hauptschloss entriegeln / öffnen | Springt nach 3 Sekunden wieder auf *locked* |
| `button` | Tür öffnen, Tag-Entriegelung (Ready2Open), Relais 2, GW Relais 1/2 | Direktauslösung |
| `button` | Telefon koppeln (Taste 1) / Status & Info abrufen | Diagnose & Kopplung |
| `number` | Griff-/Tür-LED (`HLED`, 0–60 %), Zusatz-LED (`SLED`, 0–75 %) | Benötigt Admin-Passwort + Taste 3 (blaues Licht) |
| `sensor` | Firmware-Version, Öffnungszähler, Temperatur Steuerung, Client-IMEI, Letzter Status | Diagnose-Sensoren |

Services unter `inosmart.*`: `inosmart.unlock`, `inosmart.pair_device`, `inosmart.set_led`, `inosmart.set_free_entrance`, `inosmart.sync_clock`.

---

## Standalone-CLI (`inosmart_ble.py`)

Für direkte Tests am Linux-PC oder Raspberry Pi (über BlueZ/`bleak`):

```bash
python3 -m venv .venv
.venv/bin/pip install bleak

# Türen in Reichweite suchen
.venv/bin/python inosmart_ble.py scan

# Koppeln (vorher Taste 1 + Admin-Finger an der Tür drücken)
.venv/bin/python inosmart_ble.py --address XX:XX:XX:XX:XX:XX pair

# Tür entriegeln / Relais 2 schalten / Geräteinformationen abrufen
.venv/bin/python inosmart_ble.py unlock
.venv/bin/python inosmart_ble.py relay2
.venv/bin/python inosmart_ble.py info
```

Die verwendete Adresse und synthetische IMEI werden lokal unter `~/.config/inosmart/config.json` gespeichert.

---

## Bekannte Hinweise & Fehlerbehebung

- **Nur 1 BLE-Verbindung gleichzeitig:** Die Inosmart-Tür akzeptiert immer nur einen aktiven BLE-Client. Wenn die offizielle InoSmart-App auf dem Smartphone dauerhaft im Vordergrund verbunden ist, ist die Tür für Home Assistant in dieser Zeit nicht erreichbar.
- **Bluetooth bleibt nach Kopplung dauerhaft an:** Nur der Admin-/Einstellungsmodus (**Taste 3**, blaues Licht) schaltet sich nach wenigen Minuten ab; der verschlüsselte BLE-Standby zum Entriegeln bleibt dauerhaft aktiv.
- **Tests ausführen:**
  ```bash
  .venv/bin/python -m unittest discover -s tests
  ```

---

## Lizenz & Haftungsausschluss

Inoffizielles Community-Projekt auf Basis einer Protokoll-Analyse für Interoperabilität mit Home Assistant. Nutzung auf eigene Verantwortung.
