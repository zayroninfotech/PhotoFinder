"""
Photo Finder – Flask Backend
Admin → submits Google Drive link → QR generated
User  → scans QR → uploads selfie → sees matched photos → download / email
"""

import os, re, io, json, uuid, zipfile, smtplib, threading, shutil
from pathlib import Path
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import qrcode
import requests
from flask import (Flask, render_template, request, session,
                   redirect, url_for, jsonify, send_file)
from PIL import Image

import config

# Allow overriding config from environment variables (for cloud deployment)
config.ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", config.ADMIN_USERNAME)
config.ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", config.ADMIN_PASSWORD)
config.SECRET_KEY     = os.environ.get("SECRET_KEY",     config.SECRET_KEY)
config.EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",   config.EMAIL_SENDER)
config.EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", config.EMAIL_PASSWORD)
app_secret = os.environ.get("SECRET_KEY", config.SECRET_KEY)

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

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = config.SECRET_KEY

BASE   = Path(__file__).parent
DATA   = BASE / "data"
EVENTS = DATA / "events"
QRS    = DATA / "qrcodes"
UPS    = DATA / "uploads"
RES    = DATA / "results"

for d in [EVENTS, QRS, UPS, RES]:
    d.mkdir(parents=True, exist_ok=True)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_folder_id(link: str) -> str | None:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    return m.group(1) if m else None


def list_drive_images(folder_id: str) -> list[dict]:
    """
    Return list of {id, name} for image files in a *public* Drive folder.
    Uses the undocumented export endpoint – no API key required.
    """
    url = (
        "https://drive.google.com/drive/folders/"
        + folder_id
    )
    # Try gdown approach first
    files = []
    try:
        import gdown
        # gdown can list folder contents
        file_list = gdown.download_folder(
            url,
            output="/tmp/_gdown_tmp_",
            quiet=True,
            skip_download=True,
        )
        if file_list:
            return [{"id": f, "name": Path(f).name} for f in file_list
                    if Path(f).suffix.lower() in IMG_EXTS]
    except Exception:
        pass

    # Fallback: parse the folder page HTML for file IDs
    try:
        resp = requests.get(
            "https://drive.google.com/drive/folders/" + folder_id,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
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
    """Download all images from a public Drive folder into dest/"""
    dest.mkdir(parents=True, exist_ok=True)
    _set_status(status_path, "downloading", "Downloading images from Drive…")

    downloaded = 0
    errors = 0

    # ── Try gdown (most reliable for public folders) ──────────────────────────
    try:
        import gdown
        url = "https://drive.google.com/drive/folders/" + folder_id
        gdown.download_folder(url, output=str(dest), quiet=True, use_cookies=False)
        imgs = list(dest.rglob("*"))
        imgs = [p for p in imgs if p.suffix.lower() in IMG_EXTS]
        downloaded = len(imgs)
        _set_status(status_path, "downloaded",
                    f"Downloaded {downloaded} image(s) via gdown")
        return
    except Exception as e:
        _set_status(status_path, "downloading",
                    f"gdown failed ({e}), trying direct download…")

    # ── Fallback: direct file download via Drive export URL ───────────────────
    file_list = list_drive_images(folder_id)
    for item in file_list[:200]:           # cap at 200 to avoid abuse
        fid  = item["id"]
        name = item["name"]
        out  = dest / name
        if out.exists():
            downloaded += 1
            continue
        try:
            dl_url = f"https://drive.google.com/uc?export=download&id={fid}"
            r = requests.get(dl_url, timeout=20, stream=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type",""):
                with open(out, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                downloaded += 1
        except Exception:
            errors += 1

    _set_status(status_path, "downloaded",
                f"Downloaded {downloaded} image(s) ({errors} errors)")


def build_encodings(event_dir: Path, status_path: Path):
    """Run DeepFace.find pre-population (build the pkl representation DB)."""
    images_dir = event_dir / "images"
    imgs = [p for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]

    if not imgs:
        msg = ("No images found. Make sure your Google Drive folder is shared "
               "publicly (Anyone with the link → Viewer).")
        _set_status(status_path, "error", msg)
        # Also mark meta so user page shows the real error
        _meta = _load_meta(event_dir / "meta.json")
        _meta["status"] = "error"
        _save_meta(event_dir / "meta.json", _meta)
        return

    _set_status(status_path, "indexing",
                f"Indexing {len(imgs)} image(s) for face recognition…")

    if _load_deepface():
        try:
            # Build DeepFace representation DB
            DeepFace.find(
                img_path=str(imgs[0]),
                db_path=str(images_dir),
                model_name=config.FACE_MODEL,
                detector_backend=config.FACE_DETECTOR,
                enforce_detection=False,
                silent=True,
            )
        except Exception:
            # First call may "fail" if no face in first image – still builds DB
            pass

    meta = _load_meta(event_dir / "meta.json")
    meta["status"]      = "ready"
    meta["photo_count"] = len(imgs)
    _save_meta(event_dir / "meta.json", meta)
    _set_status(status_path, "ready",
                f"Ready – {len(imgs)} photos indexed")


def process_event_bg(event_id: str):
    """Background thread: download + index."""
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


def find_matches(selfie_path: Path, images_dir: Path) -> list[dict]:
    if not _load_deepface():
        # return all images as demo when deepface unavailable
        return [{"path": str(p), "filename": p.name, "distance": 0.0}
                for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]

    try:
        results = DeepFace.find(
            img_path=str(selfie_path),
            db_path=str(images_dir),
            model_name=config.FACE_MODEL,
            detector_backend=config.FACE_DETECTOR,
            enforce_detection=False,
            threshold=config.FACE_THRESHOLD,
            silent=True,
        )
        matches = []
        seen    = set()
        for df in results:
            if df.empty:
                continue
            for _, row in df.iterrows():
                p = Path(row["identity"])
                if str(p) not in seen:
                    seen.add(str(p))
                    matches.append({
                        "path":     str(p),
                        "filename": p.name,
                        "distance": float(row.get("distance", 0)),
                    })
        matches.sort(key=lambda x: x["distance"])
        return matches
    except Exception as e:
        return []


# ── Tiny JSON helpers ─────────────────────────────────────────────────────────

def _load_meta(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}

def _save_meta(p: Path, d: dict):
    p.write_text(json.dumps(d, indent=2))

def _set_status(p: Path, state: str, msg: str):
    _save_meta(p, {"state": state, "message": msg})

def _admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
#  Admin routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return redirect(url_for("admin_login"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if (request.form.get("username") == config.ADMIN_USERNAME and
                request.form.get("password") == config.ADMIN_PASSWORD):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@_admin_required
def admin_dashboard():
    events = []
    for d in sorted(EVENTS.iterdir(), reverse=True):
        if d.is_dir():
            m = _load_meta(d / "meta.json")
            if m:
                events.append(m)
    return render_template("admin_dashboard.html", events=events)


def _make_qr(event_id: str, user_url: str):
    """Generate and save QR code PNG."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(user_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1e293b", back_color="white")
    img.save(str(QRS / f"{event_id}.png"))


@app.route("/admin/submit", methods=["POST"])
@_admin_required
def admin_submit():
    event_name  = request.form.get("event_name", "Event").strip()
    drive_link  = request.form.get("drive_link", "").strip()
    upload_mode = request.form.get("upload_mode", "drive")   # "drive" or "files"
    uploaded_files = request.files.getlist("photos")

    event_id  = uuid.uuid4().hex[:10]
    event_dir = EVENTS / event_id
    images_dir = event_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Use X-Forwarded-Proto so HTTPS is preserved behind Render's proxy
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
    }
    _save_meta(event_dir / "meta.json", meta)
    _make_qr(event_id, user_url)

    if upload_mode == "files" and uploaded_files:
        # ── Direct upload path ────────────────────────────────────────────────
        _set_status(event_dir / "status.json", "saving", "Saving uploaded photos…")
        saved = 0
        for f in uploaded_files:
            if f and f.filename:
                ext = Path(f.filename).suffix.lower()
                if ext in IMG_EXTS:
                    try:
                        img = Image.open(f.stream).convert("RGB")
                        out = images_dir / f"{uuid.uuid4().hex[:8]}{ext}"
                        img.save(str(out), quality=92)
                        saved += 1
                    except Exception:
                        pass
        if saved == 0:
            _set_status(event_dir / "status.json", "error", "No valid images found in upload")
            meta["status"] = "error"
            _save_meta(event_dir / "meta.json", meta)
        else:
            # Run indexing in background (no download needed)
            t = threading.Thread(
                target=build_encodings,
                args=(event_dir, event_dir / "status.json"),
                daemon=True,
            )
            t.start()
    else:
        # ── Drive download path ───────────────────────────────────────────────
        if not meta["folder_id"]:
            return jsonify({"error": "Invalid Google Drive folder URL"}), 400
        _set_status(event_dir / "status.json", "queued", "Queued for processing…")
        t = threading.Thread(target=process_event_bg, args=(event_id,), daemon=True)
        t.start()

    return jsonify({
        "event_id": event_id,
        "qr_url":   f"/qr/{event_id}",
        "user_url": user_url,
        "status":   "processing",
    })


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
    """Upload more photos to an existing event and re-index."""
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
                    img = Image.open(f.stream).convert("RGB")
                    out = images_dir / f"{uuid.uuid4().hex[:8]}{ext}"
                    img.save(str(out), quality=92)
                    saved += 1
                except Exception:
                    pass
    if saved:
        # Delete old pkl so DeepFace rebuilds
        for pkl in images_dir.glob("*.pkl"):
            pkl.unlink()
        t = threading.Thread(
            target=build_encodings,
            args=(event_dir, event_dir / "status.json"),
            daemon=True,
        )
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


# ── QR image ──────────────────────────────────────────────────────────────────

@app.route("/qr/<event_id>")
def serve_qr(event_id):
    p = QRS / f"{event_id}.png"
    if not p.exists():
        return "QR not found", 404
    return send_file(str(p), mimetype="image/png")


@app.route("/qr/<event_id>/download")
def download_qr(event_id):
    """Force-download the QR code PNG."""
    p = QRS / f"{event_id}.png"
    if not p.exists():
        return "QR not found", 404
    meta = load_json(EVENTS / event_id / "meta.json")
    name = meta.get("name", event_id)
    safe = re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") or event_id
    return send_file(str(p), mimetype="image/png",
                     as_attachment=True,
                     download_name=f"QR_{safe}.png")


# ══════════════════════════════════════════════════════════════════════════════
#  User routes
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
    event_status = meta.get("status")
    if event_status == "error":
        st_msg = _load_meta(event_dir / "status.json").get(
            "message", "Event processing failed.")
        return jsonify({"error": f"⚠️ {st_msg}"}), 503
    if event_status != "ready":
        return jsonify({"error": "Event photos are still being processed. Please try again shortly."}), 503

    # Save selfie
    sid          = uuid.uuid4().hex[:10]
    selfie_path  = UPS / f"{sid}.jpg"
    try:
        img = Image.open(photo.stream).convert("RGB")
        img.save(str(selfie_path), "JPEG", quality=90)
    except Exception as e:
        return jsonify({"error": f"Could not read image: {e}"}), 400

    # Find matches
    matches = find_matches(selfie_path, images_dir)

    if not matches:
        selfie_path.unlink(missing_ok=True)
        return jsonify({"error": "No matching photos found. Try a clearer selfie."}), 404

    # Save session results
    res_dir = RES / sid
    res_dir.mkdir(parents=True, exist_ok=True)
    _save_meta(res_dir / "matches.json", {"event_id": event_id, "matches": matches})

    return jsonify({
        "session_id":  sid,
        "match_count": len(matches),
        "photos": [
            {"url": f"/photo/{sid}/{i}", "filename": m["filename"]}
            for i, m in enumerate(matches)
        ],
    })


@app.route("/photo/<sid>/<int:idx>")
def serve_photo(sid, idx):
    data = _load_meta(RES / sid / "matches.json")
    if not data or idx >= len(data["matches"]):
        return "Not found", 404
    return send_file(data["matches"][idx]["path"])


@app.route("/download/<sid>")
def download_zip(sid):
    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return "Session not found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in data["matches"]:
            zf.write(m["path"], m["filename"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="my_photos.zip")


@app.route("/send-email", methods=["POST"])
def send_email():
    body      = request.json or {}
    email     = (body.get("email") or "").strip()
    sid       = body.get("session_id", "")

    if not email:
        return jsonify({"error": "Email address required"}), 400

    data = _load_meta(RES / sid / "matches.json")
    if not data:
        return jsonify({"error": "Session not found"}), 404

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in data["matches"]:
            zf.write(m["path"], m["filename"])
    buf.seek(0)

    try:
        msg              = MIMEMultipart()
        msg["From"]      = config.EMAIL_SENDER
        msg["To"]        = email
        msg["Subject"]   = "Your Photos 📸"
        body_text        = (
            f"Hi there!\n\n"
            f"We found {len(data['matches'])} photo(s) for you.\n"
            f"They are attached as a ZIP file.\n\n"
            f"Enjoy! 🎉"
        )
        msg.attach(MIMEText(body_text, "plain"))

        att = MIMEBase("application", "octet-stream")
        att.set_payload(buf.read())
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment",
                       filename="my_photos.zip")
        msg.attach(att)

        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as srv:
            srv.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            srv.send_message(msg)

        return jsonify({"ok": True, "message": f"Photos sent to {email}"})
    except Exception as e:
        return jsonify({"error": f"Email failed: {e}"}), 500


# ── 404 ───────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", config.PORT))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None  # no debug in prod
    app.run(host="0.0.0.0", port=port, debug=debug)
