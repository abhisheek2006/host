"""Cloudflare R2 storage - upload, download, delete."""

import uuid
import boto3
from config import R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_REGION, logger


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name=R2_REGION,
    )


def r2_upload(user_id: int, bot_id: int, filename: str, data: bytes) -> str:
    safe = filename.replace("/", "_").replace("\\", "_")
    key = f"users/{user_id}/{bot_id}/project.zip"
    _s3_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data)
    logger.info("R2 upload user=%s bot=%d key=%s", user_id, bot_id, key)
    return key


def r2_download(key: str) -> bytes:
    resp = _s3_client().get_object(Bucket=R2_BUCKET, Key=key)
    return resp["Body"].read()


def r2_delete(key: str) -> None:
    _s3_client().delete_object(Bucket=R2_BUCKET, Key=key)
    logger.info("R2 delete key=%s", key)
