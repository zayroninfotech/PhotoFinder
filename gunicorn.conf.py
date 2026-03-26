import os

# ── Gunicorn config for Hostinger VPS (KVM 1) ─────────────────────────────────
bind           = "127.0.0.1:" + os.environ.get("PORT", "4000")
workers        = 2       # 2 workers for 1 vCPU / 4GB RAM
threads        = 2
timeout        = 300     # 5 min — allow long photo-processing jobs
keepalive      = 5
loglevel       = "info"
accesslog      = "-"     # stdout → systemd journal
errorlog       = "-"
capture_output = True
