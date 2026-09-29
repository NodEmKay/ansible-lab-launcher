import boto3
import ipaddress
import urllib.request
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from fastapi import FastAPI
from fastapi.responses import FileResponse
from backend.aws.keys import reconcile_key
from backend.aws.workflow import get_workflow_status, validate_ansible, prepare_rh294
from backend.aws.bootstrap import bootstrap_workstation, get_bootstrap_status
from backend.aws.labs import (
    discover_environment,
    get_destroy_plan,
    build_lab,
    destroy_lab,
)

app = FastAPI(
    title="Ansible Lab Launcher",
    description="Beginner-friendly AWS Ansible lab provisioning platform",
    version="0.1.0",
)


@app.get("/")
def home():
    return {
        "application": "Ansible Lab Launcher",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/aws/status")
def aws_status():
    try:
        session = boto3.Session()
        sts = session.client("sts")

        identity = sts.get_caller_identity()

        return {
            "status": "connected",
            "region": session.region_name,
        }

    except (NoCredentialsError, BotoCoreError, ClientError) as exc:
        print("AWS status check failed:", repr(exc))
        return {
            "status": "disconnected",
            "message": "Unable to access the AWS environment.",
        }


@app.get("/api/aws/environment")
def aws_environment():
    try:
        session = boto3.Session()
        region = session.region_name

        ec2 = session.client("ec2", region_name=region)

        key_response = ec2.describe_key_pairs()

        key_pairs = sorted(
            key["KeyName"]
            for key in key_response.get("KeyPairs", [])
        )

        return {
            "status": "connected",
            "region": region,
            "key_pairs": key_pairs,
        }

    except (NoCredentialsError, BotoCoreError, ClientError) as exc:
        print("AWS environment discovery failed:", repr(exc))
        return {
            "status": "error",
            "message": "Unable to discover the AWS environment.",
        }


@app.get("/ui")
def ui():
    return FileResponse("backend/static/index.html")


@app.post("/api/aws/keys/{key_name}")
def create_ssh_key(key_name: str):
    import os

    try:
        if not key_name.replace("-", "").replace("_", "").isalnum():
            return {
                "status": "error",
                "message": "Key name may contain only letters, numbers, hyphens and underscores",
            }

        key_path = f"/data/keys/{key_name}.pem"

        # Never create a new AWS key if a local PEM with the same
        # name already exists. This prevents mismatched key pairs.
        if os.path.exists(key_path):
            return {
                "status": "error",
                "message": "Local SSH private key already exists",
            }

        session = boto3.Session()
        ec2 = session.client("ec2", region_name=session.region_name)

        existing = ec2.describe_key_pairs(
            Filters=[
                {
                    "Name": "key-name",
                    "Values": [key_name],
                }
            ]
        )

        if existing.get("KeyPairs"):
            return {
                "status": "error",
                "message": "SSH key already exists in AWS",
            }

        response = ec2.create_key_pair(
            KeyName=key_name,
            KeyType="ed25519",
        )

        try:
            with open(key_path, "x") as key_file:
                key_file.write(response["KeyMaterial"])

            os.chmod(key_path, 0o600)

        except Exception:
            # Do not leave an AWS key pair behind when its
            # corresponding private key could not be persisted.
            try:
                ec2.delete_key_pair(KeyName=key_name)
            except Exception:
                pass
            raise

        return {
            "status": "created",
            "key_name": key_name,
            "region": session.region_name,
        }

    except (NoCredentialsError, BotoCoreError, ClientError) as exc:
        print("SSH key operation failed:", repr(exc))
        return {
            "status": "error",
            "message": "Unable to complete the SSH key operation.",
        }


@app.get("/api/aws/lab-environment")
def lab_environment():
    return discover_environment()

@app.get("/api/labs")
def get_lab():
    """Return the current Launcher-managed lab state."""
    return get_destroy_plan()


@app.post("/api/labs")
def create_complete_lab(
    ami_id: str,
    allowed_ssh_cidr: str,
):
    """Build the complete Launcher-managed AWS lab."""
    from backend.aws.operations import (
        acquire_operation,
        busy_response,
        release_operation,
    )

    operation = acquire_operation("build")

    if not operation["acquired"]:
        return busy_response(operation["operation"])

    try:
        key = reconcile_key("ansible-lab-key")

        if key.get("status") != "ready":
            return {
                "status": "failed",
                "stage": "ssh_key",
                "message": "SSH key preparation failed.",
            }

        return build_lab(
            ami_id=ami_id,
            key_name=key["key_name"],
            allowed_ssh_cidr=allowed_ssh_cidr,
        )

    finally:
        release_operation("build")


@app.delete("/api/labs")
def delete_lab():
    """Destroy the Launcher-managed AWS lab."""
    from backend.aws.operations import (
        acquire_operation,
        busy_response,
        release_operation,
    )

    operation = acquire_operation("destroy")

    if not operation["acquired"]:
        return busy_response(operation["operation"])

    try:
        return destroy_lab()
    finally:
        release_operation("destroy")

@app.get("/api/network/my-ip")
def get_my_public_ip():
    """Return the Launcher's current public IPv4 address as a /32 CIDR."""
    try:
        with urllib.request.urlopen(
            "https:" + "//checkip.amazonaws.com",
            timeout=5,
        ) as response:
            public_ip = response.read().decode().strip()

        address = ipaddress.ip_address(public_ip)

        if address.version != 4:
            return {
                "status": "failed",
                "message": "Public IPv4 address was not detected",
            }

        return {
            "status": "ready",
            "cidr": f"{address}/32",
        }

    except Exception:
        return {
            "status": "failed",
            "message": "Unable to detect public IPv4 address",
        }



@app.get("/api/aws/context")
def aws_context_status():
    """Reconcile Launcher SSH key with the current AWS context."""
    result = reconcile_key("ansible-lab-key")

    if result.get("status") != "ready":
        return {
            "status": "error",
            "ssh_key": "unavailable",
            "message": result.get(
                "message",
                "AWS context reconciliation failed",
            ),
        }

    return {
        "status": "ready",
        "ssh_key": "ready",
        "key_action": result.get("action"),
        "region": result.get("region"),
    }


@app.post("/api/labs/bootstrap")
def bootstrap_lab_workstation():
    """Prepare the Ansible workstation for the current lab."""
    return bootstrap_workstation()


@app.get("/api/labs/bootstrap")
def bootstrap_lab_status():
    """Return the current workstation bootstrap status."""
    return get_bootstrap_status()

@app.get("/api/labs/workflow")
def lab_workflow_status():
    """Return current RH294 workflow state without running Ansible."""
    return get_workflow_status(run_ansible_validation=False)


@app.get("/api/labs/prepare/status")
def prepare_rh294_status():
    """Return lightweight RH294 preparation job progress."""
    from backend.aws.workflow import get_prepare_job_status
    return get_prepare_job_status()


@app.post("/api/labs/prepare")
def prepare_rh294_lab():
    """Start or resume Launcher-managed RH294 preparation."""
    from backend.aws.workflow import start_prepare_rh294_job
    return start_prepare_rh294_job()


@app.post("/api/labs/validate")
def validate_rh294_lab():
    """Run the functional RH294 Ansible validation."""
    return validate_ansible()
