# ─────────────────────────────────────────────
#  config.py  –  Edit these before running
# ─────────────────────────────────────────────

# Admin credentials  (Superadmin)
ADMIN_USERNAME = "vamsi"
ADMIN_PASSWORD = "zayron@2026"

# Flask secret key  (change this to something random)
SECRET_KEY = "change-this-secret-key-xyz-2024"

# ── Email (Gmail SMTP) ────────────────────────
# Create an App Password at: https://myaccount.google.com/apppasswords
EMAIL_SENDER   = "your-gmail@gmail.com"
EMAIL_PASSWORD = "xxxx xxxx xxxx xxxx"   # 16-char app password
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 465

# ── Face Recognition ─────────────────────────
# Model choices: 'Facenet512', 'VGG-Face', 'ArcFace', 'DeepFace'
FACE_MODEL     = "Facenet512"
FACE_DETECTOR  = "opencv"         # 'opencv' | 'retinaface' | 'mtcnn'
FACE_THRESHOLD = 0.40             # lower = stricter match

# ── Server ────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5000

# ── MongoDB ─────────────────────────────────────────────────────────────────────────────
# Local VPS:  "mongodb://localhost:27017/"
# Atlas:      "mongodb+srv://user:pass@cluster.mongodb.net/"
MONGODB_URI = "mongodb://localhost:27017/"
MONGODB_DB  = "photofinder"
