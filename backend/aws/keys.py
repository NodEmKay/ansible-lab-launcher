import json
import os
import shutil
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from backend.aws.client import get_session


KEY_DIR = "/data/keys"
ARCHIVE_DIR = f"{KEY_DIR}/archive"


def _paths(key_name):
    return {
        "pem": f"{KEY_DIR}/{key_name}.pem",
        "metadata": f"{KEY_DIR}/{key_name}.json",
    }


def _aws_context():
    session = get_session()

    region = session.region_name
    if not region:
        return {
            "status": "error",
            "message": "AWS region is not configured",
        }

    sts = session.client("sts", region_name=region)
    identity = sts.get_caller_identity()

    return {
        "status": "ready",
        "session": session,
        "region": region,
        "account_id": identity["Account"],
    }


def _aws_key_exists(ec2, key_name):
    response = ec2.describe_key_pairs(
        Filters=[
            {
                "Name": "key-name",
                "Values": [key_name],
            }
        ]
    )

    return bool(response.get("KeyPairs"))


def _read_metadata(path):
    try:
        with open(path, "r") as metadata_file:
            return json.load(metadata_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _archive_local_key(key_name):
    paths = _paths(key_name)

    os.makedirs(ARCHIVE_DIR, mode=0o700, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    archived = []

    for source, suffix in (
        (paths["pem"], ".pem"),
        (paths["metadata"], ".json"),
    ):
        if os.path.exists(source):
            destination = os.path.join(
                ARCHIVE_DIR,
                f"{key_name}-{timestamp}{suffix}",
            )
            shutil.move(source, destination)

            if suffix == ".pem":
                os.chmod(destination, 0o600)

            archived.append(destination)

    return archived


def _create_key_pair(ec2, key_name, account_id, region):
    paths = _paths(key_name)

    response = ec2.create_key_pair(
        KeyName=key_name,
        KeyType="ed25519",
    )

    try:
        with open(paths["pem"], "x") as key_file:
            key_file.write(response["KeyMaterial"])

        os.chmod(paths["pem"], 0o600)

        metadata = {
            "key_name": key_name,
            "aws_account_id": account_id,
            "region": region,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(paths["metadata"], "x") as metadata_file:
            json.dump(metadata, metadata_file, indent=2)

        os.chmod(paths["metadata"], 0o600)

    except Exception:
        # The AWS private key cannot be retrieved later.
        # Roll back the AWS key if local persistence failed.
        try:
            ec2.delete_key_pair(KeyName=key_name)
        except Exception:
            pass

        for path in paths.values():
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        raise

    return {
        "status": "ready",
        "action": "created",
        "key_name": key_name,
        "region": region,
    }


def reconcile_key(key_name="ansible-lab-key"):
    """
    Ensure the current AWS sandbox and local persistent storage
    contain a usable matching SSH key pair.
    """

    if not key_name.replace("-", "").replace("_", "").isalnum():
        return {
            "status": "error",
            "message": "Invalid SSH key name",
        }

    try:
        os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)

        context = _aws_context()
        if context["status"] != "ready":
            return context

        session = context["session"]
        region = context["region"]
        account_id = context["account_id"]

        ec2 = session.client("ec2", region_name=region)

        paths = _paths(key_name)

        aws_exists = _aws_key_exists(ec2, key_name)
        pem_exists = os.path.exists(paths["pem"])
        metadata = _read_metadata(paths["metadata"])

        metadata_matches = bool(
            metadata
            and metadata.get("aws_account_id") == account_id
            and metadata.get("region") == region
            and metadata.get("key_name") == key_name
        )

        # Fully known, matching pair.
        if aws_exists and pem_exists and metadata_matches:
            os.chmod(paths["pem"], 0o600)

            return {
                "status": "ready",
                "action": "existing",
                "key_name": key_name,
                "region": region,
            }

        # Migration case:
        # AWS + PEM exist, but this predates metadata support.
        # We cannot cryptographically prove they match here, so do
        # not silently trust the pair. Rotate it safely.
        if aws_exists:
            ec2.delete_key_pair(KeyName=key_name)

        if pem_exists or os.path.exists(paths["metadata"]):
            _archive_local_key(key_name)

        return _create_key_pair(
            ec2=ec2,
            key_name=key_name,
            account_id=account_id,
            region=region,
        )

    except (NoCredentialsError, BotoCoreError, ClientError) as exc:
        return {
            "status": "error",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"SSH key reconciliation failed: {exc}",
        }
