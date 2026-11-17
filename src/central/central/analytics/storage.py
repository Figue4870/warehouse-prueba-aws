"""Dónde se escriben los archivos exportados: S3 (en AWS) o una carpeta local (desarrollo)."""

import os
from pathlib import Path

from central.config import settings


class LocalStorage:
    """Misma estructura de carpetas que en S3: exports/movements/dt=2026-11-16/<lote>.json"""

    def __init__(self, base_dir: str):
        self.base = Path(base_dir)

    def put(self, key: str, body: bytes) -> None:
        path = self.base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)  # atómico: nunca queda un archivo a medias

    def describe(self) -> str:
        return f"carpeta local {self.base.resolve()}"


class S3Storage:
    def __init__(self, bucket: str, client=None):
        self.bucket = bucket
        if client is None:
            import boto3  # solo hace falta en AWS

            client = boto3.client("s3", region_name=settings.AWS_DEFAULT_REGION)
        self.client = client

    def put(self, key: str, body: bytes) -> None:
        # Mismo nombre (key) = el archivo se reemplaza: subir dos veces el mismo lote no duplica datos
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/x-ndjson")

    def describe(self) -> str:
        return f"s3://{self.bucket}"


def storage_from_settings():
    if settings.EXPORT_S3_BUCKET:
        return S3Storage(settings.EXPORT_S3_BUCKET)
    return LocalStorage(settings.EXPORT_DIR)
