"""Telegram notification tool — posts alert summaries to the ops channel.

Token and chat_id are read from environment variables at call time.
"""

import os

from dotenv import load_dotenv

load_dotenv()

import httpx
import structlog

from utils.formatting import markdown_to_telegram_html

log = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


def send_ops_alert(text: str, alert_id: str) -> dict:
    """Send a formatted alert message to the Telegram ops channel.

    Uses TELEGRAM_BOT_TOKEN and TELEGRAM_OPS_CHAT_ID from env vars.
    Returns dict with 'status': 'ok' or 'status': 'error'.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_OPS_CHAT_ID", "")

    if not token or not chat_id:
        log.error("telegram_missing_config", alert_id=alert_id,
                  has_token=bool(token), has_chat_id=bool(chat_id))
        return {"status": "error", "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_OPS_CHAT_ID not set"}

    html_text = markdown_to_telegram_html(text)
    return _post_message(token=token, chat_id=chat_id, text=html_text, alert_id=alert_id)


def _post_message(token: str, chat_id: str, text: str, alert_id: str) -> dict:
    """POST a message to the Telegram Bot API.

    Returns structured result dict. Never raises.
    """
    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    # Telegram message limit is 4096 chars
    truncated = text[:4000] + "\n\n<i>(truncated)</i>" if len(text) > 4000 else text
    try:
        response = httpx.post(
            url,
            json={"chat_id": chat_id, "text": truncated, "parse_mode": "HTML"},
            timeout=10,
        )
        response.raise_for_status()
        log.info("telegram_alert_sent", alert_id=alert_id, chat_id=chat_id)
        return {"status": "ok", "message_id": response.json().get("result", {}).get("message_id")}
    except httpx.HTTPStatusError as e:
        log.error("telegram_http_error", alert_id=alert_id,
                  status_code=e.response.status_code, error=e.response.text)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.error("telegram_unexpected_error", alert_id=alert_id, error=str(e))
        return {"status": "error", "error": str(e)}
