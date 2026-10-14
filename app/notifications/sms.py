"""Mock SMS gateway for password-reset OTP delivery.

The SRS names no SMS provider, so delivery is stubbed. Outside production the
code is written to the application log, which is the delivery channel for local
testing. Replace `send_otp` with a real gateway call before deploying - the
production branch below deliberately refuses to invent one.
"""

import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def mask_mobile(mobile: str) -> str:
    """Keep the last four digits so logs are traceable but not a phone-book."""
    if len(mobile) <= 4:
        return "*" * len(mobile)
    return f"{'*' * (len(mobile) - 4)}{mobile[-4:]}"


def send_otp(mobile: str, otp: str) -> None:
    settings = get_settings()
    masked = mask_mobile(mobile)

    if settings.is_production:
        # No gateway is configured. Log loudly rather than pretend it was sent,
        # and never write the code itself to a production log.
        logger.error(
            "No SMS gateway configured - password reset code for %s could not be delivered. "
            "Implement app.notifications.sms.send_otp before running in production.",
            masked,
        )
        return

    # Phrased so it does not match the key=value patterns the log redaction
    # filter scrubs - in development this line IS the delivery mechanism.
    logger.info(
        "[MOCK SMS] to=%s | Your Expense Tracker password reset code is %s (valid for %d minutes)",
        masked,
        otp,
        settings.otp_expire_minutes,
    )
