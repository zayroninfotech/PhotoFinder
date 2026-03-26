"""
UPI Payment utilities for subscription QR generation.
Handles UPI string generation, QR code creation, and subscription verification.
"""

import os
import uuid
import qrcode
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
import base64

# Config
SUBSCRIPTIONS_DIR = Path(__file__).parent / "data" / "subscriptions"
SUBSCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)

SUBSCRIPTION_PLANS = {
    "1day": {"days": 1, "amount": 1, "label": "1 Day Access"},
    "2day": {"days": 2, "amount": 3, "label": "2 Days Access"},
    "4day": {"days": 4, "amount": 5, "label": "4 Days Access"},
}


def generate_upi_string(user_id, plan_id, amount):
    """
    Generate UPI payment string for PhonePe/Google Pay.
    Format: upi://pay?pa=upiid&pn=name&am=amount&tn=note&tr=reference
    """
    UPI_MERCHANT_ID = os.environ.get("UPI_MERCHANT_ID", "merchant@upi")
    UPI_MERCHANT_NAME = "Zayro Lens"

    reference_id = f'SUB_{user_id}_{datetime.utcnow().strftime("%Y%m%d%H%M%S")}'
    note = f'Zayro Lens {plan_id.upper()}'

    upi_string = (
        f'upi://pay?'
        f'pa={UPI_MERCHANT_ID}&'
        f'pn={quote(UPI_MERCHANT_NAME)}&'
        f'am={amount}&'
        f'tn={quote(note)}&'
        f'tr={reference_id}'
    )
    return upi_string


def generate_subscription_qr(user_id, plan_id, db=None):
    """
    Generate QR code for subscription payment.
    Returns: (subscription_id, qr_code_path, upi_string, expiry_date)
    """
    plan_info = SUBSCRIPTION_PLANS.get(plan_id)
    if not plan_info:
        return None, None, None, None

    amount = plan_info["amount"]
    days = plan_info["days"]

    # Generate UPI string
    upi_string = generate_upi_string(user_id, plan_id, amount)

    # Generate QR code
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Save to file
    subscription_id = f'sub_{str(uuid.uuid4())[:8]}'
    filename = f'{subscription_id}_qr.png'
    filepath = SUBSCRIPTIONS_DIR / filename
    img.save(str(filepath))

    qr_code_path = f'subscriptions/{filename}'

    # Calculate expiry date
    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

    # Create subscription record in DB
    if db is not None:
        subscription = {
            "subscription_id": subscription_id,
            "user_id": user_id,
            "plan_id": plan_id,
            "amount": amount,
            "currency": "INR",
            "upi_string": upi_string,
            "qr_code_path": qr_code_path,
            "transaction_id": None,
            "status": "pending",
            "created_at": now,
            "expires_at": expires_at,
            "verified_at": None,
            "payment_method": "upi"
        }
        db["subscriptions"].insert_one(subscription)

    return subscription_id, qr_code_path, upi_string, expires_at


def verify_subscription(subscription_id, transaction_id, db=None):
    """Verify subscription after payment and update user subscription."""
    if db is None:
        return False

    sub = db["subscriptions"].find_one({"subscription_id": subscription_id})
    if not sub:
        return False

    now = datetime.utcnow()

    # Update subscription record
    db["subscriptions"].update_one(
        {"subscription_id": subscription_id},
        {
            "$set": {
                "status": "verified",
                "transaction_id": transaction_id,
                "verified_at": now
            }
        }
    )

    # Update user subscription end date
    user_id = sub["user_id"]
    db["users"].update_one(
        {"id": user_id},  # Use "id" field, not "_id"
        {
            "$set": {
                "subscription_end": sub["expires_at"].date().isoformat(),
                "is_active": True
            }
        }
    )

    return True


def get_user_subscriptions(user_id, db=None, limit=20):
    """Get subscription history for a user."""
    if db is None:
        return []

    subs = list(
        db["subscriptions"]
        .find({"user_id": user_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    return subs


def is_user_subscription_active(user_id, db=None):
    """Check if user has active subscription."""
    if db is None:
        return False

    now = datetime.utcnow()
    sub = db["subscriptions"].find_one({
        "user_id": user_id,
        "status": "verified",
        "expires_at": {"$gt": now}
    })
    return sub is not None
