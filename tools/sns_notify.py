"""SNS SMS notification tool — used for P1 on-call alerts.

Phone number and region are read from environment variables at call time.
"""

import os

from dotenv import load_dotenv

load_dotenv()

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

log = structlog.get_logger(__name__)


def send_sms(message: str, alert_id: str) -> dict:
    """Send an SMS to the on-call phone number via Amazon SNS.

    Phone number is read from SNS_ONCALL_PHONE env var.
    Returns dict with 'status': 'ok' and 'message_id' on success,
    or 'status': 'error' on failure.
    """
    phone = os.environ.get("SNS_ONCALL_PHONE", "")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")

    if not phone:
        log.error("sns_sms_no_phone_configured", alert_id=alert_id)
        return {"status": "error", "error": "SNS_ONCALL_PHONE env var not set"}

    truncated_message = message[:1600]  # SNS SMS limit
    return _publish_sms(phone=phone, message=truncated_message, region=region, alert_id=alert_id)


def _publish_sms(phone: str, message: str, region: str, alert_id: str) -> dict:
    """Publish an SMS message to a phone number via SNS.

    Returns structured result dict. Never raises.
    """
    try:
        client = boto3.client("sns", region_name=region)
        response = client.publish(
            PhoneNumber=phone,
            Message=message,
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
                "AWS.SNS.SMS.SenderID": {"DataType": "String", "StringValue": "ARGOS"},
            },
        )
        message_id = response.get("MessageId", "unknown")
        log.info("sns_sms_sent", alert_id=alert_id, message_id=message_id, phone_suffix=phone[-4:])
        return {"status": "ok", "message_id": message_id}
    except ClientError as e:
        log.error("sns_client_error", alert_id=alert_id,
                  error_code=e.response["Error"]["Code"],
                  error=e.response["Error"]["Message"])
        return {"status": "aws_error", "error": e.response["Error"]["Message"]}
    except BotoCoreError as e:
        log.error("sns_botocore_error", alert_id=alert_id, error=str(e))
        return {"status": "botocore_error", "error": str(e)}
    except Exception as e:
        log.error("sns_unexpected_error", alert_id=alert_id, error=str(e))
        return {"status": "error", "error": str(e)}
