"""
Photo Finder – Flask Backend
Superadmin → manages users & subscriptions
Admin       → submits Google Drive link → QR generated
User        → scans QR → uploads selfie → sees matched photos → download / email
"""

import os, re, io, json, uuid, zipfile, smtplib, threading, shutil, hmac, hashlib, tempfile
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
        print(f"[INFO] MongoDB connected → {uri}  db={db_name}")
    except Exception as e:
        print(f"[WARN] MongoDB unavailable ({e}); falling back to JSON")
        _mongo_db = None
    return _mongo_db

def _ucol():
    """Return the 'users' collection or None (JSON fallback)."""
    db = _get_db()
    return db["users"] if db is not None else None

# DeepFace is imported lazily inside find_matches() to avoid loading
# TensorFlow at startup (prevents OOM crash on free-tier hosting)
DeepFace = None
FACE_OK   = False

def _load_deepface():
    global DeepFace, FACE_OK
    if DeepFace is not None:
        return FACE_OK
    try:
        from deepface import DeepFace as _DF
        DeepFace = _DF
        FACE_OK  = True
    except Exception as e:
        print(f"[WARN] deepface unavailable: {e}")
        FACE_OK = False
    return FACE_OK

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = config.SECRET_KEY

BASE   = Path(__file__).parent
DATA   = BASE / "data"
EVENTS = DATA / "events"
QRS    = DATA / "qrcodes"
UPS    = DATA / "uploads"
RES    = DATA / "results"
USERS_FILE           = DATA / "users.json"
PAYMENT_SETTINGS_FILE = DATA / "payment_settings.json"
PAYMENT_QR_FILE       = DATA / "payment_qr.png"

for d in [EVENTS, QRS, UPS, RES]:
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

# Run one-time JSON→MongoDB migration on startup
with app.app_context():
    try:
        migrate_json_to_mongo()
    except Exception:
        pass

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
            print(f"[INFO] Migrated {migrated} users from JSON → MongoDB")
    except Exception as e:
        print(f"[WARN] Migration error: {e}")

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
        # Subscription check for non-superadmin users
        if not session.get("is_superadmin"):
            uid = session.get("user_id")
            if uid:
                user = load_users().get(uid)
                if not user or not is_subscription_active(user):
                    session.clear()
                    return redirect(url_for("admin_login", expired=1))
                # Force-logout check
                if user.get("force_logout"):
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

def get_folder_id(link: str):
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    return m.group(1) if m else None

def list_drive_images(folder_id: str) -> list:
    url = "https://drive.google.com/drive/folders/" + folder_id
    files = []
    try:
        import gdown
        file_list = gdown.download_folder(
            url, output="/tmp/_gdown_tmp_", quiet=True, skip_download=True)
        if file_list:
            return [{"id": f, "name": Path(f).name} for f in file_list
                    if Path(f).suffix.lower() in IMG_EXTS]
    except Exception:
        pass
    try:
        resp = requests.get(
            "https://drive.google.com/drive/folders/" + folder_id,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        ids = re.findall(r'"([a-zA-Z0-9_-]{28,})"', resp.text)
        seen = set()
        for fid in ids:
            if fid not in seen:
                seen.add(fid)
                files.append({"id": fid, "name": fid + ".jpg"})
    except Exception:
        pass
    return files

def download_drive_folder(folder_id: str, dest: Path, status_path: Path):
    dest.mkdir(parents=True, exist_ok=True)
    _set_status(status_path, "downloading", "Downloading images from Drive…")
    downloaded = 0
    errors = 0
    try:
        import gdown
        url = "https://drive.google.com/drive/folders/" + folder_id
        gdown.download_folder(url, output=str(dest), quiet=True, use_cookies=False)
        imgs = [p for p in dest.rglob("*") if p.suffix.lower() in IMG_EXTS]
        downloaded = len(imgs)
        _set_status(status_path, "downloaded", f"Downloaded {downloaded} image(s) via gdown")
        return
    except Exception as e:
        _set_status(status_path, "downloading", f"gdown failed ({e}), trying direct…")

    file_list = list_drive_images(folder_id)
    for item in file_list[:200]:
        fid, name = item["id"], item["name"]
        out = dest / name
        if out.exists():
            downloaded += 1
            continue
        try:
            dl_url = f"https://drive.google.com/uc?export=download&id={fid}"
            r = requests.get(dl_url, timeout=20, stream=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                with open(out, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                downloaded += 1
        except Exception:
            errors += 1
    _set_status(status_path, "downloaded", f"Downloaded {downloaded} image(s) ({errors} errors)")

def build_encodings(event_dir: Path, status_path: Path):
    images_dir = event_dir / "images"
    imgs = [p for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]
    if not imgs:
        msg = ("No images found. Make sure your Google Drive folder is shared "
               "publicly (Anyone with the link → Viewer).")
        _set_status(status_path, "error", msg)
        _meta = _load_meta(event_dir / "meta.json")
        _meta["status"] = "error"
        _save_meta(event_dir / "meta.json", _meta)
        return
    _set_status(status_path, "indexing", f"Indexing {len(imgs)} image(s) for face recognition…")
    if _load_deepface():
        try:
            DeepFace.find(
                img_path=str(imgs[0]), db_path=str(images_dir),
                model_name=config.FACE_MODEL, detector_backend=config.FACE_DETECTOR,
                enforce_detection=False, silent=True)
        except Exception:
            pass
    meta = _load_meta(event_dir / "meta.json")
    meta["status"]      = "ready"
    meta["photo_count"] = len(imgs)
    _save_meta(event_dir / "meta.json", meta)
    _set_status(status_path, "ready", f"Ready – {len(imgs)} photos indexed")

def process_event_bg(event_id: str):
    event_dir   = EVENTS / event_id
    status_path = event_dir / "status.json"
    meta        = _load_meta(event_dir / "meta.json")
    try:
        download_drive_folder(meta["folder_id"], event_dir / "images", status_path)
        build_encodings(event_dir, status_path)
    except Exception as e:
        _set_status(status_path, "error", str(e))
        meta["status"] = "error"
        _save_meta(event_dir / "meta.json", meta)

def find_matches(selfie_path: Path, images_dir: Path) -> list:
    if not _load_deepface():
        return [{"path": str(p), "filename": p.name, "distance": 0.0}
                for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]
    try:
        results = DeepFace.find(
            img_path=str(selfie_path), db_path=str(images_dir),
            model_name=config.FACE_MODEL, detector_backend=config.FACE_DETECTOR,
            enforce_detection=False, threshold=config.FACE_THRESHOLD, silent=True)
        matches, seen = [], set()
        for df in results:
            if df.empty:
                continue
            for _, row in df.iterrows():
                p = Path(row["identity"])
                if str(p) not in seen:
                    seen.add(str(p))
                    matches.append({"path": str(p), "filename": p.name,
                                    "distance": float(row.get("distance", 0))})
        matches.sort(key=lambda x: x["distance"])
        return matches
    except Exception:
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
        error = "Your subscription has expired or session ended. Please log in again."

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # ── Registered user check ─────────────────────────────────────────────
        uid, user = get_user_by_username(username)
        if uid and user:
            if check_password_hash(user["password"], password):
                if not user.get("is_active", False):
                    error = "⏳ Account pending activation. Please contact the administrator."
                elif not is_subscription_active(user):
                    error = "❌ Your subscription has expired. Please contact the administrator to renew."
                else:
                    # Clear any force-logout flag on fresh login
                    if user.get("force_logout"):
                        update_user(uid, {"force_logout": False})
                    session.clear()
                    session["admin"]        = True
                    session["is_superadmin"] = False
                    session["user_id"]       = uid
                    session["username"]      = username
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
    if user_info:
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
        "user_url":    user_url,
        "status":      "processing",
        "photo_count": 0,
        "upload_mode": upload_mode,
        "owner_id":    session.get("user_id") or "superadmin",
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
    if ev_status != "ready":
        return jsonify({"error": "Event photos are still processing. Please try again shortly."}), 503

    sid         = uuid.uuid4().hex[:10]
    selfie_path = UPS / f"{sid}.jpg"
    try:
        img = Image.open(photo.stream).convert("RGB")
        img.save(str(selfie_path), "JPEG", quality=90)
    except Exception as e:
        return jsonify({"error": f"Could not read image: {e}"}), 400

    matches = find_matches(selfie_path, images_dir)
    if not matches:
        selfie_path.unlink(missing_ok=True)
        return jsonify({"error": "No matching photos found. Try a clearer selfie."}), 404

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
    """Serve matched photo at full original quality."""
    data = _load_meta(RES / sid / "matches.json")
    if not data or idx >= len(data["matches"]):
        return "Not found", 404
    p = Path(data["matches"][idx]["path"])
    if not p.exists():
        return "Photo not found", 404
    return send_file(str(p))


@app.route("/photo/<sid>/<int:idx>/thumb")
def serve_photo_thumb(sid, idx):
    """Serve a small thumbnail (max 400px) for fast grid preview."""
    data = _load_meta(RES / sid / "matches.json")
    if not data or idx >= len(data["matches"]):
        return "Not found", 404
    p = Path(data["matches"][idx]["path"])
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
    """Stream matched photos as ZIP using a temp file — safe for large/many images."""
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return "Session not found", 404
    matches = data.get("matches", [])
    if not matches:
        return "No photos to download", 404

    # Write ZIP to a temp file so we don't hold GBs in RAM
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    tmp_path = Path(tmp.name)

    seen_names: set = set()
    try:
        # ZIP_STORED: JPEG/PNG are already compressed; no point deflating
        with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_STORED) as zf:
            for m in matches:
                p = Path(m["path"])
                if not p.exists():
                    continue
                fname = m["filename"]
                if fname in seen_names:
                    fname = f"{p.stem}_{uuid.uuid4().hex[:4]}{p.suffix}"
                seen_names.add(fname)
                zf.write(str(p), fname)

        zip_size = tmp_path.stat().st_size

        def _generate():
            try:
                with open(str(tmp_path), "rb") as fh:
                    while True:
                        chunk = fh.read(65536)   # 64 KB chunks
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

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
    """Return file sizes of matched photos for display in UI."""
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return jsonify([])
    info = []
    for i, m in enumerate(data.get("matches", [])):
        p = Path(m["path"])
        size_kb = round(p.stat().st_size / 1024, 1) if p.exists() else 0
        info.append({"idx": i, "filename": m["filename"], "size_kb": size_kb})
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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in data["matches"]:
            zf.write(m["path"], m["filename"])
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


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", config.PORT))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None
    app.run(host="0.0.0.0", port=port, debug=debug)
