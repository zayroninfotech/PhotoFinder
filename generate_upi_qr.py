"""
Generate a static UPI QR code for saivanteddu@ybl
Run this once to create the QR code image
"""

import qrcode
from pathlib import Path
from urllib.parse import quote

# Configuration
UPI_ID = "saivanteddu@ybl"
MERCHANT_NAME = "Zayro Lens"
AMOUNT = 0  # 0 = dynamic amount (user can enter)

# Create UPI string
# Format: upi://pay?pa=UPI_ID&pn=NAME&am=AMOUNT
upi_string = f"upi://pay?pa={UPI_ID}&pn={quote(MERCHANT_NAME)}&am={AMOUNT}"

print("[INFO] UPI String: " + upi_string)

# Generate QR code
qr = qrcode.QRCode(
    version=None,
    error_correction=qrcode.constants.ERROR_CORRECT_H,
    box_size=10,
    border=4,
)
qr.add_data(upi_string)
qr.make(fit=True)

# Create image
img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

# Save to file
qr_dir = Path(__file__).parent / "data" / "subscriptions"
qr_dir.mkdir(parents=True, exist_ok=True)

qr_path = qr_dir / "upi_qr.png"
img.save(str(qr_path))

print("[SUCCESS] QR Code saved: " + str(qr_path))
print("[INFO] Location: data/subscriptions/upi_qr.png")
