# ANPR : détection de sens interdit et lecture de plaques (IoT)

Système de vision par ordinateur qui détecte un véhicule roulant à contre-sens,
lit sa plaque d'immatriculation, enregistre l'infraction (SQLite + image preuve)
et la diffuse par MQTT vers un dashboard Node-RED. Le dashboard affiche aussi les
capteurs d'un ESP32 et commande la caméra et une LED à distance.

> Projet pédagogique (module IoT). Les données utilisées sont fictives.

## Démonstration

[Vidéo du dashboard](LIEN_VIDEO) · [Guide de révision (PDF)](docs/Projet_ANPR_Guide.pdf)

## Fonctionnement

```
ESP32 (capteurs) ──MQTT──┐
                         ├── Broker MQTT (Cedalo) ──── Node-RED ──── Dashboard « ANPR UI »
Python (OpenCV, YOLO,    │                                 │
ByteTrack, EasyOCR) ─────┘                                 └── API /export-infractions (CSV)
        │
   SQLite + preuves/*.jpg
```

1. **Détection** : un modèle YOLO (`exp-3.pt`, classe `carplater`) détecte les plaques ; ByteTrack suit chaque véhicule.
2. **Sens interdit** : la surface de la boîte suivie grossit d'au moins 25 % sur l'historique (20 positions, minimum 10 images), confirmée 8 fois de suite.
3. **Lecture de plaque** : EasyOCR (arabe + anglais) après prétraitement (niveaux de gris, agrandissement, CLAHE). Un vote 2 sur 7 valide la lecture. Formats gérés : `NNN TUN NNNN`, `RS`, format français.
4. **Enregistrement** : insertion SQLite, image preuve avec boîtes, recherche du propriétaire dans `vehicles_db.csv`. Si la plaque n'est pas lue en 4 s, elle est enregistrée comme `INCONNUE`. Un délai de 60 s évite les doublons pour une même plaque.
5. **Diffusion** : message JSON sur `ANPR/infraction` (QoS 1), image du flux à 8 images/s (480×360, JPEG 50) sur `ANPR/camera/frame`.

## Topics MQTT

| Topic | Sens | Contenu |
|---|---|---|
| `ANPR/camera/control` | Node-RED vers Python | `ON` / `OFF` |
| `ANPR/camera/frame` | Python vers Node-RED | image JPEG en base64 |
| `ANPR/vehicle/detection` | Python vers Node-RED | détections (classe, confiance) |
| `ANPR/vehicle/plate` | Python vers Node-RED | plaque lue |
| `ANPR/infraction` | Python vers Node-RED | infraction et propriétaire (JSON) |
| `DATA/TEMP`, `DATA/HUM`, `DATA/GAZ`, `DATA/GAZ_ALERTE`, `DATA/DIST`, `DATA/IR`, `DATA/BUZZER`, `DATA/GPS`, `DATA/GPS_SPEED` | ESP32 vers Node-RED | mesures des capteurs |
| `LED/CONTROL` | Node-RED vers ESP32 | commande de la LED |

## Installation

```bash
git clone https://github.com/<utilisateur>/anpr-wrong-way-iot.git
cd anpr-wrong-way-iot
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # puis renseigner les identifiants MQTT
cp data/vehicles_db.example.csv data/vehicles_db.csv
```

Le fichier `vehicles_db.csv` doit contenir les colonnes :
`matricule, nom, prenom, date_naissance, carte_grise, email, telephone`.

## Lancement

1. Démarrer Node-RED, importer `node-red/flows_node_red.json`, renseigner le broker et ses identifiants, puis déployer. Le dashboard est sur `http://localhost:1880/ui`.
2. Lancer le script Python : `python src/anpr_infractions.py`.
3. Dans le dashboard, cliquer sur **Démarrer Caméra**. La source est `SOURCE = 0` (webcam) ; remplacez par le chemin d'une vidéo pour tester.

## Paramètres principaux (dans le script)

| Paramètre | Valeur | Rôle |
|---|---|---|
| `conf` YOLO | 0,5 | confiance minimale de détection |
| `AREA_CHANGE` | 0,25 | grossissement minimal pour « sens interdit » |
| `CONFIRM_EVALS` | 8 | évaluations avant de suspecter |
| `OCR_MIN_CONF` | 0,30 | confiance OCR minimale |
| `VOTE_WINDOW` / `VOTE_MIN` | 7 / 2 | vote de la plaque |
| `PLATE_WAIT` | 4 s | attente de plaque avant `INCONNUE` |
| `COOLDOWN` | 60 s | délai entre deux infractions pour une même plaque |

## Contenu du dépôt

```
src/anpr_infractions.py       détection, OCR, base, MQTT
src/export_csv.py             export des infractions en CSV (appelé par Node-RED)
models/exp-3.pt               modèle YOLO (classe carplater)
node-red/flows_node_red.json  flow et dashboard « ANPR UI »
data/vehicles_db.example.csv  exemple de registre (données fictives)
docs/                         guide de révision, captures
```

## Limites

- La règle de sens interdit repose sur la variation de surface de la boîte : elle dépend de l'angle de la caméra et du sens de circulation (`WRONG_WAY`).
- Le firmware de l'ESP32 n'est pas inclus : les topics sont ceux utilisés par le flow Node-RED.
- La lecture OCR est sensible à la lumière et à la résolution (15 infractions sur 45 enregistrées avec la plaque `INCONNUE`).
- Pas de notification par e-mail dans cette version.
- Données personnelles : n'utiliser que des données fictives ou autorisées.

## Licence

MIT (à adapter) · Auteur : <Votre nom> · Encadrement : <Enseignant>
