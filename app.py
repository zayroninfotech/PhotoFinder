"""
Photo Finder – Flask Backend
Superadmin → manages users & subscriptions
Admin       → submits Google Drive link → QR generated
User        → scans QR → uploads selfie → sees matched photos → download / email
"""

import os, re, io, json, uuid, zipfile, smtplib, threading, shutil, hmac, hashlib, tempfile

# Suppress TensorFlow/Keras warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
os.environ['TF_SUPPRESS_CONVERSION_WARNING'] = '1'
from pymongo import MongoClient
try:
    import razorpay as _rzp_lib
    RAZORPAY_AVAILABLE = True
except ImportError:
    RAZORPAY_AVAILABLE = False
from datetime import date, datetime, timedelta
from pathlib import Path
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import qrcode
import requests
from flask import (Flask, render_template, request, session,
                   redirect, url_for, jsonify, send_file,
                   Response, stream_with_context)
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash

import config

# Allow overriding config from environment variables (for cloud deployment)
config.ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", config.ADMIN_USERNAME)
config.ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", config.ADMIN_PASSWORD)
config.SECRET_KEY     = os.environ.get("SECRET_KEY",     config.SECRET_KEY)
config.EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",   config.EMAIL_SENDER)
config.EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", config.EMAIL_PASSWORD)

# ── MongoDB connection ────────────────────────────────────────────────────────
_mongo_db = None

def _get_db():
    """Return MongoDB database instance; None if unavailable."""
    global _mongo_db
    if _mongo_db is not None:
        return _mongo_db
    try:
        uri     = getattr(config, "MONGODB_URI", "mongodb://localhost:27017/")
        db_name = getattr(config, "MONGODB_DB",  "photofinder")
        client  = MongoClient(uri, serverSelectionTimeoutMS=4000)
        client.admin.command("ping")          # verify connection
        _mongo_db = client[db_name]
        # Create unique index on username once
        _mongo_db["users"].create_index("username", unique=True, background=True)
        _mongo_db["users"].create_index("id",       unique=True, background=True)
        try:
            print(f"[INFO] MongoDB connected -> {uri}  db={db_name}")
        except Exception:
            pass  # ignore console encoding errors on Windows
    except Exception as e:
        try:
            print(f"[WARN] MongoDB unavailable; falling back to JSON")
        except Exception:
            pass
        _mongo_db = None
    return _mongo_db

def _ucol():
    """Return the 'users' collection or None (JSON fallback)."""
    db = _get_db()
    return db["users"] if db is not None else None

def _pcol():
    """Return the 'event_photos' collection or None (JSON fallback)."""
    db = _get_db()
    if db is not None:
        col = db["event_photos"]
        col.create_index([("event_id", 1), ("admin_id", 1)], background=True)
        col.create_index([("created_at", 1)], background=True)
        return col
    return None

def _scol():
    """Return the 'subscriptions' collection or None (JSON fallback)."""
    db = _get_db()
    if db is not None:
        col = db["subscriptions"]
        col.create_index([("user_id", 1), ("created_at", -1)], background=True)
        col.create_index([("status", 1)], background=True)
        col.create_index([("subscription_id", 1)], unique=True, background=True)
        return col
    return None

# Face detection is loaded lazily to avoid OOM on startup
FACE_OK       = False
_face_tried = False   # only warn once

def _load_insight():
    global FACE_OK, _face_tried
    if _face_tried:          # already attempted — return cached result
        return FACE_OK
    _face_tried = True
    try:
        # Suppress TensorFlow/Keras warnings
        import warnings
        warnings.filterwarnings('ignore', category=DeprecationWarning)
        warnings.filterwarnings('ignore', category=FutureWarning)
        from deepface import DeepFace
        FACE_OK = True
        print(f"[INFO] DeepFace loaded OK with model={config.FACE_MODEL}")
    except Exception as e:
        FACE_OK = False
        print(f"[WARN] DeepFace unavailable: {e}")
    return FACE_OK

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# In-memory cache for pickle files to avoid repeated disk reads
_pickle_cache = {}  # {event_id: (mtime, entries)}

BASE   = Path(__file__).parent
DATA   = BASE / "data"
EVENTS = DATA / "events"
QRS    = DATA / "qrcodes"
UPS    = DATA / "uploads"
RES    = DATA / "results"
SUBS   = DATA / "subscriptions"
USERS_FILE           = DATA / "users.json"
PAYMENT_SETTINGS_FILE = DATA / "payment_settings.json"
PAYMENT_QR_FILE       = DATA / "payment_qr.png"

for d in [EVENTS, QRS, UPS, RES, SUBS]:
    d.mkdir(parents=True, exist_ok=True)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# Subscription plan options (days)
SUBSCRIPTION_PLANS = [
    {"days": 1,   "label": "1 Day",     "tag": "Trial",         "price": "₹99"},
    {"days": 3,   "label": "3 Days",    "tag": "Short Trial",   "price": "₹249"},
    {"days": 6,   "label": "6 Days",    "tag": "Week Trial",    "price": "₹449"},
    {"days": 30,  "label": "30 Days",   "tag": "Monthly",       "price": "₹999"},
    {"days": 90,  "label": "90 Days",   "tag": "Quarterly",     "price": "₹2,499"},
    {"days": 180, "label": "180 Days",  "tag": "Half Yearly",   "price": "₹4,499"},
    {"days": 365, "label": "365 Days",  "tag": "Annual",        "price": "₹7,999"},
]
VALID_PLAN_DAYS = {p["days"] for p in SUBSCRIPTION_PLANS}

# Run one-time JSON->MongoDB migration on startup, then bootstrap superadmin
with app.app_context():
    try:
        migrate_json_to_mongo()
    except Exception:
        pass
    try:
        _ensure_superadmin_exists()
    except Exception:
        pass

# ── Auto-sync: check Drive for new photos every 15 seconds ────────────────────
_sync_locks = {}   # per-event lock to prevent concurrent syncs

def _auto_sync_all_events():
    """Background job — runs every 15 sec.
       Re-runs gdown for each ready event (gdown skips already-downloaded files).
       Only triggers reindex if new images appear in the images/ dir."""
    import threading
    try:
        if EVENTS.exists():
            for event_dir in EVENTS.iterdir():
                if not event_dir.is_dir():
                    continue
                meta        = _load_meta(event_dir / "meta.json")
                status_path = event_dir / "status.json"
                status      = _load_meta(status_path)
                folder_id   = meta.get("folder_id", "")
                # Skip if not ready, no folder, or already syncing
                if not folder_id:
                    continue
                if status.get("state") not in ("ready", None):
                    continue
                if meta.get("status") != "ready":
                    continue
                if _sync_locks.get(event_dir.name):
                    continue
                # Check local image count vs encoded count to detect new files
                try:
                    images_dir    = event_dir / "images"
                    enc_path      = event_dir / "face_encodings.pkl"
                    local_imgs    = {p.name for p in images_dir.rglob("*")
                                     if p.suffix.lower() in IMG_EXTS} if images_dir.exists() else set()
                    encoded_names = set()
                    if enc_path.exists():
                        import pickle as _pkl
                        try:
                            with open(str(enc_path), "rb") as _f:
                                for _e in _pkl.load(_f):
                                    encoded_names.add(_e["filename"])
                        except Exception:
                            pass
                    new_local = local_imgs - encoded_names
                    if not new_local:
                        continue   # nothing new locally — skip
                    print(f"[AUTO-SYNC] {event_dir.name}: {len(new_local)} new photo(s) found")
                    _sync_locks[event_dir.name] = True
                    def _run(eid):
                        try:
                            reindex_event_bg(eid)
                        finally:
                            _sync_locks.pop(eid, None)
                    threading.Thread(target=_run, args=(event_dir.name,), daemon=True).start()
                except Exception:
                    pass   # skip silently on error
    except Exception as e:
        try:
            print(f"[WARN] Auto-sync error: {e}")
        except Exception:
            pass
    finally:
        import threading
        threading.Timer(15, _auto_sync_all_events).start()  # every 15 seconds

# Pre-warm MediaPipe model on startup (so first search is instant)
def _prewarm_insight():
    try:
        _load_insight()
        print("[INFO] MediaPipe pre-warmed on startup")
    except Exception as e:
        print(f"[WARN] MediaPipe pre-warm failed: {e}")

import threading as _threading
_threading.Thread(target=_prewarm_insight, daemon=True).start()
_threading.Timer(10, _auto_sync_all_events).start()  # start auto-sync after 10s

# ── Payment settings helpers ──────────────────────────────────────────────────

PAYMENT_DEFAULTS = {
    "upi_id":         "",
    "upi_name":       "",
    "bank_name":      "",
    "account_name":   "",
    "account_number": "",
    "ifsc_code":      "",
    "branch":         "",
    "amount_note":    "Pay as per your selected plan and mention your Username in the payment remarks.",
    "has_qr":         False,
}

def load_payment_settings() -> dict:
    s = dict(PAYMENT_DEFAULTS)
    if PAYMENT_SETTINGS_FILE.exists():
        try:
            s.update(json.loads(PAYMENT_SETTINGS_FILE.read_text()))
        except Exception:
            pass
    s["has_qr"] = PAYMENT_QR_FILE.exists()
    return s

def save_payment_settings(settings: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    PAYMENT_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ── User helpers ───────────────────────────────────────────────────────────────

def load_users() -> dict:
    """Return all users as a dict keyed by user ID.
       Uses MongoDB if available, falls back to JSON file."""
    col = _ucol()
    if col is not None:
        try:
            return {u["id"]: u for u in col.find({}, {"_id": 0})}
        except Exception as e:
            print(f"[WARN] MongoDB load_users: {e}")
    # JSON fallback
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_users(users: dict):
    """Upsert all users into MongoDB; also write JSON as backup."""
    col = _ucol()
    if col is not None:
        try:
            for uid, user in users.items():
                col.replace_one({"id": uid}, {**user, "id": uid}, upsert=True)
        except Exception as e:
            print(f"[WARN] MongoDB save_users: {e}")
    # Always keep JSON backup
    DATA.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))

def delete_user(user_id: str):
    """Delete a single user by ID from MongoDB and JSON backup."""
    col = _ucol()
    if col is not None:
        try:
            col.delete_one({"id": user_id})
        except Exception as e:
            print(f"[WARN] MongoDB delete_user: {e}")
    # Update JSON backup
    users = {}
    if USERS_FILE.exists():
        try:
            users = json.loads(USERS_FILE.read_text())
        except Exception:
            pass
    if user_id in users:
        del users[user_id]
    DATA.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))

def update_user(user_id: str, fields: dict):
    """Update specific fields of a user in MongoDB and JSON backup."""
    col = _ucol()
    if col is not None:
        try:
            col.update_one({"id": user_id}, {"$set": fields})
        except Exception as e:
            print(f"[WARN] MongoDB update_user: {e}")
    # Update JSON backup
    users = load_users()
    if user_id in users:
        users[user_id].update(fields)
        DATA.mkdir(parents=True, exist_ok=True)
        USERS_FILE.write_text(json.dumps(users, indent=2))

def get_user_by_username(username: str):
    """Find a user by username; returns (id, user_dict) or (None, None)."""
    col = _ucol()
    if col is not None:
        try:
            u = col.find_one(
                {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
                {"_id": 0}
            )
            if u:
                return u["id"], u
            return None, None
        except Exception as e:
            print(f"[WARN] MongoDB get_user_by_username: {e}")
    # JSON fallback
    users = load_users()
    for uid, user in users.items():
        if user.get("username", "").lower() == username.lower():
            return uid, user
    return None, None

def migrate_json_to_mongo():
    """One-time migration: import existing users.json into MongoDB."""
    col = _ucol()
    if col is None or not USERS_FILE.exists():
        return
    try:
        existing = json.loads(USERS_FILE.read_text())
        if not existing:
            return
        migrated = 0
        for uid, user in existing.items():
            if col.find_one({"id": uid}) is None:
                col.insert_one({**user, "id": uid})
                migrated += 1
        if migrated:
            print(f"[INFO] Migrated {migrated} users from JSON -> MongoDB")
    except Exception as e:
        print(f"[WARN] Migration error: {e}")


def _ensure_superadmin_exists():
    """Auto-create the default superadmin MongoDB record on first run."""
    col = _ucol()
    if col is None:
        return  # MongoDB not available; superadmin uses config.py only
    if col.find_one({"id": "superadmin"}):
        return  # already exists
    try:
        sa = {
            "_id":               "superadmin",
            "id":                "superadmin",
            "username":          config.ADMIN_USERNAME,
            "password":          generate_password_hash(config.ADMIN_PASSWORD),
            "email":             "",
            "company":           "Zayron Infotech Pvt. Ltd.",
            "phone":             "",
            "is_active":         True,
            "role":              "superadmin",
            "subscription_end":  "2126-01-01",
            "subscription_days": 36500,
            "payment_status":    "approved",
            "force_logout":      False,
            "created_at":        datetime.utcnow().isoformat(),
        }
        col.insert_one(sa)
        try:
            print(f"[INFO] Superadmin bootstrapped: {config.ADMIN_USERNAME}")
        except Exception:
            pass
    except Exception as e:
        try:
            print(f"[WARN] Could not bootstrap superadmin: {e}")
        except Exception:
            pass


def is_subscription_active(user: dict) -> bool:
    if not user.get("is_active", False):
        return False
    end_str = user.get("subscription_end")
    if not end_str:
        return False
    try:
        return date.today() <= date.fromisoformat(end_str)
    except Exception:
        return False

def days_remaining(user: dict) -> int:
    end_str = user.get("subscription_end")
    if not end_str:
        return 0
    try:
        delta = (date.fromisoformat(end_str) - date.today()).days
        return max(0, delta)
    except Exception:
        return 0

def plan_label(days: int) -> str:
    for p in SUBSCRIPTION_PLANS:
        if p["days"] == days:
            return f"{p['label']} ({p['tag']})"
    return f"{days} Days"

# ── Auth decorators ───────────────────────────────────────────────────────────

def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        # Subscription check moved to action level (admin_submit) instead of route level
        # Allows users with expired subscriptions to view dashboard, but blocks actions
        if not session.get("is_superadmin"):
            uid = session.get("user_id")
            if uid:
                user = load_users().get(uid)
                # Force-logout check (still applies)
                if user and user.get("force_logout"):
                    update_user(uid, {"force_logout": False})
                    session.clear()
                    return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

def _superadmin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin") or not session.get("is_superadmin"):
            return redirect(url_for("superadmin_login"))
        return f(*args, **kwargs)
    return wrapper

# ── Drive / processing helpers ────────────────────────────────────────────────

_drive_cache: dict = {}   # folder_id -> {"ts": float, "files": list}
_DRIVE_CACHE_TTL  = 600   # 10 minutes

def _cached_drive_images(folder_id: str) -> list:
    import time
    entry = _drive_cache.get(folder_id)
    if entry and (time.time() - entry["ts"]) < _DRIVE_CACHE_TTL:
        return entry["files"]
    files = _scrape_drive_files(folder_id)
    if files:
        _drive_cache[folder_id] = {"ts": time.time(), "files": files}
    return files

def get_folder_id(link: str):
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    return m.group(1) if m else None

def _scrape_drive_files(folder_id: str) -> list:
    """
    Use Google Drive embeddedfolderview (static HTML, no JS needed) to get
    [{"id": FILE_ID, "name": FILENAME, "thumb": THUMB_URL}, ...] for images.
    """
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    files, seen_ids = [], set()
    _IMG_RE = r'\.(?:jpe?g|png|webp|bmp|gif)'
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        text = resp.text
        # Each entry: id="entry-{FILE_ID}" ... flip-entry-title">{FILENAME}</div>
        for fid, fname in re.findall(
            r'id="entry-([a-zA-Z0-9_-]{25,44})"[^>]*>.*?'
            r'flip-entry-title">(.*?)</div>',
            text, re.IGNORECASE | re.DOTALL
        ):
            fname = fname.strip()
            if re.search(_IMG_RE, fname, re.IGNORECASE) and fid not in seen_ids:
                seen_ids.add(fid)
                files.append({"id": fid, "name": fname})
        # Fallback: extract IDs from /file/d/ links + image names, pair by order
        if not files:
            ids   = re.findall(r'/file/d/([a-zA-Z0-9_-]{25,44})', text)
            names = re.findall(r'flip-entry-title">(.*?)</div>', text)
            img_pairs = [(i, n) for i, n in zip(ids, names)
                         if re.search(_IMG_RE, n, re.IGNORECASE)]
            for fid, fname in img_pairs:
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    files.append({"id": fid, "name": fname.strip()})
    except Exception as e:
        print(f"[WARN] Drive embeddedfolderview scrape: {e}")
    return files


def _download_drive_file(file_id: str, dest: Path) -> bool:
    """Download a single public Drive file directly via requests (no gdown)."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    urls = [
        f"https://drive.google.com/thumbnail?id={file_id}&sz=w2000",
        f"https://lh3.googleusercontent.com/d/{file_id}=w2000",
        f"https://drive.google.com/uc?export=download&id={file_id}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=60, stream=True, headers=headers)
            ct = r.headers.get("Content-Type", "")
            if r.status_code == 200 and "image" in ct:
                dest.write_bytes(r.content)
                return True
            # Handle Google virus-scan confirm page
            if r.status_code == 200 and "confirm" in r.text[:800].lower():
                token = re.search(r'confirm=([0-9A-Za-z_-]+)', r.text)
                if token:
                    r2 = requests.get(
                        f"https://drive.google.com/uc?export=download"
                        f"&id={file_id}&confirm={token.group(1)}",
                        timeout=60, stream=True, headers=headers)
                    if r2.status_code == 200 and "image" in r2.headers.get("Content-Type", ""):
                        dest.write_bytes(r2.content)
                        return True
        except Exception:
            continue
    return False


def build_encodings(event_dir: Path, status_path: Path):
    """
    Build/update face encodings for an event.
    1. Scrape Drive folder HTML → get file IDs
    2. Download each photo directly via requests (no gdown)
    3. Encode with InsightFace (incremental — skip already-encoded)
    FALLBACK: if scraping returns 0, try gdown; if that also fails → error
    """
    import pickle, numpy as np
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    import threading as _th

    meta       = _load_meta(event_dir / "meta.json")
    folder_id  = meta.get("folder_id", "")
    enc_path   = event_dir / "face_encodings.pkl"
    images_dir = event_dir / "images"

    # Load existing encodings (incremental — skip already-processed filenames)
    existing = {}
    if enc_path.exists():
        try:
            with open(str(enc_path), "rb") as f:
                for entry in pickle.load(f):
                    existing[entry["filename"]] = entry
        except Exception:
            existing = {}

    # ── STEP 1: Scrape Drive folder for file IDs ──────────────────────────────
    if folder_id:
        _set_status(status_path, "downloading", "Scanning Drive folder…")
        drive_files = _scrape_drive_files(folder_id)

        if drive_files:
            images_dir.mkdir(parents=True, exist_ok=True)
            to_download = [f for f in drive_files
                           if not (images_dir / f["name"]).exists()]
            if to_download:
                _set_status(status_path, "downloading",
                            f"Downloading {len(to_download)} new photo(s)…")
                def _dl(item):
                    dest = images_dir / item["name"]
                    ok = _download_drive_file(item["id"], dest)
                    if not ok:
                        print(f"[WARN] Could not download {item['name']}")
                with _TPE(max_workers=4) as pool:
                    list(pool.map(_dl, to_download))
        else:
            # Scraping returned 0 — set error, folder may not be public
            _set_status(status_path, "error",
                        "No images found. Make sure your Google Drive folder is "
                        "shared publicly (Anyone with the link → Viewer).")
            meta["status"] = "error"
            _save_meta(event_dir / "meta.json", meta)
            return

    # ── STEP 2: Encode all local images ───────────────────────────────────────
    imgs = []
    if images_dir.exists():
        imgs = [p for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]

    if not imgs:
        msg = ("No images found. Make sure your Google Drive folder is shared "
               "publicly (Anyone with the link → Viewer).")
        _set_status(status_path, "error", msg)
        meta["status"] = "error"
        _save_meta(event_dir / "meta.json", meta)
        return

    _set_status(status_path, "indexing", f"Indexing {len(imgs)} photo(s)…")

    if _load_insight():
        from deepface import DeepFace
        to_process  = [p for p in imgs if p.name not in existing]
        cached      = [existing[p.name] for p in imgs if p.name in existing]
        _lock       = _th.Lock()
        new_entries = list(cached)
        done_count  = [len(cached)]

        def _encode_local(img_path):
            try:
                # Use DeepFace to extract face embedding
                result = DeepFace.represent(
                    img_path=str(img_path),
                    model_name=config.FACE_MODEL,
                    detector_backend=config.FACE_DETECTOR,
                    enforce_detection=True  # FIXED: Only match actual faces (no non-face images)
                )
                if not result:
                    return []
                # Extract embedding from first detected face (or image if no face)
                emb = np.array(result[0]["embedding"], dtype=np.float32)
                return [{
                    "path":      str(img_path),
                    "filename":  img_path.name,
                    "embedding": emb,
                }]
            except Exception as e:
                print(f"[WARN] Could not encode {img_path.name}: {e}")
                return []

        with _TPE(max_workers=4) as pool:
            futs = {pool.submit(_encode_local, p): p for p in to_process}
            for fut in _ac(futs):
                entries = fut.result()
                with _lock:
                    new_entries.extend(entries)
                    done_count[0] += 1
                    _set_status(status_path, "indexing",
                                f"Indexing photos… {done_count[0]}/{len(imgs)}")

        with open(str(enc_path), "wb") as f:
            pickle.dump(new_entries, f)

    meta["status"]      = "ready"
    meta["photo_count"] = len(imgs)
    _save_meta(event_dir / "meta.json", meta)
    _set_status(status_path, "ready", f"Ready – {len(imgs)} photos indexed")


def reindex_event_bg(event_id: str):
    """Check Drive for new photos and encode only new ones (no full download needed)."""
    event_dir   = EVENTS / event_id
    status_path = event_dir / "status.json"
    meta        = _load_meta(event_dir / "meta.json")
    try:
        _set_status(status_path, "downloading", "Checking Drive for new photos…")
        build_encodings(event_dir, status_path)
    except Exception as e:
        _set_status(status_path, "error", str(e))
        meta["status"] = "error"
        _save_meta(event_dir / "meta.json", meta)


def process_event_bg(event_id: str):
    """Initial event processing — calls build_encodings() which handles Drive thumbnails."""
    event_dir   = EVENTS / event_id
    status_path = event_dir / "status.json"
    meta        = _load_meta(event_dir / "meta.json")
    try:
        build_encodings(event_dir, status_path)
    except Exception as e:
        _set_status(status_path, "error", str(e))
        meta["status"] = "error"
        _save_meta(event_dir / "meta.json", meta)

def find_matches(selfie_path: Path, images_dir: Path) -> list:
    import pickle, numpy as np
    from scipy.spatial.distance import cosine
    if not _load_insight():
        return []
    try:
        from deepface import DeepFace
        # Extract selfie embedding using DeepFace
        selfie_result = DeepFace.represent(
            img_path=str(selfie_path),
            model_name=config.FACE_MODEL,
            detector_backend=config.FACE_DETECTOR,
            enforce_detection=False
        )
        if not selfie_result:
            return []
        selfie_emb = np.array(selfie_result[0]["embedding"], dtype=np.float32)

        # Load event encodings
        enc_path = images_dir.parent / "face_encodings.pkl"
        if not enc_path.exists():
            return []
        with open(str(enc_path), "rb") as f:
            encodings = pickle.load(f)

        # Use cosine distance with threshold from config
        threshold = config.FACE_THRESHOLD
        matches, seen = [], set()
        for entry in encodings:
            emb = np.array(entry["embedding"], dtype=np.float32)
            # Cosine distance (0=identical, 1=orthogonal, 2=opposite)
            distance = cosine(selfie_emb, emb)
            # Dedup key: file_id (new Drive-based) or path (legacy local)
            dedup_key = entry.get("file_id") or entry.get("path", "")
            if distance <= threshold and dedup_key not in seen:
                seen.add(dedup_key)
                matches.append({
                    "path":     entry.get("path", ""),
                    "file_id":  entry.get("file_id", ""),
                    "filename": entry["filename"],
                    "distance": float(round(distance, 4)),  # Convert numpy float to Python float
                })
        matches.sort(key=lambda x: x["distance"])
        # Return all matching photos (sorted by similarity - lowest distance first)
        return matches
    except Exception as e:
        print(f"[WARN] find_matches error: {e}")
        return []

# ── JSON helpers ──────────────────────────────────────────────────────────────

def _load_meta(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}

def _save_meta(p: Path, d: dict):
    p.write_text(json.dumps(d, indent=2))

def _set_status(p: Path, state: str, msg: str):
    _save_meta(p, {"state": state, "message": msg})

# ══════════════════════════════════════════════════════════════════════════════
#  Auth routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return redirect(url_for("admin_login"))

# ══════════════════════════════════════════════════════════════════════════════
#  Superadmin dedicated login / logout
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/superadmin/login", methods=["GET", "POST"])
def superadmin_login():
    """Dedicated login page for superadmin only."""
    if session.get("admin") and session.get("is_superadmin"):
        return redirect(url_for("superadmin_dashboard"))

    error         = None
    form_username = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        form_username = username

        if (username == config.ADMIN_USERNAME and
                password == config.ADMIN_PASSWORD):
            session.clear()
            session["admin"]         = True
            session["is_superadmin"] = True
            session["username"]      = username
            return redirect(url_for("superadmin_dashboard"))
        else:
            error = "Invalid superadmin credentials."

    return render_template("superadmin_login.html",
                           error=error, form_username=form_username)


@app.route("/superadmin/logout")
def superadmin_logout():
    session.clear()
    return redirect(url_for("superadmin_login"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.args.get("expired"):
        error = "Session ended. Please log in again."

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # ── Registered user check ─────────────────────────────────────────────
        uid, user = get_user_by_username(username)
        if uid and user:
            if check_password_hash(user["password"], password):
                if not user.get("is_active", False):
                    error = "⏳ Account pending activation. Please contact the administrator."
                else:
                    # Clear any force-logout flag on fresh login
                    if user.get("force_logout"):
                        update_user(uid, {"force_logout": False})
                    session.clear()
                    # Superadmin role → redirect to superadmin dashboard
                    if user.get("role") == "superadmin":
                        session["admin"]         = True
                        session["is_superadmin"] = True
                        session["username"]      = username
                        return redirect(url_for("superadmin_dashboard"))
                    # Allow login even if subscription expired; check will happen on action
                    session["admin"]        = True
                    session["is_superadmin"] = False
                    session["user_id"]       = uid
                    session["username"]      = username
                    session["subscription_active"] = is_subscription_active(user)
                    return redirect(url_for("admin_dashboard"))
            else:
                error = "Invalid username or password."
        else:
            if username:
                error = "Invalid username or password."

    return render_template("admin_login.html", error=error)


@app.route("/admin/register", methods=["GET", "POST"])
def admin_register():
    error   = None
    success = None
    form    = {}

    if request.method == "POST":
        form = {
            "username":  request.form.get("username", "").strip(),
            "email":     request.form.get("email", "").strip(),
            "company":   request.form.get("company", "").strip(),
            "phone":     request.form.get("phone", "").strip(),
        }
        password         = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        try:
            sub_days = int(request.form.get("subscription_days", 30))
        except Exception:
            sub_days = 30

        # ── Validation ────────────────────────────────────────────────────────
        if not all([form["username"], password, confirm_password, form["email"], form["company"]]):
            error = "All fields are required."
        elif len(form["username"]) < 3:
            error = "Username must be at least 3 characters."
        elif not re.match(r'^[a-zA-Z0-9_]+$', form["username"]):
            error = "Username can only contain letters, numbers and underscores."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif not re.match(r'^[^@]+@[^@]+\.[^@]+$', form["email"]):
            error = "Please enter a valid email address."
        else:
            uid_exist, _ = get_user_by_username(form["username"])
            if uid_exist or form["username"].lower() == config.ADMIN_USERNAME.lower():
                error = "Username already taken. Please choose another."
            else:
                # Always auto-activate on registration
                # sub_days defaults to 30 if not valid
                if sub_days not in VALID_PLAN_DAYS:
                    sub_days = 30
                new_id  = uuid.uuid4().hex[:12]
                today   = date.today()
                end_dt  = today + timedelta(days=sub_days)
                new_user = {
                    "id":                 new_id,
                    "username":           form["username"],
                    "password":           generate_password_hash(password),
                    "email":              form["email"],
                    "company":            form["company"],
                    "phone":              form["phone"],
                    "subscription_days":  sub_days,
                    "subscription_start": today.isoformat(),
                    "subscription_end":   end_dt.isoformat(),
                    "is_active":          True,
                    "payment_status":     "paid",
                    "created_at":         datetime.now().isoformat(),
                }
                col = _ucol()
                if col is not None:
                    try:
                        col.insert_one({**new_user, "_id": new_id})
                    except Exception:
                        users = load_users()
                        users[new_id] = new_user
                        save_users(users)
                else:
                    users = load_users()
                    users[new_id] = new_user
                    save_users(users)
                success = "✅ Registration successful! Your account is active. You can now log in."
                form = {}

    return render_template("admin_register.html",
                           error=error, success=success,
                           form=form, plans=SUBSCRIPTION_PLANS,
                           payment=load_payment_settings())


@app.route("/payment-qr")
def serve_payment_qr():
    if PAYMENT_QR_FILE.exists():
        return send_file(str(PAYMENT_QR_FILE), mimetype="image/png")
    return "Not found", 404


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ══════════════════════════════════════════════════════════════════════════════
#  Razorpay payment routes
# ══════════════════════════════════════════════════════════════════════════════

def _rzp_client():
    """Return a Razorpay Client or None if not configured."""
    key_id     = getattr(config, "RAZORPAY_KEY_ID",     "")
    key_secret = getattr(config, "RAZORPAY_KEY_SECRET", "")
    if not RAZORPAY_AVAILABLE or not key_id or not key_secret:
        return None
    return _rzp_lib.Client(auth=(key_id, key_secret))


@app.route("/admin/create-payment-order", methods=["POST"])
@_admin_required
def create_payment_order():
    """Create a Razorpay order for subscription renewal."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    try:
        days = int(data.get("days", 30))
    except Exception:
        days = 30

    plan = next((p for p in SUBSCRIPTION_PLANS if p["days"] == days), None)
    if not plan:
        return jsonify({"error": "Invalid plan selected."}), 400

    # Parse price string e.g. "₹999" → 999 → 99900 paise
    price_str   = plan["price"].replace("₹", "").replace(",", "").strip()
    amount_paise = int(float(price_str) * 100)

    client = _rzp_client()
    if client is None:
        return jsonify({"error": "Payment gateway not configured. Contact admin."}), 503

    try:
        order = client.order.create({
            "amount":          amount_paise,
            "currency":        "INR",
            "payment_capture": 1,
            "notes":           {"user_id": uid, "plan_days": str(days)},
        })
        return jsonify({
            "order_id": order["id"],
            "amount":   amount_paise,
            "currency": "INR",
            "plan":     plan,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/verify-payment", methods=["POST"])
@_admin_required
def verify_payment():
    """Verify Razorpay payment signature, extend subscription, store history."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    data               = request.json or {}
    rzp_order_id       = data.get("razorpay_order_id", "")
    rzp_payment_id     = data.get("razorpay_payment_id", "")
    rzp_signature      = data.get("razorpay_signature", "")
    try:
        days = int(data.get("days", 30))
    except Exception:
        days = 30

    # Verify HMAC-SHA256 signature
    key_secret = getattr(config, "RAZORPAY_KEY_SECRET", "")
    msg        = f"{rzp_order_id}|{rzp_payment_id}"
    expected   = hmac.new(key_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if expected != rzp_signature:
        return jsonify({"error": "Payment verification failed. Invalid signature."}), 400

    # Extend subscription
    users = load_users()
    user  = users.get(uid)
    if not user:
        return jsonify({"error": "User not found"}), 404

    end_str = user.get("subscription_end")
    try:
        base = max(date.fromisoformat(end_str), date.today()) if end_str else date.today()
    except Exception:
        base = date.today()

    new_end = base + timedelta(days=days)
    plan    = next((p for p in SUBSCRIPTION_PLANS if p["days"] == days), {})

    sub_fields = {
        "subscription_days":  days,
        "subscription_end":   new_end.isoformat(),
        "is_active":          True,
        "payment_status":     "paid",
    }
    if not user.get("subscription_start"):
        sub_fields["subscription_start"] = date.today().isoformat()
    update_user(uid, sub_fields)

    # Store payment in MongoDB payments collection
    payment_doc = {
        "id":           uuid.uuid4().hex[:12],
        "user_id":      uid,
        "username":     user.get("username", ""),
        "order_id":     rzp_order_id,
        "payment_id":   rzp_payment_id,
        "plan_days":    days,
        "plan_label":   plan.get("label", ""),
        "amount":       plan.get("price", ""),
        "status":       "success",
        "paid_at":      datetime.now().isoformat(),
        "new_sub_end":  new_end.isoformat(),
    }
    db = _get_db()
    if db is not None:
        try:
            db["payments"].insert_one({**payment_doc, "_id": payment_doc["id"]})
        except Exception as e:
            print(f"[WARN] Payment record save: {e}")

    return jsonify({
        "ok":      True,
        "new_end": new_end.isoformat(),
        "days":    days,
        "plan":    plan.get("label", ""),
    })


@app.route("/admin/payment-history")
@_admin_required
def payment_history():
    """Return this user's payment history from MongoDB."""
    uid = session.get("user_id")
    db  = _get_db()
    if db is not None:
        records = list(
            db["payments"].find({"user_id": uid}, {"_id": 0})
                          .sort("paid_at", -1).limit(20)
        )
    else:
        records = []
    return jsonify(records)


# ══════════════════════════════════════════════════════════════════════════════
#  Superadmin routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/superadmin/dashboard")
@_superadmin_required
def superadmin_dashboard():
    users = load_users()
    # Inject computed fields
    for uid, u in users.items():
        u["days_remaining"]  = days_remaining(u)
        u["is_sub_active"]   = is_subscription_active(u)
        u["plan_label"]      = plan_label(u.get("subscription_days", 0))

    # All events
    all_events = []
    if EVENTS.exists():
        for d in sorted(EVENTS.iterdir(), reverse=True):
            if d.is_dir():
                m = _load_meta(d / "meta.json")
                if m:
                    oid = m.get("owner_id", "")
                    owner_user = users.get(oid)
                    m["owner_name"] = owner_user["username"] if owner_user else (
                        "Superadmin" if oid == "superadmin" else oid)
                    all_events.append(m)

    # Per-user event counts and last event date
    event_stats = {}   # uid -> {"count": N, "last": "YYYY-MM-DD"}
    for ev in all_events:
        oid = ev.get("owner_id", "")
        if oid not in event_stats:
            event_stats[oid] = {"count": 0, "last": ""}
        event_stats[oid]["count"] += 1
        ev_date = (ev.get("created_at") or "")[:10]
        if ev_date > event_stats[oid]["last"]:
            event_stats[oid]["last"] = ev_date

    # Inject event stats into each user
    for uid, u in users.items():
        stats = event_stats.get(uid, {"count": 0, "last": "—"})
        u["event_count"] = stats["count"]
        u["last_event"]  = stats["last"] or "—"

    return render_template("superadmin_dashboard.html",
                           users=users, all_events=all_events,
                           payment=load_payment_settings())


@app.route("/superadmin/activate/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_activate(user_id):
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    u        = users[user_id]
    sub_days = u.get("subscription_days", 30)
    today    = date.today()
    end_iso  = (today + timedelta(days=sub_days)).isoformat()
    fields = {
        "is_active":          True,
        "payment_status":     "paid",
        "subscription_start": today.isoformat(),
        "subscription_end":   end_iso,
    }
    update_user(user_id, fields)
    return jsonify({"ok": True, "end": end_iso, "days": sub_days})


@app.route("/superadmin/deactivate/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_deactivate(user_id):
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    update_user(user_id, {"is_active": False})
    return jsonify({"ok": True})


@app.route("/superadmin/set-status/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_set_status(user_id):
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    active = bool((request.json or {}).get("active", False))
    u      = users[user_id]
    fields = {"is_active": active}
    if active:
        if not u.get("subscription_start"):
            today    = date.today()
            sub_days = u.get("subscription_days", 30)
            fields["subscription_start"] = today.isoformat()
            fields["subscription_end"]   = (today + timedelta(days=sub_days)).isoformat()
        fields["payment_status"] = "paid"
    update_user(user_id, fields)
    return jsonify({"ok": True})


@app.route("/superadmin/extend/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_extend(user_id):
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    try:
        extra = int((request.json or {}).get("days", 30))
    except Exception:
        extra = 30
    u       = users[user_id]
    end_str = u.get("subscription_end")
    try:
        base = max(date.fromisoformat(end_str), date.today()) if end_str else date.today()
    except Exception:
        base = date.today()
    new_end = base + timedelta(days=extra)
    fields  = {
        "subscription_end":   new_end.isoformat(),
        "is_active":          True,
        "payment_status":     "paid",
    }
    if not u.get("subscription_start"):
        fields["subscription_start"] = date.today().isoformat()
    update_user(user_id, fields)
    return jsonify({"ok": True, "new_end": new_end.isoformat()})


@app.route("/superadmin/delete-user/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_delete_user(user_id):
    delete_user(user_id)
    return jsonify({"ok": True})


@app.route("/superadmin/force-logout/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_force_logout(user_id):
    """Set force_logout flag — user is kicked out on their next request."""
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    update_user(user_id, {"force_logout": True})
    return jsonify({"ok": True})


@app.route("/superadmin/create-user", methods=["POST"])
@_superadmin_required
def superadmin_create_user():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    company  = (data.get("company")  or "").strip()
    email    = (data.get("email")    or "").strip()
    phone    = (data.get("phone")    or "").strip()
    password = (data.get("password") or "").strip()
    try:
        sub_days = int(data.get("sub_days", 30))
    except Exception:
        sub_days = 30
    if sub_days not in VALID_PLAN_DAYS:
        sub_days = 30

    # Validation
    if not all([username, company, email, password]):
        return jsonify({"error": "All required fields must be filled."}), 400
    if len(username) < 3 or not re.match(r'^[a-zA-Z0-9_]+$', username):
        return jsonify({"error": "Invalid username."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    uid_exist, _ = get_user_by_username(username)
    if uid_exist or username.lower() == config.ADMIN_USERNAME.lower():
        return jsonify({"error": "Username already taken."}), 400

    # Always auto-activate on creation
    new_id = uuid.uuid4().hex[:12]
    today  = date.today()
    new_user = {
        "id":                 new_id,
        "username":           username,
        "password":           generate_password_hash(password),
        "email":              email,
        "company":            company,
        "phone":              phone,
        "subscription_days":  sub_days,
        "subscription_start": today.isoformat(),
        "subscription_end":   (today + timedelta(days=sub_days)).isoformat(),
        "is_active":          True,
        "payment_status":     "paid",
        "created_at":         datetime.now().isoformat(),
    }
    col = _ucol()
    if col is not None:
        try:
            col.insert_one({**new_user, "_id": new_id})
        except Exception:
            users = load_users()
            users[new_id] = new_user
            save_users(users)
    else:
        users = load_users()
        users[new_id] = new_user
        save_users(users)
    return jsonify({"ok": True, "id": new_id})


@app.route("/superadmin/generate-payment-qr", methods=["POST"])
@_superadmin_required
def superadmin_generate_payment_qr():
    """Generate a UPI payment QR code from a UPI ID and save it."""
    data     = request.json or {}
    upi_id   = (data.get("upi_id")   or "").strip()
    upi_name = (data.get("upi_name") or "").strip()
    if not upi_id:
        return jsonify({"error": "UPI ID is required"}), 400
    # Build standard UPI payment URL
    import urllib.parse
    params = {"pa": upi_id, "cu": "INR"}
    if upi_name:
        params["pn"] = upi_name
    upi_url = "upi://pay?" + urllib.parse.urlencode(params)
    try:
        qr = qrcode.QRCode(
            version=1, box_size=10, border=4,
            error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1e293b", back_color="white")
        DATA.mkdir(parents=True, exist_ok=True)
        img.save(str(PAYMENT_QR_FILE))
        # Persist upi_id + upi_name into settings too
        settings = load_payment_settings()
        settings["upi_id"]   = upi_id
        settings["upi_name"] = upi_name
        settings["has_qr"]   = True
        save_payment_settings(settings)
        return jsonify({"ok": True, "upi_url": upi_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/superadmin/payment-settings", methods=["POST"])
@_superadmin_required
def superadmin_payment_settings():
    settings = load_payment_settings()
    for field in ["upi_id", "upi_name", "bank_name", "account_name",
                  "account_number", "ifsc_code", "branch", "amount_note"]:
        val = request.form.get(field, "").strip()
        settings[field] = val
    # Handle QR image upload
    qr_file = request.files.get("payment_qr")
    if qr_file and qr_file.filename:
        try:
            img = Image.open(qr_file.stream).convert("RGBA")
            img.save(str(PAYMENT_QR_FILE), "PNG")
        except Exception as e:
            return jsonify({"error": f"QR image error: {e}"}), 400
    # Remove QR if requested
    if request.form.get("remove_qr") == "1" and PAYMENT_QR_FILE.exists():
        PAYMENT_QR_FILE.unlink()
    settings["has_qr"] = PAYMENT_QR_FILE.exists()
    save_payment_settings(settings)
    return jsonify({"ok": True})


@app.route("/superadmin/update-plan/<user_id>", methods=["POST"])
@_superadmin_required
def superadmin_update_plan(user_id):
    users = load_users()
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404
    try:
        days = int((request.json or {}).get("days", 30))
    except Exception:
        return jsonify({"error": "Invalid days"}), 400
    if days not in VALID_PLAN_DAYS:
        return jsonify({"error": "Invalid plan"}), 400
    update_user(user_id, {"subscription_days": days})
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  Admin (regular user) routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/dashboard")
@_admin_required
def admin_dashboard():
    uid          = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)

    events = []
    if EVENTS.exists():
        for d in sorted(EVENTS.iterdir(), reverse=True):
            if d.is_dir():
                m = _load_meta(d / "meta.json")
                if m:
                    # Each user sees only their own events
                    if is_superadmin or m.get("owner_id") == uid:
                        events.append(m)

    user_info = None
    if not is_superadmin and uid:
        u = load_users().get(uid)
        if u:
            user_info = {**u,
                         "days_remaining": days_remaining(u),
                         "plan_label":     plan_label(u.get("subscription_days", 0))}

    # Pass Razorpay public key to template
    rzp_key = getattr(config, "RAZORPAY_KEY_ID", "")
    sub_active = True
    if user_info and uid:
        # Check subscription from MongoDB first (updated by payment), fallback to JSON
        db = _get_db()
        if db is not None:
            user_from_db = db["users"].find_one({"id": uid})
            if user_from_db:
                sub_active = is_subscription_active(user_from_db)
            else:
                sub_active = is_subscription_active(load_users().get(uid, {}))
        else:
            sub_active = is_subscription_active(load_users().get(uid, {}))

    return render_template("admin_dashboard.html",
                           events=events,
                           user_info=user_info,
                           is_superadmin=is_superadmin,
                           rzp_key=rzp_key,
                           sub_active=sub_active,
                           plans=SUBSCRIPTION_PLANS)


def _make_qr(event_id: str, user_url: str):
    qr = qrcode.QRCode(version=1, box_size=10, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(user_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1e293b", back_color="white")
    img.save(str(QRS / f"{event_id}.png"))


@app.route("/admin/submit", methods=["POST"])
@_admin_required
def admin_submit():
    # Block if subscription expired (superadmin bypasses)
    uid = session.get("user_id")
    if not session.get("is_superadmin") and uid:
        user = load_users().get(uid)
        if not user or not is_subscription_active(user):
            return jsonify({"error": "subscription_expired",
                            "message": "Your subscription has expired. Please renew to generate QR codes."}), 403

    event_name   = request.form.get("event_name", "Event").strip()
    drive_link   = request.form.get("drive_link", "").strip()
    upload_mode  = request.form.get("upload_mode", "drive")
    uploaded_files = request.files.getlist("photos")

    # Date range fields
    from_date    = request.form.get("from_date", "").strip()
    to_date      = request.form.get("to_date", "").strip()

    event_id   = uuid.uuid4().hex[:10]
    event_dir  = EVENTS / event_id
    images_dir = event_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    scheme   = request.headers.get("X-Forwarded-Proto", request.scheme)
    user_url = f"{scheme}://{request.host}/event/{event_id}"

    meta = {
        "id":          event_id,
        "name":        event_name,
        "drive_link":  drive_link,
        "folder_id":   get_folder_id(drive_link) if drive_link else None,
        "folder_name": drive_link.split("/folders/")[-1].split("?")[0] if drive_link else None,
        "user_url":    user_url,
        "status":      "processing",
        "photo_count": 0,
        "upload_mode": upload_mode,
        "owner_id":    session.get("user_id") or "superadmin",
        "from_date":   from_date if from_date else None,
        "to_date":     to_date if to_date else None,
        "created_at":  datetime.now().isoformat(),
    }
    _save_meta(event_dir / "meta.json", meta)
    _make_qr(event_id, user_url)

    if upload_mode == "files" and uploaded_files:
        _set_status(event_dir / "status.json", "saving", "Saving uploaded photos…")
        saved = 0
        for f in uploaded_files:
            if f and f.filename:
                ext = Path(f.filename).suffix.lower()
                if ext in IMG_EXTS:
                    try:
                        # Save original bytes directly — no PIL re-encoding,
                        # so the photo stays at 100 % of its original quality.
                        raw = f.read()
                        out = images_dir / f"{uuid.uuid4().hex[:8]}{ext}"
                        out.write_bytes(raw)
                        saved += 1
                    except Exception:
                        pass
        if saved == 0:
            _set_status(event_dir / "status.json", "error", "No valid images found")
            meta["status"] = "error"
            _save_meta(event_dir / "meta.json", meta)
        else:
            t = threading.Thread(target=build_encodings,
                                 args=(event_dir, event_dir / "status.json"), daemon=True)
            t.start()
    else:
        if not meta["folder_id"]:
            return jsonify({"error": "Invalid Google Drive folder URL"}), 400
        _set_status(event_dir / "status.json", "queued", "Queued for processing…")
        t = threading.Thread(target=process_event_bg, args=(event_id,), daemon=True)
        t.start()

    return jsonify({"event_id": event_id, "qr_url": f"/qr/{event_id}",
                    "user_url": user_url, "status": "processing"})


@app.route("/admin/status/<event_id>")
@_admin_required
def admin_status(event_id):
    st = _load_meta(EVENTS / event_id / "status.json")
    mt = _load_meta(EVENTS / event_id / "meta.json")
    return jsonify({**st, "photo_count": mt.get("photo_count", 0),
                    "overall": mt.get("status", "unknown")})


@app.route("/admin/events-by-date", methods=["POST"])
@_admin_required
def get_events_by_date():
    """Query events by date range and user."""
    data = request.get_json() or {}
    from_date = data.get("from_date", "")
    to_date = data.get("to_date", "")
    user_id = session.get("user_id") or "superadmin"

    # Load all events from local storage
    events = []
    if EVENTS.exists():
        for event_dir in EVENTS.iterdir():
            if not event_dir.is_dir():
                continue
            meta = _load_meta(event_dir / "meta.json")
            if not meta:
                continue

            # Filter by owner
            if meta.get("owner_id") != user_id and not session.get("is_superadmin"):
                continue

            # Filter by date range if provided
            if from_date and meta.get("from_date"):
                if meta.get("from_date") < from_date:
                    continue
            if to_date and meta.get("to_date"):
                if meta.get("to_date") > to_date:
                    continue

            events.append({
                "id": meta.get("id"),
                "name": meta.get("name"),
                "from_date": meta.get("from_date"),
                "to_date": meta.get("to_date"),
                "folder_name": meta.get("folder_name"),
                "photo_count": meta.get("photo_count", 0),
                "status": meta.get("status"),
                "created_at": meta.get("created_at"),
            })

    return jsonify({"events": events, "total": len(events)})


@app.route("/admin/add-photos/<event_id>", methods=["POST"])
@_admin_required
def admin_add_photos(event_id):
    event_dir  = EVENTS / event_id
    images_dir = event_dir / "images"
    images_dir.mkdir(exist_ok=True)
    files = request.files.getlist("photos")
    saved = 0
    for f in files:
        if f and f.filename:
            ext = Path(f.filename).suffix.lower()
            if ext in IMG_EXTS:
                try:
                    raw = f.read()
                    out = images_dir / f"{uuid.uuid4().hex[:8]}{ext}"
                    out.write_bytes(raw)
                    saved += 1
                except Exception:
                    pass
    if saved:
        for pkl in images_dir.glob("*.pkl"):
            pkl.unlink()
        t = threading.Thread(target=build_encodings,
                             args=(event_dir, event_dir / "status.json"), daemon=True)
        t.start()
        return jsonify({"ok": True, "saved": saved})
    return jsonify({"error": "No valid images"}), 400


def save_browse_history(folder_id, admin_id, files_list):
    """Save photo browse history to MongoDB."""
    try:
        col = _pcol()
        if col is None:
            return  # Fallback to JSON only

        doc = {
            "folder_id": folder_id,
            "admin_id": admin_id,
            "file_count": len(files_list),
            "files": [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "thumb": f.get("thumb"),
                    "full": f.get("full")
                }
                for f in files_list
            ],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        # Insert or update
        col.update_one(
            {"folder_id": folder_id, "admin_id": admin_id},
            {"$set": doc},
            upsert=True
        )
        print(f"[INFO] Saved {len(files_list)} photos to event_photos collection")
    except Exception as e:
        print(f"[WARN] Failed to save browse history: {e}")


@app.route("/admin/browse-drive", methods=["POST"])
@_admin_required
def admin_browse_drive():
    """Return the list of images in a public Drive folder (for the Browse tab)."""
    data       = request.get_json(force=True)
    drive_link = (data.get("drive_link") or "").strip()
    force      = bool(data.get("force"))          # True → bypass cache
    folder_id  = get_folder_id(drive_link)
    if not folder_id:
        return jsonify({"error": "Invalid Drive link — must contain /folders/"}), 400
    if force:
        _drive_cache.pop(folder_id, None)         # clear cache → fresh fetch
    files = _cached_drive_images(folder_id)
    result = [
        {"id": f["id"], "name": f["name"],
         "thumb": f"https://drive.google.com/thumbnail?id={f['id']}&sz=w800",
         "full":  f"https://lh3.googleusercontent.com/d/{f['id']}"}
        for f in files
    ]

    # Save to MongoDB
    admin_id = session.get("user_id") or "superadmin"
    save_browse_history(folder_id, admin_id, result)

    return jsonify({"files": result, "total": len(result), "folder_id": folder_id})


@app.route("/admin/browse-history", methods=["GET"])
@_admin_required
def get_browse_history():
    """Get browse history for current admin."""
    admin_id = session.get("user_id") or "superadmin"
    col = _pcol()

    if col is None:
        return jsonify({"history": []})

    try:
        records = list(col.find(
            {"admin_id": admin_id},
            {"files": 0}  # Exclude large files array for summary view
        ).sort("updated_at", -1).limit(50))

        for r in records:
            r["_id"] = str(r.get("_id", ""))

        return jsonify({
            "history": records,
            "total": len(records)
        })
    except Exception as e:
        print(f"[WARN] Failed to get browse history: {e}")
        return jsonify({"history": [], "error": str(e)})


@app.route("/admin/browse-history/<folder_id>", methods=["GET"])
@_admin_required
def get_browse_history_detail(folder_id):
    """Get detailed browse history for a specific folder."""
    admin_id = session.get("user_id") or "superadmin"
    col = _pcol()

    if col is None:
        return jsonify({"history": None})

    try:
        record = col.find_one({
            "folder_id": folder_id,
            "admin_id": admin_id
        })

        if not record:
            return jsonify({"history": None, "error": "Not found"}), 404

        record["_id"] = str(record.get("_id", ""))
        return jsonify({"history": record})
    except Exception as e:
        print(f"[WARN] Failed to get browse history detail: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/reindex/<event_id>", methods=["POST"])
@_admin_required
def admin_reindex(event_id):
    """Re-download only NEW photos from Drive and encode only new ones."""
    event_dir  = EVENTS / event_id
    meta       = _load_meta(event_dir / "meta.json")
    folder_id  = meta.get("folder_id")
    if not folder_id:
        return jsonify({"error": "No Drive folder linked to this event"}), 400
    images_dir = event_dir / "images"
    images_dir.mkdir(exist_ok=True)
    status_path = event_dir / "status.json"
    meta["status"] = "processing"
    _save_meta(event_dir / "meta.json", meta)
    # Do NOT delete .pkl files — DeepFace will only encode new photos
    t = threading.Thread(target=reindex_event_bg, args=(event_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Re-indexing started"})


@app.route("/admin/delete/<event_id>", methods=["POST"])
@_admin_required
def admin_delete(event_id):
    ed = EVENTS / event_id
    if ed.exists():
        shutil.rmtree(ed)
    qr = QRS / f"{event_id}.png"
    if qr.exists():
        qr.unlink()
    return jsonify({"ok": True})


# ── QR ─────────────────────────────────────────────────────────────────────────

@app.route("/qr/<event_id>")
def serve_qr(event_id):
    p = QRS / f"{event_id}.png"
    if not p.exists():
        return "QR not found", 404
    return send_file(str(p), mimetype="image/png")


@app.route("/qr/<event_id>/download")
def download_qr(event_id):
    p = QRS / f"{event_id}.png"
    if not p.exists():
        return "QR not found", 404
    meta_path = EVENTS / event_id / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    name = meta.get("name", event_id)
    safe = re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") or event_id
    return send_file(str(p), mimetype="image/png",
                     as_attachment=True, download_name=f"QR_{safe}.png")


# ══════════════════════════════════════════════════════════════════════════════
#  User (public) routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/event/<event_id>")
def user_page(event_id):
    meta = _load_meta(EVENTS / event_id / "meta.json")
    if not meta:
        return render_template("404.html"), 404
    return render_template("user.html", event=meta)


@app.route("/event/<event_id>/status")
def event_status(event_id):
    st = _load_meta(EVENTS / event_id / "status.json")
    mt = _load_meta(EVENTS / event_id / "meta.json")
    return jsonify({**st, "overall": mt.get("status", "unknown"),
                    "photo_count": mt.get("photo_count", 0)})


@app.route("/event/<event_id>/upload", methods=["POST"])
def upload_selfie(event_id):
    photo = request.files.get("photo")
    if not photo:
        return jsonify({"error": "No photo uploaded"}), 400

    event_dir  = EVENTS / event_id
    images_dir = event_dir / "images"
    meta       = _load_meta(event_dir / "meta.json")

    if not meta:
        return jsonify({"error": "Event not found"}), 404
    ev_status = meta.get("status")
    if ev_status == "error":
        st_msg = _load_meta(event_dir / "status.json").get("message", "Event processing failed.")
        return jsonify({"error": f"⚠️ {st_msg}"}), 503
    # Allow search if ready OR downloading (auto-sync) — existing encodings still valid
    enc_exists = (event_dir / "face_encodings.pkl").exists()
    if ev_status not in ("ready", "downloading") or (ev_status != "ready" and not enc_exists):
        return jsonify({"error": "Event photos are still processing. Please try again shortly."}), 503

    sid         = uuid.uuid4().hex[:10]
    selfie_path = UPS / f"{sid}.jpg"
    try:
        img = Image.open(photo.stream).convert("RGB")
        img.save(str(selfie_path), "JPEG", quality=90)
    except Exception as e:
        return jsonify({"error": f"Could not read image: {e}"}), 400

    # Validate that uploaded image is readable
    try:
        import cv2

        img_cv = cv2.imread(str(selfie_path))
        if img_cv is None:
            selfie_path.unlink(missing_ok=True)
            return jsonify({"error": "❌ Could not read image. Please try again."}), 400

        h, w = img_cv.shape[:2]
        if h < 50 or w < 50:
            selfie_path.unlink(missing_ok=True)
            return jsonify({"error": "❌ Image too small. Please upload a larger photo."}), 400
    except Exception as e:
        selfie_path.unlink(missing_ok=True)
        return jsonify({"error": f"❌ Error validating image: {e}"}), 400

    # Find matching photos
    matches = find_matches(selfie_path, images_dir)

    if not matches:
        selfie_path.unlink(missing_ok=True)
        return jsonify({"error": "No photos in this event."}), 404

    res_dir = RES / sid
    res_dir.mkdir(parents=True, exist_ok=True)
    _save_meta(res_dir / "matches.json", {"event_id": event_id, "matches": matches})

    return jsonify({
        "session_id":  sid,
        "match_count": len(matches),
        "photos": [{"url": f"/photo/{sid}/{i}", "filename": m["filename"]}
                   for i, m in enumerate(matches)],
    })


@app.route("/photo/<sid>/<int:idx>")
def serve_photo(sid, idx):
    """Serve matched photo — redirect to Drive (new) or serve local file (legacy)."""
    data = _load_meta(RES / sid / "matches.json")
    if not data or idx >= len(data["matches"]):
        return "Not found", 404
    m   = data["matches"][idx]
    fid = m.get("file_id")
    if fid:
        # Serve directly from Google Drive (original quality, no server storage)
        return redirect(f"https://drive.google.com/uc?export=download&id={fid}")
    # Legacy: local file
    p = Path(m.get("path", ""))
    if p.exists():
        return send_file(str(p))
    return "Photo not found", 404


@app.route("/photo/<sid>/<int:idx>/thumb")
def serve_photo_thumb(sid, idx):
    """Serve thumbnail — redirect to Drive thumbnail (new) or resize local file (legacy)."""
    data = _load_meta(RES / sid / "matches.json")
    if not data or idx >= len(data["matches"]):
        return "Not found", 404
    m   = data["matches"][idx]
    fid = m.get("file_id")
    if fid:
        # Drive thumbnail URL — fast, no server load
        return redirect(f"https://drive.google.com/thumbnail?id={fid}&sz=w400")
    # Legacy: local thumbnail
    p = Path(m.get("path", ""))
    if not p.exists():
        return "Photo not found", 404
    try:
        img = Image.open(str(p)).convert("RGB")
        img.thumbnail((400, 400), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75, optimize=True)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception:
        return send_file(str(p))


@app.route("/download/<sid>")
def download_zip(sid):
    """Stream matched photos as ZIP. Fetches from Drive (new) or local files (legacy)."""
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return "Session not found", 404
    matches = data.get("matches", [])
    if not matches:
        return "No photos to download", 404

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    tmp_path   = Path(tmp.name)
    seen_names: set = set()
    try:
        with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_STORED) as zf:
            for m in matches:
                fname = m["filename"]
                if fname in seen_names:
                    fname = f"{Path(fname).stem}_{uuid.uuid4().hex[:4]}{Path(fname).suffix}"
                seen_names.add(fname)

                fid = m.get("file_id")
                if fid:
                    # Download from Drive on-the-fly
                    try:
                        r = requests.get(
                            f"https://drive.google.com/uc?export=download&id={fid}",
                            timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                            zf.writestr(fname, r.content)
                            continue
                    except Exception:
                        pass
                # Legacy: local file
                p = Path(m.get("path", ""))
                if p.exists():
                    zf.write(str(p), fname)

        zip_size = tmp_path.stat().st_size

        def _generate():
            try:
                with open(str(tmp_path), "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try: tmp_path.unlink(missing_ok=True)
                except Exception: pass

        return Response(
            stream_with_context(_generate()),
            mimetype="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="my_photos.zip"',
                "Content-Length":      str(zip_size),
                "Cache-Control":       "no-store",
            }
        )
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500


@app.route("/photo/<sid>/info")
def photo_info(sid):
    """Return info about matched photos for display in UI."""
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return jsonify([])
    info = []
    for i, m in enumerate(data.get("matches", [])):
        p       = Path(m.get("path", ""))
        size_kb = round(p.stat().st_size / 1024, 1) if p.exists() else 0
        info.append({"idx": i, "filename": m["filename"], "size_kb": size_kb,
                     "from_drive": bool(m.get("file_id"))})
    return jsonify(info)


@app.route("/send-email", methods=["POST"])
def send_email():
    body  = request.json or {}
    email = (body.get("email") or "").strip()
    sid   = body.get("session_id", "")
    if not email:
        return jsonify({"error": "Email address required"}), 400
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return jsonify({"error": "Session not found"}), 404
    buf = io.BytesIO()
    seen_names: set = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in data["matches"]:
            fname = m["filename"]
            if fname in seen_names:
                fname = f"{Path(fname).stem}_{uuid.uuid4().hex[:4]}{Path(fname).suffix}"
            seen_names.add(fname)
            fid = m.get("file_id")
            if fid:
                try:
                    r = requests.get(
                        f"https://drive.google.com/uc?export=download&id={fid}",
                        timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                        zf.writestr(fname, r.content)
                        continue
                except Exception:
                    pass
            p = Path(m.get("path", ""))
            if p.exists():
                zf.write(str(p), fname)
    buf.seek(0)
    try:
        msg            = MIMEMultipart()
        msg["From"]    = config.EMAIL_SENDER
        msg["To"]      = email
        msg["Subject"] = "Your Photos 📸"
        msg.attach(MIMEText(
            f"Hi there!\n\nWe found {len(data['matches'])} photo(s) for you.\n"
            "They are attached as a ZIP file.\n\nEnjoy! 🎉", "plain"))
        att = MIMEBase("application", "octet-stream")
        att.set_payload(buf.read())
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="my_photos.zip")
        msg.attach(att)
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as srv:
            srv.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            srv.send_message(msg)
        return jsonify({"ok": True, "message": f"Photos sent to {email}"})
    except Exception as e:
        return jsonify({"error": f"Email failed: {e}"}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  History & Photo Browsing Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/events-history")
@_admin_required
def events_history():
    """Return all events with metadata for history panel."""
    uid = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)
    events = []
    if EVENTS.exists():
        for event_dir in sorted(EVENTS.iterdir(), reverse=True):
            if not event_dir.is_dir():
                continue
            meta = _load_meta(event_dir / "meta.json")
            status = _load_meta(event_dir / "status.json")
            # Filter: superadmin sees all, regular users see only their own
            if is_superadmin or meta.get("owner_id") == uid:
                events.append({
                    "id": event_dir.name,
                    "name": meta.get("name", "Unnamed"),
                    "folder_link": meta.get("folder_link", ""),
                    "photo_count": meta.get("photo_count", 0),
                    "status": meta.get("status", "unknown"),
                    "created_at": meta.get("created_at", ""),
                    "current_status": status.get("state", "unknown"),
                    "current_msg": status.get("message", ""),
                })
    return jsonify({"events": events})


@app.route("/admin/event/<event_id>/photos")
@_admin_required
def event_photos(event_id):
    """Return photos from an event (file IDs + names)."""
    # Verify ownership before returning photos
    uid = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)

    event_dir = EVENTS / event_id
    if not event_dir.exists():
        return jsonify({"error": "Event not found"}), 404

    # Check ownership
    meta = _load_meta(event_dir / "meta.json")
    if not is_superadmin and meta.get("owner_id") != uid:
        return jsonify({"error": "Unauthorized"}), 403

    enc_path = event_dir / "face_encodings.pkl"
    photos = []

    if enc_path.exists():
        try:
            import pickle
            with open(str(enc_path), "rb") as f:
                entries = pickle.load(f)
                for idx, entry in enumerate(entries):
                    photos.append({
                        "idx": idx,
                        "filename": entry.get("filename", f"photo_{idx}"),
                        "file_id": entry.get("file_id", ""),
                        "has_face": entry.get("embedding") is not None,
                    })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"photos": photos, "count": len(photos)})


def _load_pickle_cached(enc_path):
    """Load pickle file with in-memory caching to avoid repeated disk reads."""
    global _pickle_cache
    event_id = enc_path.parent.name

    if not enc_path.exists():
        return None

    try:
        mtime = enc_path.stat().st_mtime
        if event_id in _pickle_cache:
            cached_mtime, entries = _pickle_cache[event_id]
            if cached_mtime == mtime:
                return entries
    except Exception:
        pass

    try:
        import pickle
        with open(str(enc_path), "rb") as f:
            entries = pickle.load(f)
            mtime = enc_path.stat().st_mtime
            _pickle_cache[event_id] = (mtime, entries)
            return entries
    except Exception:
        return None


@app.route("/admin/event/<event_id>/photo/<int:idx>")
@_admin_required
def event_photo_thumb(event_id, idx):
    """Serve photo thumbnail from event."""
    # Verify ownership before serving photo
    uid = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)

    event_dir = EVENTS / event_id
    if not event_dir.exists():
        return "Not found", 404

    # Check ownership
    meta = _load_meta(event_dir / "meta.json")
    if not is_superadmin and meta.get("owner_id") != uid:
        return "Unauthorized", 403

    enc_path = event_dir / "face_encodings.pkl"
    if not enc_path.exists():
        return "Not found", 404

    try:
        entries = _load_pickle_cached(enc_path)
        if not entries:
            return "Error loading photos", 500

        if idx < 0 or idx >= len(entries):
            return "Not found", 404
        entry = entries[idx]
        file_id = entry.get("file_id")
        if not file_id:
            return "Not found", 404

        # Download thumbnail from Drive
        import tempfile as _tf
        tmp = Path(_tf.mktemp(suffix=".jpg"))
        if _download_drive_file(file_id, tmp):
            with open(str(tmp), "rb") as f:
                data = f.read()
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            response = send_file(io.BytesIO(data), mimetype="image/jpeg")
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response
    except Exception:
        pass

    return "Error loading photo", 500


@app.route("/admin/event/<event_id>/delete", methods=["POST"])
@_admin_required
def delete_event(event_id):
    """Delete an event and all its data."""
    # Verify ownership before deleting
    uid = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)

    event_dir = EVENTS / event_id
    if not event_dir.exists():
        return jsonify({"error": "Event not found"}), 404

    # Check ownership
    meta = _load_meta(event_dir / "meta.json")
    if not is_superadmin and meta.get("owner_id") != uid:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        import shutil
        shutil.rmtree(str(event_dir))
        qr_file = QRS / f"{event_id}.png"
        if qr_file.exists():
            qr_file.unlink()
        return jsonify({"ok": True, "message": f"Event '{event_id}' deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/event/<event_id>/export-zip", methods=["GET"])
@_admin_required
def export_event_zip(event_id):
    """Export all event photos as ZIP."""
    event_dir = EVENTS / event_id
    if not event_dir.exists():
        return "Not found", 404

    enc_path = event_dir / "face_encodings.pkl"
    if not enc_path.exists():
        return "Not found", 404

    try:
        import pickle
        with open(str(enc_path), "rb") as f:
            entries = pickle.load(f)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, entry in enumerate(entries):
                file_id = entry.get("file_id")
                fname = entry.get("filename", f"photo_{idx}.jpg")
                if file_id:
                    try:
                        import tempfile as _tf
                        tmp = Path(_tf.mktemp(suffix=".jpg"))
                        if _download_drive_file(file_id, tmp):
                            zf.write(str(tmp), arcname=fname)
                            tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

        buf.seek(0)
        return send_file(buf, mimetype="application/zip",
                        as_attachment=True, download_name=f"{event_id}_photos.zip")
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/admin/events-history/search")
@_admin_required
def search_events():
    """Search events by name."""
    uid = session.get("user_id")
    is_superadmin = session.get("is_superadmin", False)
    query = request.args.get("q", "").lower()
    events = []

    if EVENTS.exists():
        for event_dir in sorted(EVENTS.iterdir(), reverse=True):
            if not event_dir.is_dir():
                continue
            meta = _load_meta(event_dir / "meta.json")
            status = _load_meta(event_dir / "status.json")
            name = meta.get("name", "").lower()

            # Filter: superadmin sees all, regular users see only their own
            if (query in name or not query) and (is_superadmin or meta.get("owner_id") == uid):
                events.append({
                    "id": event_dir.name,
                    "name": meta.get("name", "Unnamed"),
                    "folder_link": meta.get("folder_link", ""),
                    "photo_count": meta.get("photo_count", 0),
                    "status": meta.get("status", "unknown"),
                    "created_at": meta.get("created_at", ""),
                    "current_status": status.get("state", "unknown"),
                    "current_msg": status.get("message", ""),
                })

    return jsonify({"events": events, "count": len(events)})


@app.route("/admin/event/<event_id>/view-history", methods=["POST"])
@_admin_required
def log_event_view(event_id):
    """Log event view in MongoDB for analytics."""
    uid = session.get("user_id", "unknown")
    col = _get_db()
    if col is None:
        return jsonify({"ok": True}), 200  # Silent fail if no MongoDB

    try:
        col_history = col.db["event_views"] if hasattr(col, 'db') else _get_db()["event_views"]
        col_history.insert_one({
            "user_id": uid,
            "event_id": event_id,
            "viewed_at": datetime.utcnow().isoformat(),
            "ip": request.remote_addr,
        })
    except Exception:
        pass  # Silently ignore logging errors

    return jsonify({"ok": True})


@app.route("/admin/event-analytics/<event_id>")
@_admin_required
def get_event_analytics(event_id):
    """Get analytics for an event (views, popular photos, etc.)."""
    col = _get_db()
    if col is None:
        return jsonify({"views": 0, "unique_viewers": 0, "last_viewed": None})

    try:
        col_history = col["event_views"]
        views = col_history.count_documents({"event_id": event_id})
        unique = col_history.distinct("user_id", {"event_id": event_id})
        last = col_history.find_one(
            {"event_id": event_id},
            sort=[("viewed_at", -1)]
        )
        return jsonify({
            "views": views,
            "unique_viewers": len(unique),
            "last_viewed": last.get("viewed_at") if last else None,
        })
    except Exception:
        return jsonify({"views": 0, "unique_viewers": 0, "last_viewed": None})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ── SUBSCRIPTIONS – UPI QR CODE PAYMENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/api/subscription/initiate", methods=["POST"])
def subscription_initiate():
    """Initiate subscription payment - generate QR code."""
    try:
        data = request.get_json()
        plan_id = data.get("plan_id")
        user_id = session.get("user_id")

        if not plan_id or plan_id not in config.QUICK_SUBSCRIPTION_PLANS:
            return jsonify({"error": "Invalid plan"}), 400

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        from payment_utils import generate_subscription_qr
        db = _get_db()

        sub_id, qr_path, upi_str, expires_at = generate_subscription_qr(
            user_id, plan_id, db
        )

        if not sub_id:
            return jsonify({"error": "Failed to generate subscription"}), 500

        # Convert QR image to base64
        qr_full_path = SUBS / (qr_path.split('/')[-1])
        with open(qr_full_path, "rb") as f:
            import base64
            qr_base64 = base64.b64encode(f.read()).decode("utf-8")

        plan_info = config.QUICK_SUBSCRIPTION_PLANS[plan_id]

        return jsonify({
            "success": True,
            "subscription_id": sub_id,
            "qr_code_base64": qr_base64,
            "qr_code_path": qr_path,
            "upi_string": upi_str,
            "plan_id": plan_id,
            "amount": plan_info["amount"],
            "label": plan_info["label"],
            "days": plan_info["days"],
            "expires_at": expires_at.isoformat()
        })
    except Exception as e:
        print(f"[ERROR] subscription_initiate: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/subscription/verify", methods=["POST"])
def subscription_verify():
    """Verify subscription payment and activate access."""
    try:
        data = request.get_json()
        subscription_id = data.get("subscription_id")
        transaction_id = data.get("transaction_id", "manual_confirm")
        user_id = session.get("user_id")

        print(f"[PAYMENT] Verify called: sub_id={subscription_id}, txn_id={transaction_id}, user_id={user_id}")

        if not subscription_id:
            return jsonify({"error": "Missing subscription_id"}), 400

        from payment_utils import verify_subscription
        db = _get_db()

        # Check if subscription exists
        sub_record = db["subscriptions"].find_one({"subscription_id": subscription_id})
        print(f"[PAYMENT] Found subscription: {sub_record is not None}")
        if sub_record:
            print(f"[PAYMENT] Subscription user_id: {sub_record.get('user_id')}, status: {sub_record.get('status')}")

        success = verify_subscription(subscription_id, transaction_id, db)
        print(f"[PAYMENT] Verification success: {success}")

        if success:
            sub = db["subscriptions"].find_one({"subscription_id": subscription_id})
            print(f"[PAYMENT] Updated subscription: {sub}")

            # Check user was updated
            user = db["users"].find_one({"id": user_id}) if user_id else None
            print(f"[PAYMENT] User subscription_end: {user.get('subscription_end') if user else 'N/A'}")

            return jsonify({
                "success": True,
                "message": "Subscription activated",
                "expires_at": sub["expires_at"].isoformat()
            })
        else:
            return jsonify({"error": "Subscription not found or already verified"}), 404
    except Exception as e:
        import traceback
        print(f"[ERROR] subscription_verify: {e}")
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/subscription/history", methods=["GET"])
def subscription_history_api():
    """Get subscription history for logged-in user."""
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        from payment_utils import get_user_subscriptions
        db = _get_db()

        subs = get_user_subscriptions(user_id, db)

        # Convert ObjectId to string for JSON serialization
        for sub in subs:
            if "_id" in sub:
                sub["_id"] = str(sub["_id"])
            if "created_at" in sub:
                sub["created_at"] = sub["created_at"].isoformat()
            if "expires_at" in sub:
                sub["expires_at"] = sub["expires_at"].isoformat()
            if "verified_at" in sub and sub["verified_at"]:
                sub["verified_at"] = sub["verified_at"].isoformat()

        return jsonify({
            "success": True,
            "subscriptions": subs
        })
    except Exception as e:
        print(f"[ERROR] subscription_history_api: {e}")
        return jsonify({"error": str(e)}), 500


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", config.PORT))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None
    app.run(host="0.0.0.0", port=port, debug=debug)
