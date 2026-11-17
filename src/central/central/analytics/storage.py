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
    def __init__(self, bucket: str, client=None, region: str | None = None):
        self.bucket = bucket
        self.region = region or settings.AWS_DEFAULT_REGION
        if client is None:
            import boto3  # solo hace falta en AWS

            client = boto3.client("s3", region_name=self.region)
        self.client = client
        self._ready = False

    def ensure_bucket(self) -> bool:
        """
        Crea el bucket si no existe (devuelve True si lo creó). Lo hace la central y
        no Terraform porque el Learner Lab no deja a Terraform leer la configuración
        del bucket (ver terraform/modules/analytics/main.tf). S3 cifra y bloquea el
        acceso público por defecto en los buckets nuevos.
        """
        from botocore.exceptions import ClientError

        created = False
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                raise
            kwargs = {"Bucket": self.bucket}
            if self.region != "us-east-1":  # en us-east-1 S3 no acepta LocationConstraint
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            try:
                self.client.create_bucket(**kwargs)
                created = True
            except ClientError as create_error:
                if create_error.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                    raise
        if created:
            try:  # los resultados de Athena se borran solos a los 7 días (si el laboratorio lo permite)
                self.client.put_bucket_lifecycle_configuration(Bucket=self.bucket, LifecycleConfiguration={"Rules": [{
                    "ID": "borrar-resultados-viejos-de-athena", "Status": "Enabled",
                    "Filter": {"Prefix": "athena-results/"}, "Expiration": {"Days": 7}}]})
            except ClientError:
                pass
        self._ready = True
        return created

    def put(self, key: str, body: bytes) -> None:
        if not self._ready:
            self.ensure_bucket()
        # Mismo nombre (key) = el archivo se reemplaza: subir dos veces el mismo lote no duplica datos
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/x-ndjson")

    def describe(self) -> str:
        return f"s3://{self.bucket}"


def storage_from_settings():
    if settings.EXPORT_S3_BUCKET:
        return S3Storage(settings.EXPORT_S3_BUCKET)
    return LocalStorage(settings.EXPORT_DIR)
