"""Optional unattended Upstox login.

Upstox access tokens always expire at 03:30 IST and Upstox issues no
refresh_token grant, so the only way to renew a token without a human
opening a browser is to script the same TOTP-based login a person would
do by hand. This module does that using the third-party `upstox-totp`
package, and is only active when UPSTOX_AUTO_LOGIN_ENABLED=true and the
account credentials below are present in .env.

This trades convenience for a materially larger secret surface: unlike a
plain access token, these credentials can place trades on the account.
Keep backend/.env off any shared machine and out of version control.

Required when enabled:
  UPSTOX_USERNAME       10-digit Upstox account mobile number
  UPSTOX_PASSWORD       Upstox account password
  UPSTOX_PIN_CODE       Upstox account PIN
  UPSTOX_TOTP_SECRET    TOTP secret shown when enabling authenticator 2FA
  UPSTOX_CLIENT_ID / UPSTOX_CLIENT_SECRET / UPSTOX_REDIRECT_URI (already
  used by the manual OAuth flow in main.py)
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("uvicorn.error")

ENV_PATH = Path(__file__).resolve().parent / ".env"


def auto_login_enabled() -> bool:
    return os.getenv("UPSTOX_AUTO_LOGIN_ENABLED", "false").strip().lower() == "true"


def renew_access_token() -> str:
    """Log in to Upstox with stored TOTP credentials and return a fresh access token.

    Raises RuntimeError on failure. The error message intentionally omits
    request/response bodies so credentials never reach the logs.
    """
    from upstox_totp import UpstoxTOTP
    from upstox_totp.errors import ConfigurationError, UpstoxError, ValidationError

    # Deliberately NOT UpstoxTOTP.from_env_file(): that helper base64-encodes
    # the PIN itself and then AppTokenAPI.submit_pin() encodes it again,
    # double-encoding the PIN and breaking login. The plain constructor reads
    # the same env vars without pre-encoding.
    try:
        client = UpstoxTOTP()
        response = client.app_token.get_access_token()
    except ConfigurationError as error:
        raise RuntimeError(f"Upstox auto-login is missing configuration: {error}") from error
    except (UpstoxError, ValidationError) as error:
        raise RuntimeError(f"Upstox auto-login was rejected: {error}") from error

    if not response.success or not response.data:
        raise RuntimeError(f"Upstox auto-login failed: {response.error}")

    return response.data.access_token


def persist_access_token(token: str) -> None:
    """Rewrite UPSTOX_ACCESS_TOKEN in backend/.env so a restart keeps the fresh token."""
    if not ENV_PATH.exists():
        logger.warning("Cannot persist renewed Upstox token: %s does not exist", ENV_PATH)
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = False
    for index, line in enumerate(lines):
        if line.startswith("UPSTOX_ACCESS_TOKEN="):
            lines[index] = f"UPSTOX_ACCESS_TOKEN={token}"
            updated = True
            break
    if not updated:
        lines.append(f"UPSTOX_ACCESS_TOKEN={token}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
