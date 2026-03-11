"""Central LLM factory — builds a ChatBedrock instance with explicit credentials.

Reads credentials from environment at call time so they are always fresh.
Separates BEDROCK_REGION from AWS_DEFAULT_REGION because Bedrock is often
enabled in a different region than the workload (e.g. us-east-1 while
infra runs in ap-south-1).
"""

import os

import structlog
from dotenv import load_dotenv
from langchain_aws import ChatBedrock

load_dotenv()

log = structlog.get_logger(__name__)

# Default model — override with BEDROCK_MODEL_ID env var.
# Accepts both the langchain "bedrock:..." prefix format and the raw model ID.
# Examples:
#   bedrock:global.anthropic.claude-sonnet-4-6      (cross-region, all regions)
#   global.anthropic.claude-sonnet-4-6              (same, no prefix)
#   anthropic.claude-3-5-sonnet-20241022-v2:0       (single region)
_DEFAULT_MODEL_ID = "bedrock:global.anthropic.claude-sonnet-4-6"


def get_bedrock_llm(structured_output_schema=None):
    """Build a ChatBedrock instance with credentials loaded from environment.

    Passes AWS credentials explicitly so they are picked up correctly in
    background threads where the boto3 credential chain may not find .env vars.
    Strips the 'bedrock:' prefix if present — ChatBedrock takes the raw model ID.

    Args:
        structured_output_schema: Optional Pydantic model class. When provided,
            returns llm.with_structured_output(schema).

    Returns:
        Configured ChatBedrock instance (or structured output wrapper).
    """
    raw = os.environ.get("BEDROCK_MODEL_ID", _DEFAULT_MODEL_ID)
    # ChatBedrock takes the model ID without the "bedrock:" provider prefix
    model_id = raw.removeprefix("bedrock:")
    region = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")  # needed for temporary credentials

    log.debug("bedrock_llm_init", model_id=model_id, region=region, has_key=bool(access_key))

    kwargs = {
        "model_id": model_id,
        "region_name": region,
        "model_kwargs": {"max_tokens": 2048},
    }

    # Only pass explicit credentials if present — allows IAM role auth when keys absent
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token

    llm = ChatBedrock(**kwargs)

    if structured_output_schema is not None:
        return llm.with_structured_output(structured_output_schema)
    return llm
