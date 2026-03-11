"""DynamoDB-backed LangGraph checkpoint saver.

Implements BaseCheckpointSaver so LangGraph can persist full graph state
between nodes. Each alert run is isolated by thread_id.

DynamoDB table schema (single table, two item types):
  PK  thread_id   String  — e.g. "123456789012#alert-id-1234"
  SK  record_id   String  — "ckpt#{checkpoint_uuid}"
                          — "write#{checkpoint_uuid}#{task_id}#{idx}"

Create table once with: python utils/dynamodb_checkpointer.py --create
"""

import base64
import os
from typing import Any, Iterator, Optional, Sequence

import boto3
import structlog
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

load_dotenv()
log = structlog.get_logger(__name__)


class DynamoDBSaver(BaseCheckpointSaver):
    """LangGraph checkpoint saver backed by Amazon DynamoDB."""

    def __init__(self, table_name: str, region: str | None = None) -> None:
        super().__init__()
        self.table_name = table_name
        self.region = region or os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
        dynamodb = boto3.resource(
            "dynamodb",
            region_name=self.region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        self.table = dynamodb.Table(table_name)
        log.info("dynamodb_checkpointer_ready", table=table_name, region=self.region)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_tuple(self, config: dict) -> CheckpointTuple | None:
        """Fetch the latest (or specific) checkpoint for a thread."""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id")

        item = (
            self._get_item(thread_id, f"ckpt#{checkpoint_id}")
            if checkpoint_id
            else self._get_latest_checkpoint(thread_id)
        )
        if not item:
            return None
        return self._item_to_tuple(item, thread_id)

    def list(
        self,
        config: Optional[dict],
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """Yield checkpoints for a thread, newest first."""
        if not config:
            return
        thread_id = config["configurable"]["thread_id"]
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("thread_id").eq(thread_id)
                & Key("record_id").begins_with("ckpt#")
            ),
            "ScanIndexForward": False,
        }
        if limit:
            kwargs["Limit"] = limit

        try:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                yield self._item_to_tuple(item, thread_id)
        except ClientError as e:
            log.error("dynamodb_list_failed", thread_id=thread_id,
                      error=e.response["Error"]["Message"])

    # ── Write ─────────────────────────────────────────────────────────────────

    def put(
        self,
        config: dict,
        checkpoint: dict,
        metadata: dict,
        new_versions: dict,
    ) -> dict:
        """Persist a checkpoint. Returns the config pointing to this checkpoint."""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id", "")

        ckpt_type, ckpt_bytes = self.serde.dumps_typed(checkpoint)
        meta_type, meta_bytes = self.serde.dumps_typed(metadata)

        try:
            self.table.put_item(Item={
                "thread_id": thread_id,
                "record_id": f"ckpt#{checkpoint_id}",
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": parent_id,
                "checkpoint_type": ckpt_type,
                "checkpoint_data": base64.b64encode(ckpt_bytes).decode(),
                "metadata_type": meta_type,
                "metadata_data": base64.b64encode(meta_bytes).decode(),
            })
            log.debug("dynamodb_checkpoint_saved", thread_id=thread_id,
                      checkpoint_id=checkpoint_id)
        except ClientError as e:
            log.error("dynamodb_put_failed", thread_id=thread_id,
                      checkpoint_id=checkpoint_id, error=e.response["Error"]["Message"])

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """Persist intermediate node writes for recovery."""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id", "")

        for idx, (channel, value) in enumerate(writes):
            write_type, write_bytes = self.serde.dumps_typed(value)
            try:
                self.table.put_item(Item={
                    "thread_id": thread_id,
                    "record_id": f"write#{checkpoint_id}#{task_id}#{idx}",
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "channel": channel,
                    "write_type": write_type,
                    "write_data": base64.b64encode(write_bytes).decode(),
                })
            except ClientError as e:
                log.error("dynamodb_write_failed", thread_id=thread_id,
                          channel=channel, error=e.response["Error"]["Message"])

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_item(self, thread_id: str, record_id: str) -> dict | None:
        """Fetch a single DynamoDB item by primary key."""
        try:
            response = self.table.get_item(
                Key={"thread_id": thread_id, "record_id": record_id}
            )
            return response.get("Item")
        except ClientError as e:
            log.error("dynamodb_get_item_failed", thread_id=thread_id,
                      error=e.response["Error"]["Message"])
            return None

    def _get_latest_checkpoint(self, thread_id: str) -> dict | None:
        """Query for the most recent checkpoint of a thread."""
        try:
            response = self.table.query(
                KeyConditionExpression=(
                    Key("thread_id").eq(thread_id)
                    & Key("record_id").begins_with("ckpt#")
                ),
                ScanIndexForward=False,
                Limit=1,
            )
            items = response.get("Items", [])
            return items[0] if items else None
        except ClientError as e:
            log.error("dynamodb_query_failed", thread_id=thread_id,
                      error=e.response["Error"]["Message"])
            return None

    def _get_pending_writes(self, thread_id: str, checkpoint_id: str) -> list[tuple[str, str, Any]]:
        """Fetch intermediate writes associated with a checkpoint."""
        try:
            response = self.table.query(
                KeyConditionExpression=(
                    Key("thread_id").eq(thread_id)
                    & Key("record_id").begins_with(f"write#{checkpoint_id}#")
                ),
            )
            writes = []
            for item in response.get("Items", []):
                value = self.serde.loads_typed((
                    item["write_type"],
                    base64.b64decode(item["write_data"]),
                ))
                writes.append((item["task_id"], item["channel"], value))
            return writes
        except ClientError:
            return []

    def _item_to_tuple(self, item: dict, thread_id: str) -> CheckpointTuple:
        """Convert a DynamoDB item into a CheckpointTuple."""
        checkpoint = self.serde.loads_typed((
            item["checkpoint_type"],
            base64.b64decode(item["checkpoint_data"]),
        ))
        metadata = self.serde.loads_typed((
            item["metadata_type"],
            base64.b64decode(item["metadata_data"]),
        ))
        checkpoint_id = item["checkpoint_id"]
        parent_id = item.get("parent_checkpoint_id", "")

        config_out = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }
        parent_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": parent_id,
            }
        } if parent_id else None

        pending_writes = self._get_pending_writes(thread_id, checkpoint_id)

        return CheckpointTuple(
            config=config_out,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )


# ── Table creation helper ─────────────────────────────────────────────────────


def create_table(table_name: str, region: str) -> None:
    """Create the DynamoDB checkpoint table. Run once during setup.

    Raises if the table already exists (safe to ignore that error).
    """
    client = boto3.client(
        "dynamodb",
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    try:
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "thread_id", "KeyType": "HASH"},
                {"AttributeName": "record_id",  "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "thread_id", "AttributeType": "S"},
                {"AttributeName": "record_id",  "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Project", "Value": "Argos"},
                {"Key": "ManagedBy", "Value": "argos-checkpointer"},
            ],
        )
        print(f"Creating table '{table_name}' in {region}...")
        client.get_waiter("table_exists").wait(TableName=table_name)
        print(f"✅ Table '{table_name}' is ready.")
    except client.exceptions.ResourceInUseException:
        print(f"ℹ️  Table '{table_name}' already exists — nothing to do.")
    except ClientError as e:
        print(f"❌ Failed: {e.response['Error']['Code']}: {e.response['Error']['Message']}")
        raise


if __name__ == "__main__":
    import sys
    if "--create" in sys.argv:
        _table = os.environ.get("DYNAMODB_CHECKPOINT_TABLE", "argos-checkpoints")
        _region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
        create_table(_table, _region)
    else:
        print("Usage: python utils/dynamodb_checkpointer.py --create")
