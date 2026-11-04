import cv2
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from ultralytics import YOLO
import easyocr
import base64
import json
import os
import re
import sqlite3
import threading
import time
import csv
from collections import Counter, deque

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Configuration MQTT ──────────────────────────────────────────
BROKER   = "pf-zoq8qkg6ltsqavj8z1xh.cedalo.cloud"
PORT     = 1883
USERNAME = "client3"
PASSWORD = "kawib39328@bullbaby.com"

TOPIC_CAMERA_FRAME   = "ANPR/camera/frame"
TOPIC_CAMERA_CONTROL = "ANPR/camera/control"
TOPIC_DETECTION      = "ANPR/vehicle/detection"
TOPIC_PLATE          = "ANPR/vehicle/plate"
TOPIC_INFRACTION     = "ANPR/infraction"

# ── Source vidéo ────────────────────────────────────────────────
SOURCE      = 0
SHOW_WINDOW = True

# ── Réglages du flux envoyé au dashboard ────────────────────────
SEND_FPS     = 8
SEND_SIZE    = (480, 360)
JPEG_QUALITY = 50

# ── Réglages OCR ────────────────────────────────────────────────
OCR_MIN_CONF  = 0.30
VOTE_WINDOW   = 7
VOTE_MIN      = 2
OCR_INTERVAL  = 0.7
DEBUG_OCR     = False
OCR_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + "تونس"

# ── Réglages « sens interdit » ──────────────────────────────────
INFRACTION_TYPE  = "SENS_INTERDIT"
WRONG_WAY        = "approaching"
HIST_LEN         = 20
MIN_TRACK_FRAMES = 10
AREA_CHANGE      = 0.25
MOVE_PX          = 60
CONFIRM_EVALS    = 8
PLATE_WAIT       = 4.0
COOLDOWN         = 60

# ── Base de données et preuves ──────────────────────────────────
DB_PATH      = os.path.join(BASE_DIR, "infractions.db")
EVIDENCE_DIR = os.path.join(BASE_DIR, "preuves")
VEHICLES_CSV = os.path.join(BASE_DIR, "vehicles_db.csv")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

# ════════════════════════════════════════════════════════════════
#  Base véhicules CSV
# ════════════════════════════════════════════════════════════════
def load_vehicles():
    vehicles = {}
    try:
        with open(VEHICLES_CSV, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                key = row['matricule'].strip().upper()
                vehicles[key] = row
        print(f"✅ Base véhicules chargée : {len(vehicles)} entrées")
    except FileNotFoundError:
        print(f"⚠️ vehicles_db.csv introuvable dans {BASE_DIR}")
    return vehicles

vehicles_db = load_vehicles()

def lookup_owner(plate):
    """Cherche le propriétaire — essaie la clé exacte puis sans espaces."""
    key = plate.strip().upper()
    if key in vehicles_db:
        return vehicles_db[key]
    key_nospace = key.replace(" ", "")
    for k, v in vehicles_db.items():
        if k.replace(" ", "") == key_nospace:
            return v
    return None

# ── Chargement des modèles ──────────────────────────────────────
MODEL_PATH = r"C:\projet_anpr\models\exp-3.pt"
model  = YOLO(MODEL_PATH)
reader = easyocr.Reader(['ar', 'en'], gpu=False)

# ── Variables globales ──────────────────────────────────────────
camera_active = False
cap     = None
running = True

ocr_lock      = threading.Lock()
ocr_crops     = {}
votes_by_tid  = {}
plate_by_tid  = {}
tracks        = {}
last_infraction = {}
last_plate    = ""

# ════════════════════════════════════════════════════════════════
#  Plaques : reconnaissance du format
# ════════════════════════════════════════════════════════════════
TO_LETTER = {'0':'O','1':'I','5':'S','8':'B','6':'G','2':'Z','4':'A'}
TO_DIGIT  = {'O':'0','Q':'0','D':'0','I':'1','L':'1',
             'S':'5','B':'8','G':'6','Z':'2','A':'4'}
AR_RE = re.compile(r'[\u0600-\u06FF]+')
FR_RE = re.compile(r'^[A-Z]{2}\d{3}[A-Z]{2}$')

def _digits(s):
    return re.sub(r'\D', '', s)

def _format_tn(left, right, mid="TUN"):
    if 1 <= len(left) <= 3 and 1 <= len(right) <= 4:
        return f"{left} {mid} {right}"
    return None

def parse_plate(tokens):
    raw = " ".join(t.strip().upper() for t in tokens if t.strip())
    if not raw:
        return None
    latin = re.sub(r'[^A-Z0-9]', '', raw)
    if AR_RE.search(raw):
        for t in tokens:
            if AR_RE.search(t):
                p = AR_RE.split(t.strip())
                first, last = _digits(p[0]), _digits(p[-1])
                if first and last:
                    return _format_tn(last, first)
        parts = AR_RE.split(raw)
        return _format_tn(_digits(parts[0]), _digits(parts[-1]))
    if len(latin) == 7 and not latin.isdigit():
        c = list(latin)
        for i in (0, 1, 5, 6):
            c[i] = TO_LETTER.get(c[i], c[i])
        for i in (2, 3, 4):
            c[i] = TO_DIGIT.get(c[i], c[i])
        t = "".join(c)
        if FR_RE.match(t):
            return f"{t[:2]}-{t[2:5]}-{t[5:]}"
    nums    = re.findall(r'\d+', raw)
    letters = re.sub(r'[^A-Z]', '', raw)
    if len(nums) >= 2 and len(letters) <= 3:
        return _format_tn(nums[0], nums[-1], "RS" if "RS" in letters else "TUN")
    if latin.isdigit() and 5 <= len(latin) <= 7:
        return _format_tn(latin[:-4], latin[-4:])
    return None

# ════════════════════════════════════════════════════════════════
#  Sens interdit
# ════════════════════════════════════════════════════════════════
def is_wrong_way(hist):
    x0, y0, a0 = hist[0]
    x1, y1, a1 = hist[-1]
    ratio = a1 / a0 if a0 > 0 else 1.0
    dx, dy = x1 - x0, y1 - y0
    if WRONG_WAY == "approaching": return ratio >= 1 + AREA_CHANGE
    if WRONG_WAY == "receding":    return ratio <= 1 / (1 + AREA_CHANGE)
    if WRONG_WAY == "left":        return dx <= -MOVE_PX
    if WRONG_WAY == "right":       return dx >= MOVE_PX
    if WRONG_WAY == "up":          return dy <= -MOVE_PX
    if WRONG_WAY == "down":        return dy >= MOVE_PX
    return False

# ════════════════════════════════════════════════════════════════
#  Base de données SQLite
# ════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS infractions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            horodatage    TEXT NOT NULL,
            plaque        TEXT NOT NULL,
            type          TEXT NOT NULL,
            piste_id      INTEGER,
            image         TEXT,
            statut        TEXT NOT NULL DEFAULT 'EN_ATTENTE',
            nom           TEXT,
            prenom        TEXT,
            carte_grise   TEXT,
            email         TEXT,
            telephone     TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plaque ON infractions(plaque)")
    conn.commit()
    return conn

db = init_db()

# ════════════════════════════════════════════════════════════════
#  Enregistrement infraction + lookup propriétaire
# ════════════════════════════════════════════════════════════════
def register_infraction(tid, plate, frame_with_boxes):
    now = time.time()
    key = (plate, INFRACTION_TYPE)
    if plate != "INCONNUE" and now - last_infraction.get(key, 0) < COOLDOWN:
        print(f"ℹ️ Infraction déjà enregistrée pour {plate} (délai {COOLDOWN}s)")
        return None
    last_infraction[key] = now

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    safe  = re.sub(r'[^A-Za-z0-9]+', '_', plate)
    image_path = os.path.join(
        EVIDENCE_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{safe}.jpg")
    cv2.imwrite(image_path, frame_with_boxes)

    # Recherche propriétaire
    owner = lookup_owner(plate)
    o = owner if owner else {}

    cur = db.execute(
        "INSERT INTO infractions "
        "(horodatage, plaque, type, piste_id, image, statut, nom, prenom, carte_grise, email, telephone) "
        "VALUES (?, ?, ?, ?, ?, 'EN_ATTENTE', ?, ?, ?, ?, ?)",
        (stamp, plate, INFRACTION_TYPE, tid, image_path,
         o.get('nom',''), o.get('prenom',''),
         o.get('carte_grise',''), o.get('email',''), o.get('telephone','')))
    db.commit()
    inf_id = cur.lastrowid

    payload = {
        "id"          : inf_id,
        "horodatage"  : stamp,
        "plaque"      : plate,
        "type"        : INFRACTION_TYPE,
        "statut"      : "EN_ATTENTE",
        "proprietaire": {
            "nom"           : o.get('nom', 'INCONNU'),
            "prenom"        : o.get('prenom', ''),
            "date_naissance": o.get('date_naissance', ''),
            "carte_grise"   : o.get('carte_grise', ''),
            "email"         : o.get('email', ''),
            "telephone"     : o.get('telephone', '')
        }
    }
    mqtt_client.publish(TOPIC_INFRACTION,
                        json.dumps(payload, ensure_ascii=False), qos=1)
    print(f"🚨 INFRACTION #{inf_id} : {plate} | "
          f"propriétaire : {o.get('nom','INCONNU')} {o.get('prenom','')}")
    return inf_id

# ════════════════════════════════════════════════════════════════
#  Tracking reset
# ════════════════════════════════════════════════════════════════
def reset_tracking():
    global tracks, ocr_crops, votes_by_tid, plate_by_tid, last_plate
    tracks.clear(); ocr_crops.clear()
    votes_by_tid.clear(); plate_by_tid.clear()
    last_plate = ""

# ════════════════════════════════════════════════════════════════
#  MQTT callbacks
# ════════════════════════════════════════════════════════════════
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("✅ Connecté au broker MQTT")
        client.subscribe(TOPIC_CAMERA_CONTROL)
    else:
        print(f"❌ Erreur connexion MQTT : rc={reason_code}")

def on_message(client, userdata, msg):
    global camera_active, cap
    payload = msg.payload.decode()
    print(f"📩 Commande reçue : {payload}")
    if payload == "ON":
        reset_tracking()
        camera_active = True
        cap = cv2.VideoCapture(SOURCE)
        print("📷 Caméra démarrée")
    elif payload == "OFF":
        camera_active = False
        if cap:
            cap.release()
            cap = None
        reset_tracking()
        print("📷 Caméra arrêtée")

# ════════════════════════════════════════════════════════════════
#  OCR (thread séparé)
# ════════════════════════════════════════════════════════════════
def preprocess(crop):
    gray  = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scale = min(8.0, max(3.0, 100.0 / max(gray.shape[0], 1)))
    gray  = cv2.resize(gray, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_CUBIC)
    gray  = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8)).apply(gray)
    return gray

def ocr_worker():
    global last_plate
    while running:
        tid, crop = None, None
        with ocr_lock:
            if ocr_crops:
                todo = [k for k in ocr_crops if k not in plate_by_tid]
                if todo:
                    tid  = todo[0]
                    crop = ocr_crops.pop(tid)
                else:
                    ocr_crops.clear()
        if crop is None:
            time.sleep(0.05)
            continue
        time.sleep(OCR_INTERVAL)
        try:
            results = reader.readtext(preprocess(crop),
                                      allowlist=OCR_ALLOWLIST, detail=1)
        except Exception as e:
            print(f"⚠️ Erreur OCR : {e}")
            continue
        if DEBUG_OCR:
            print(f"🔎 OCR brut (piste {tid}) : "
                  f"{[(t, round(float(c),2)) for _,t,c in results]}")
        results.sort(key=lambda r: r[0][0][0])
        parts = [t for _, t, c in results if c >= OCR_MIN_CONF and t.strip()]
        text  = parse_plate(parts)
        if not text:
            continue
        votes = votes_by_tid.setdefault(tid, deque(maxlen=VOTE_WINDOW))
        votes.append(text)
        best, count = Counter(votes).most_common(1)[0]
        if count >= VOTE_MIN and tid not in plate_by_tid:
            plate_by_tid[tid] = best
            last_plate = best
            mqtt_client.publish(TOPIC_PLATE, best, qos=0)
            print(f"🔢 Matricule (piste {tid}) : {best}")

# ── Client MQTT v2 ──────────────────────────────────────────────
mqtt_client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="anpr_python_cam"
)
mqtt_client.username_pw_set(USERNAME, PASSWORD)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(BROKER, PORT, 60)
mqtt_client.loop_start()

threading.Thread(target=ocr_worker, daemon=True).start()

# ════════════════════════════════════════════════════════════════
#  Boucle principale
# ════════════════════════════════════════════════════════════════
print("🚀 Système ANPR démarré - en attente de commandes...")
last_send  = 0
frames_sent = 0
last_log   = 0
frame_idx  = 0

try:
    while True:
        if camera_active and cap and cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                if not isinstance(SOURCE, int):
                    print("🏁 Fin de la vidéo")
                    camera_active = False
                    cap.release(); cap = None
                continue
            frame_idx += 1

            results  = model.track(frame, conf=0.5, persist=True,
                                   tracker="bytetrack.yaml", verbose=False)
            boxes    = results[0].boxes
            annotated = results[0].plot()
            ids      = boxes.id.int().tolist() if boxes.id is not None else []

            for box, tid in zip(boxes, ids):
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cx, cy = (x1+x2)/2, (y1+y2)/2
                area   = max(1, (x2-x1)*(y2-y1))

                tr = tracks.setdefault(tid, {
                    "hist": deque(maxlen=HIST_LEN), "wrong": 0,
                    "pending": None, "reported": False, "last": frame_idx})
                tr["hist"].append((cx, cy, area))
                tr["last"] = frame_idx

                if tid not in plate_by_tid:
                    h, w = frame.shape[:2]; pad = 5
                    crop = frame[max(0,y1-pad):min(h,y2+pad),
                                 max(0,x1-pad):min(w,x2+pad)]
                    if crop.size > 0:
                        with ocr_lock:
                            ocr_crops[tid] = crop.copy()

                if not tr["reported"] and len(tr["hist"]) >= MIN_TRACK_FRAMES:
                    if is_wrong_way(tr["hist"]):
                        tr["wrong"] += 1
                    else:
                        tr["wrong"] = max(0, tr["wrong"]-1)
                    if tr["wrong"] >= CONFIRM_EVALS and tr["pending"] is None:
                        tr["pending"] = time.time()
                        print(f"⚠️ Sens interdit suspecté (piste {tid})")

                if tr["pending"] is not None and not tr["reported"]:
                    cv2.putText(annotated, "SENS INTERDIT",
                                (x1, max(20, y1-30)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)
                    plate = plate_by_tid.get(tid)
                    if plate or time.time()-tr["pending"] > PLATE_WAIT:
                        register_infraction(tid, plate or "INCONNUE", annotated)
                        tr["reported"] = True

            if frame_idx % 100 == 0:
                for k in [k for k,v in tracks.items()
                          if frame_idx-v["last"] > 150]:
                    tracks.pop(k, None)
                    plate_by_tid.pop(k, None)
                    votes_by_tid.pop(k, None)

            now = time.time()
            if now - last_send >= 1.0/SEND_FPS:
                last_send = now
                small = cv2.resize(annotated, SEND_SIZE)
                ok, buffer = cv2.imencode('.jpg', small,
                                          [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    mqtt_client.publish(
                        TOPIC_CAMERA_FRAME,
                        base64.b64encode(buffer).decode('utf-8'), qos=0)
                    frames_sent += 1
                    if now-last_log >= 3:
                        last_log = now
                        print(f"📤 {frames_sent} images | pistes:{len(tracks)} "
                              f"| plaque:{last_plate or '-'}")

            detections = [{"class": results[0].names[int(b.cls)],
                           "confidence": round(float(b.conf),2)} for b in boxes]
            if detections:
                mqtt_client.publish(TOPIC_DETECTION, json.dumps({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "detections": detections}))

            if SHOW_WINDOW:
                try:
                    cv2.imshow("ANPR - Detection", annotated)
                    if cv2.waitKey(1) == ord('q'):
                        break
                except cv2.error:
                    print("⚠️ Fenêtre locale désactivée.")
                    SHOW_WINDOW = False
        else:
            time.sleep(0.1)

except KeyboardInterrupt:
    print("\n🛑 Arrêt du système")

finally:
    running = False
    if cap: cap.release()
    if SHOW_WINDOW:
        try: cv2.destroyAllWindows()
        except cv2.error: pass
    db.close()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
