# Öffentliches-Gut-Spiel

Ein webbasiertes Multiplayer-Experiment auf Basis des klassischen **Public-Goods-Game** aus der Spieltheorie – entwickelt für den Einsatz in Seminaren und Vorlesungen.

---

## Worum geht es?

Das Öffentliche-Gut-Spiel ist ein Standardexperiment der Verhaltensökonomik. Jede Runde erhält jeder Spieler ein festes Startguthaben und entscheidet, wie viel davon er in einen gemeinsamen Topf einzahlt. Der Topf wird mit einem einstellbaren Multiplikator vervielfacht und gleichmäßig auf alle Gruppenmitglieder verteilt – unabhängig davon, ob sie selbst eingezahlt haben.

**Kernspannung:** Kooperieren lohnt sich für die Gruppe, Trittbrettfahren (0 einzahlen) lohnt sich individuell – zumindest kurzfristig.

### Spielablauf

```
Spielleiter erstellt Raum
    │
    ├─► Spieler treten bei (öffentlich oder per Code)
    │
    ├─► Spielleiter startet das Spiel
    │       Spieler werden in Gruppen eingeteilt
    │
    └─► Pro Runde:
            Spieler zahlen Coins in den Topf ein (Timer läuft)
            Topf × Multiplikator ÷ Gruppengröße → Ausschüttung
            Rundenauswertung sichtbar
            Spielleiter startet nächste Runde
                │
                └─► Ende nach fester Rundenzahl
                    oder nach Zufallsprinzip
```

---

## Features

| Feature | Beschreibung |
|---|---|
| **Echtzeit** | WebSocket-basiert (Socket.IO), keine Seitenneuladen während des Spiels |
| **Bots** | 0–50 automatische Bots pro Raum, spielen mit zufälligen Beiträgen |
| **Inkognito-Modus** | Spieler sehen nur ihre eigenen Beiträge, nicht die der anderen |
| **Gruppenaufteilung** | Feste oder jede Runde neu gemischte Gruppen |
| **Rundenmodi** | Feste Rundenzahl oder Wahrscheinlichkeitsmodell |
| **Privat-Räume** | Zugang nur per automatisch generiertem 6-stelligen Code |
| **Spielverlauf** | Abgeschlossene Spiele werden gespeichert und sind einsehbar |
| **Export** | Spielauswertung als Druckansicht (Evaluation-Seite) |
| **Reconnect** | Spieler können nach Verbindungsabbruch nahtlos zurückkehren |

---

## Tech Stack

- **Backend:** Python 3.x · Flask 3.1 · Flask-SocketIO 5.3
- **Async:** gevent (WSGI-Server + WebSocket-Handler)
- **Frontend:** Vanilla JS · Socket.IO Client · Chart.js
- **Persistenz:** In-Memory (kein Datenbank-Dependency) + JSON-Spielhistorie

---

## Installation

### Voraussetzungen

- Python 3.9 oder neuer
- pip

### Schritte

```bash
# 1. Repository klonen
git clone https://github.com/CodewithMax05/Gueterspiel.git
cd Gueterspiel

# 2. Virtuelle Umgebung anlegen und aktivieren
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

# 3. Abhängigkeiten installieren
pip install -r requirements.txt

# 4. Umgebungsvariablen konfigurieren
cp .env.example .env   # oder .env manuell anlegen (siehe unten)
```

### `.env` Datei

Erstelle eine `.env`-Datei im Projektroot:

```env
# Pflichtfeld – beliebiger langer zufälliger String
SECRET_KEY=dein-geheimer-schluessel-hier

# Optionale Felder
FLASK_ENV=development          # production → aktiviert sicherere Cookie-Einstellungen
ALLOWED_ORIGIN=*               # CORS-Ursprung für Socket.IO (z. B. https://deine-domain.de)
```

> **Tipp:** Einen sicheren Key generieren:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

---

## Starten

### Development (lokaler Test)

```bash
python main.py
```

Die App ist dann unter [http://localhost:5000](http://localhost:5000) erreichbar.

### Production (empfohlen)

```bash
gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
         -w 1 \
         --bind 0.0.0.0:5000 \
         main:app
```

> **Wichtig:** Nur `1` Worker verwenden – die Spielzustände liegen im Arbeitsspeicher und werden nicht zwischen Prozessen geteilt.

---

## Bedienung

### Als Spielleiter

1. Startseite öffnen → **„Spiel erstellen"**
2. Einstellungen festlegen (Name, Coins, Multiplikator, Runden, Bots usw.)
3. Raum-Link / -Code an Spieler weitergeben
4. Warten bis alle bereit sind → **„Spiel starten"**
5. Nach jeder Runde Ergebnisse einsehen und nächste Runde starten
6. Nach dem letzten Runde: Gesamtauswertung

### Als Spieler

1. Startseite öffnen → **„Spiel beitreten"**
2. Öffentlichen Raum auswählen oder Code eingeben
3. Namen eingeben → bereit melden
4. Jede Runde: Beitrag per Slider/Eingabe festlegen und bestätigen
5. Rundenauswertung abwarten, weiter bis Spielende

---

## Projektstruktur

```
Gueterspiel/
├── main.py                   # Flask-App + SocketIO-Handler (gesamte Backend-Logik)
├── requirements.txt
├── .env                      # Nicht einchecken!
├── logs/                     # Automatisch erzeugt; rotierendes Log
├── static/
│   └── css/                  # Seitenspezifische Stylesheets
└── templates/
    ├── base.html             # Gemeinsames Layout: Socket.IO-Setup, Timer, Navigation
    ├── index.html            # Startseite
    ├── create_game.html     # Spielerstellung
    ├── join_game.html       # Spiel beitreten
    ├── game_room.html       # Warteraum
    ├── leader_dashboard.html# Spielleiter-Steuerung
    ├── game.html            # Eingabeseite Spieler
    ├── round_results.html   # Rundenauswertung
    ├── evaluation.html      # Endauswertung
    └── history.html         # Spielhistorie
```

---

## Developer

WI24 – Max Schieck, Justin Burian
