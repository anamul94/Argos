"""Credential and Bedrock diagnostics — run this before the main app.

Usage:
    python test_credentials.py

Checks in order:
  1. What credentials are loaded from .env
  2. STS get-caller-identity  — proves the keys are valid
  3. Bedrock list-foundation-models — proves Bedrock access
  4. Bedrock invoke (Claude) — proves the model works end-to-end
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── 1. Show what's loaded ──────────────────────────────────────────────────────

access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
secret_key  = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
region      = os.environ.get("AWS_DEFAULT_REGION", "NOT SET")
bedrock_region = os.environ.get("BEDROCK_REGION", region)
model_id    = os.environ.get("BEDROCK_MODEL_ID", "NOT SET")

print("=" * 60)
print("STEP 1 — Environment variables")
print("=" * 60)
print(f"AWS_ACCESS_KEY_ID     : {access_key[:8]}... ({len(access_key)} chars)" if access_key else "AWS_ACCESS_KEY_ID     : *** NOT SET ***")
print(f"AWS_SECRET_ACCESS_KEY : {'*' * 8}... ({len(secret_key)} chars)" if secret_key else "AWS_SECRET_ACCESS_KEY : *** NOT SET ***")
print(f"AWS_DEFAULT_REGION    : {region}")
print(f"BEDROCK_REGION        : {bedrock_region}")
print(f"BEDROCK_MODEL_ID      : {model_id}")

if not access_key or not secret_key:
    print("\n❌ STOP: AWS credentials not found in .env")
    print("   Add these to your .env file:")
    print("   AWS_ACCESS_KEY_ID=AKIA...")
    print("   AWS_SECRET_ACCESS_KEY=...")
    sys.exit(1)

# ── 2. Test STS — proves the keys are valid ────────────────────────────────────

import boto3
from botocore.exceptions import ClientError

print("\n" + "=" * 60)
print("STEP 2 — STS get-caller-identity (validates AWS keys)")
print("=" * 60)

try:
    sts = boto3.client(
        "sts",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    identity = sts.get_caller_identity()
    print(f"✅ Keys are valid!")
    print(f"   Account : {identity['Account']}")
    print(f"   UserID  : {identity['UserId']}")
    print(f"   ARN     : {identity['Arn']}")
except ClientError as e:
    print(f"❌ Keys are INVALID: {e.response['Error']['Code']}: {e.response['Error']['Message']}")
    print("\n   Fix: Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in your .env")
    print("   Common issues:")
    print("   - Extra spaces around the = sign")
    print("   - Keys copied with a newline at the end")
    print("   - Keys are expired or deleted in IAM console")
    sys.exit(1)

# ── 3. Test Bedrock access ─────────────────────────────────────────────────────

print("\n" + "=" * 60)
print(f"STEP 3 — Bedrock list models in region: {bedrock_region}")
print("=" * 60)

try:
    bedrock = boto3.client(
        "bedrock",
        region_name=bedrock_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    response = bedrock.list_foundation_models(byProvider="Anthropic")
    models = [m["modelId"] for m in response.get("modelSummaries", [])]
    print(f"✅ Bedrock accessible in {bedrock_region}")
    print(f"   Available Anthropic models:")
    for m in models:
        print(f"   - {m}")
except ClientError as e:
    code = e.response["Error"]["Code"]
    print(f"❌ Bedrock not accessible: {code}: {e.response['Error']['Message']}")
    if code == "UnrecognizedClientException":
        print("\n   Fix: Add BEDROCK_REGION=us-east-1 to your .env")
        print("   (ap-south-1 may not support all Bedrock models)")
    elif code == "AccessDeniedException":
        print("\n   Fix: Your IAM user/role needs bedrock:* permissions")
        print("   Also enable model access in: AWS Console → Bedrock → Model access")
    sys.exit(1)

# ── 4. Test Bedrock invoke ─────────────────────────────────────────────────────

print("\n" + "=" * 60)
print(f"STEP 4 — Bedrock invoke: {model_id}")
print("=" * 60)

raw_model_id = model_id.removeprefix("bedrock:")

try:
    from langchain_aws import ChatBedrock
    from langchain_core.messages import HumanMessage

    llm = ChatBedrock(
        model_id=raw_model_id,
        region_name=bedrock_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        model_kwargs={"max_tokens": 64},
    )
    result = llm.invoke([HumanMessage(content="Reply with exactly: ARGOS_OK")])
    print(f"✅ Bedrock invoke works!")
    print(f"   Model response: {result.content}")

except ClientError as e:
    code = e.response["Error"]["Code"]
    msg  = e.response["Error"]["Message"]
    print(f"❌ Bedrock invoke failed: {code}: {msg}")
    if "not found" in msg.lower() or "does not exist" in msg.lower():
        print(f"\n   Fix: Model '{raw_model_id}' is not available.")
        print(f"   Use one of the model IDs printed in STEP 3 above.")
        print(f"   Also enable it in: AWS Console → Bedrock → Model access")
    elif code == "AccessDeniedException":
        print(f"\n   Fix: Enable model access in AWS Console → Bedrock → Model access")
    sys.exit(1)
except Exception as e:
    print(f"❌ Unexpected error: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("✅ ALL CHECKS PASSED — credentials and Bedrock are working")
print("=" * 60)
