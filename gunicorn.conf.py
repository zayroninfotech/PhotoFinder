import os

bind    = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
timeout = 120
loglevel    = "info"
accesslog   = "-"   # stdout
errorlog    = "-"   # stdout (so Render captures it)
