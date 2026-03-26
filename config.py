# ─────────────────────────────────────────────
#  config.py  –  Edit these before running
# ─────────────────────────────────────────────

# Admin credentials  (Superadmin)
ADMIN_USERNAME = "vamsi"
ADMIN_PASSWORD = "Zayron@2026"

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
FACE_DETECTOR  = "retinaface"      # Better accuracy: 'retinaface' | 'opencv' | 'mtcnn'
FACE_THRESHOLD = 0.35              # Stricter matching (was 0.40, lower = stricter)

# ── Server ────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5000

# ── MongoDB ─────────────────────────────────────────────────────────────────────────────
# Local VPS:  "mongodb://localhost:27017/"
# Atlas:      "mongodb+srv://user:pass@cluster.mongodb.net/"
MONGODB_URI = "mongodb://localhost:27017/"
MONGODB_DB  = "photofinder"

# ── Razorpay ──────────────────────────────────────────────────────────────────────────────
# Sign up at https://razorpay.com  → Settings → API Keys → Generate Test Key
RAZORPAY_KEY_ID     = "rzp_test_SRzQUI8EhzQGZv"
RAZORPAY_KEY_SECRET = "Cx2hPbtdTw8HTTGWVhDgUtm2"

# ── Quick Subscription Plans (UPI) ───────────────────────────────────────────────────────
# Plans for user subscriptions via UPI QR code payment
QUICK_SUBSCRIPTION_PLANS = {
    "1day": {"days": 1, "amount": 1, "label": "1 Day Access"},
    "2day": {"days": 2, "amount": 3, "label": "2 Days Access"},
    "4day": {"days": 4, "amount": 5, "label": "4 Days Access"},
}

# ── UPI Configuration ────────────────────────────────────────────────────────────────────
# UPI ID for receiving payments (merchant UPI)
UPI_MERCHANT_ID = "saivanteddu@ybl"  # Your UPI ID
UPI_MERCHANT_NAME = "Zayro Lens"
