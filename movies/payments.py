"""
Thin wrapper around the Razorpay SDK

Kept separate from views.py so the actual network-calling client is a single, easily
moctale seam in tests (patch movies.payments.get_client) rather than something scattered
through the view functions.
"""

import razorpay
from django.conf import settings


class PaymentVerificationError(Exception):
    """Raised when a payment or webhook signature fails verification."""


def get_client():
    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def create_order(amount_in_paise, currency="INR", receipt=None, notes=None):
    """Create a Raxorpay Order and return the SDK's response dict (contains 'id)."""
    client = get_client()
    return client.order.create(
        {
            "amount": amount_in_paise,
            "currency": currency,
            "payment_capture": 1,
            "receipt": receipt or "",
            "notes": notes or {},
        }
    )


def verify_payment_signature(order_id, payment_id, signature):
    """Verify the signature Razorpay's checkout.js handed back to the browser after a
    successful payment. Raised PaymentVerificationError if it check out."""
    client = get_client()
    params = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }
    try:
        client.utility.verify_payment_signature(params)
    except razorpay.errors.SignatureVerificationError as exc:
        raise PaymentVerificationError(str(exc)) from exc


def verify_webhook_signature(payload_body, received_signature):
    """Verify a Razorpay webhook's X-Razorpay-Signature header against the raw request body,
    using the seperate webhook secret configured in the Razorpay dashboard."""
    client = get_client()
    try:
        client.utility.verify_webhook_signature(
            payload_body, received_signature, settings.RAZORPAY_WEBHOOK_SECRET
        )
    except razorpay.errors.SignatureVerificationError as exc:
        raise PaymentVerificationError(str(exc)) from exc
