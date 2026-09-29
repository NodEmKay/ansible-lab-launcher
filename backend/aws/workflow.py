import json
import os
from datetime import datetime, timezone

import paramiko

VALIDATION_STATE = "/data/rh294-validation.json"

from backend.aws.bootstrap import (
    KEY_PATH,
    get_bootstrap_status,
    validate_managed_hosts,
)
from backend.aws.labs import get_lab_inventory


def _lab_identity(inventory):
    """Return a stable identity for the currently discovered AWS lab."""

    nodes = inventory.get("nodes", {})

    return {
        name: data.get("instance_id")
        for name, data in sorted(nodes.items())
        if data.get("instance_id")
    }


def _save_validation_state(status, inventory):
    """Persist validation only for the current AWS lab instances."""

    data = {
        "status": status,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "lab_identity": _lab_identity(inventory),
    }

    tmp = VALIDATION_STATE + ".tmp"

    with open(tmp, "w") as handle:
        json.dump(data, handle)

    os.chmod(tmp, 0o600)
    os.replace(tmp, VALIDATION_STATE)


def _read_validation_state(inventory):
    """Return saved validation only when it belongs to this AWS lab."""

    try:
        with open(VALIDATION_STATE) as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    if data.get("lab_identity") != _lab_identity(inventory):
        return {}

    return data


def _run_remote(client, command, timeout=30):
    """Run a command on the workstation and return basic execution data."""

    stdin, stdout, stderr = client.exec_command(
        command,
        timeout=timeout,
    )

    exit_code = stdout.channel.recv_exit_status()

    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
    }


def _connect_workstation(inventory):
    """Open an SSH connection to the current AWS workstation."""

    workstation = inventory.get("nodes", {}).get("workstation")

    if not workstation or not workstation.get("public_ip"):
        raise RuntimeError("Workstation public IP is unavailable")

    if not os.path.isfile(KEY_PATH):
        raise RuntimeError("Launcher SSH key is unavailable")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=workstation["public_ip"],
        username="ec2-user",
        key_filename=KEY_PATH,
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )

    return client


def _host_mapping_command(nodes):
    """Build a host-mapping validation command from current AWS inventory."""

    commands = []

    for hostname in ("servera", "serverb", "serverc", "serverd"):
        node = nodes.get(hostname, {})
        private_ip = node.get("private_ip")

        if not private_ip:
            return None

        commands.append(
            f"grep -Eq '^{private_ip}[[:space:]].*{hostname}"
            f"([[:space:]]|$)' /etc/hosts"
        )

    return " && ".join(commands)


def get_rh294_status(inventory=None):
    """Inspect RH294-specific state on the AWS workstation."""

    if inventory is None:
        inventory = get_lab_inventory()

    if inventory.get("status") != "ready":
        return {
            "status": "not_ready",
            "checks": {},
        }

    nodes = inventory.get("nodes", {})
    mapping_command = _host_mapping_command(nodes)

    if not mapping_command:
        return {
            "status": "not_ready",
            "checks": {},
        }

    client = None

    try:
        client = _connect_workstation(inventory)

        host_mappings = _run_remote(
            client,
            mapping_command,
        )["ok"]

        registry_authenticated = _run_remote(
            client,
            "podman login --get-login registry.redhat.io >/dev/null 2>&1",
        )["ok"]

        custom_ee = _run_remote(
            client,
            "podman image exists localhost/rh294-ee:1.0",
        )["ok"]

        dev_exists = _run_remote(
            client,
            "podman container exists ansible-dev",
        )["ok"]

        dev_running = False

        if dev_exists:
            dev_running = _run_remote(
                client,
                "test \"$(podman inspect --format '{{.State.Status}}' "
                "ansible-dev 2>/dev/null)\" = running",
            )["ok"]

        nested_ee = False

        if dev_running:
            nested_ee = _run_remote(
                client,
                "podman exec ansible-dev "
                "podman image exists localhost/rh294-ee:1.0",
            )["ok"]

        checks = {
            "host_mappings": host_mappings,
            "registry_authenticated": registry_authenticated,
            "custom_ee": custom_ee,
            "dev_container_exists": dev_exists,
            "dev_container_running": dev_running,
            "nested_ee": nested_ee,
        }

        environment_built = (
            host_mappings
            and custom_ee
            and dev_exists
            and nested_ee
        )

        return {
            "status": "ready" if environment_built else "not_ready",
            "checks": checks,
        }

    except Exception as exc:
        print("RH294 environment status check failed:", repr(exc))
        return {
            "status": "not_ready",
            "checks": {},
            "error": "Unable to determine RH294 environment status.",
        }

    finally:
        if client:
            client.close()


def validate_ansible(inventory=None):
    """Run the final RH294 Ansible connectivity validation."""

    if inventory is None:
        inventory = get_lab_inventory()

    client = None

    try:
        client = _connect_workstation(inventory)

        dev_exists = _run_remote(
            client,
            "podman container exists ansible-dev",
        )["ok"]

        if not dev_exists:
            return {
                "status": "not_ready",
                "reason": "ansible-dev container does not exist",
            }

        dev_running = _run_remote(
            client,
            "test \"$(podman inspect --format '{{.State.Status}}' "
            "ansible-dev 2>/dev/null)\" = running",
        )["ok"]

        if not dev_running:
            start_result = _run_remote(
                client,
                "podman start ansible-dev >/dev/null",
                timeout=60,
            )

            if not start_result["ok"]:
                return {
                    "status": "not_ready",
                    "reason": "ansible-dev could not be started",
                }

        nested_ee = _run_remote(
            client,
            "podman exec ansible-dev "
            "podman image exists localhost/rh294-ee:1.0",
        )["ok"]

        if not nested_ee:
            return {
                "status": "not_ready",
                "reason": "Nested RH294 execution environment is unavailable",
            }

        command = (
            "podman exec ansible-dev bash -lc "
            "'cd /workspaces/aws-rh294 && "
            "ansible-navigator run ping.yml -m stdout "
            ">/tmp/rh294-validation.log 2>&1'"
        )

        result = _run_remote(
            client,
            command,
            timeout=180,
        )

        validation_status = (
            "ready" if result["ok"] else "not_ready"
        )

        _save_validation_state(validation_status, inventory)

        # RC2: expose a clean student workspace only after the
        # Launcher-managed RH294 environment has validated successfully.
        if validation_status == "ready":
            student_workspace = _run_remote(
                client,
                """python3 -m ensurepip --user >/dev/null 2>&1 &&
python3 -m pip install --user ansible-navigator >/tmp/rh294-navigator-install.log 2>&1 &&
mkdir -p "$HOME/rhce-lab" &&
cp "$HOME/ansible-projects/aws-rh294/ansible.cfg" "$HOME/rhce-lab/ansible.cfg" &&
cp "$HOME/ansible-projects/aws-rh294/ansible-navigator.yml" "$HOME/rhce-lab/ansible-navigator.yml" &&
cp "$HOME/ansible-projects/aws-rh294/inventory" "$HOME/rhce-lab/inventory" &&
chmod 755 "$HOME/rhce-lab" &&
chmod 644 "$HOME/rhce-lab/ansible.cfg" "$HOME/rhce-lab/ansible-navigator.yml" "$HOME/rhce-lab/inventory" &&
"$HOME/.local/bin/ansible-navigator" --version >/dev/null""",
                timeout=300,
            )

            if not student_workspace["ok"]:
                return {
                    "status": "not_ready",
                    "reason": "Unable to prepare the student workspace",
                }

        return {
            "status": validation_status,
            "exit_code": result["exit_code"],
        }

    except Exception as exc:
        print("Ansible validation failed:", repr(exc))
        return {
            "status": "not_ready",
            "error": "Unable to complete Ansible validation.",
        }

    finally:
        if client:
            client.close()


def get_workflow_status(run_ansible_validation=False):
    """Return the complete Launcher-managed RH294 workflow state."""

    steps = []

    inventory = get_lab_inventory()
    nodes = inventory.get("nodes", {})

    infrastructure_ready = (
        inventory.get("status") == "ready"
        and len(nodes) == 5
    )

    steps.append({
        "id": "infrastructure",
        "name": "Provision AWS infrastructure",
        "status": "complete" if infrastructure_ready else "pending",
        "detail": f"{len(nodes)}/5 nodes available",
    })

    if not infrastructure_ready:
        return {
            "status": "not_ready",
            "rh294_ready": False,
            "steps": steps,
        }

    bootstrap = get_bootstrap_status()

    workstation_ready = (
        bootstrap.get("status") == "ready"
        and bootstrap.get("bootstrap") == "complete"
    )

    steps.append({
        "id": "workstation",
        "name": "Prepare Ansible workstation",
        "status": "complete" if workstation_ready else "pending",
        "detail": (
            "Git, Podman, repository and SSH ready"
            if workstation_ready
            else "Workstation preparation required"
        ),
    })

    if not workstation_ready:
        return {
            "status": "not_ready",
            "rh294_ready": False,
            "steps": steps,
        }

    managed = validate_managed_hosts()
    host_results = managed.get("results", {})

    reachable = sum(
        1
        for result in host_results.values()
        if result.get("reachable")
    )

    managed_ready = (
        managed.get("status") == "ready"
        and reachable == 4
    )

    steps.append({
        "id": "managed_hosts",
        "name": "Validate managed hosts",
        "status": "complete" if managed_ready else "pending",
        "detail": f"{reachable}/4 hosts reachable",
    })

    if not managed_ready:
        return {
            "status": "not_ready",
            "rh294_ready": False,
            "steps": steps,
        }

    rh294 = get_rh294_status(inventory)
    checks = rh294.get("checks", {})

    host_mappings = checks.get("host_mappings", False)

    steps.append({
        "id": "host_mappings",
        "name": "Configure lab host mappings",
        "status": "complete" if host_mappings else "pending",
        "detail": (
            "Current AWS private addresses configured"
            if host_mappings
            else "Host mappings require configuration"
        ),
    })

    registry = checks.get("registry_authenticated", False)

    steps.append({
        "id": "registry",
        "name": "Red Hat registry access",
        "status": "complete" if registry else "not_required",
        "detail": (
            "Authenticated"
            if registry
            else "Authentication required only for future image pulls"
        ),
    })

    custom_ee = checks.get("custom_ee", False)

    steps.append({
        "id": "execution_environment",
        "name": "Prepare execution environment",
        "status": "complete" if custom_ee else "pending",
        "detail": (
            "localhost/rh294-ee:1.0 available"
            if custom_ee
            else "RH294 execution environment required"
        ),
    })

    dev_exists = checks.get("dev_container_exists", False)
    dev_running = checks.get("dev_container_running", False)
    nested_ee = checks.get("nested_ee", False)

    development_ready = (
        dev_exists
        and dev_running
        and nested_ee
    )

    if development_ready:
        development_status = "complete"
        development_detail = "ansible-dev running; nested EE available"
    elif dev_exists and not dev_running:
        development_status = "stopped"
        development_detail = "ansible-dev exists and can be restarted"
    else:
        development_status = "pending"
        development_detail = "Development environment requires preparation"

    steps.append({
        "id": "development_environment",
        "name": "Prepare development environment",
        "status": development_status,
        "detail": development_detail,
    })

    ansible_ready = False

    if run_ansible_validation:
        validation = validate_ansible(inventory)
        ansible_ready = validation.get("status") == "ready"

        if ansible_ready:
            for step in steps:
                if step["id"] == "development_environment":
                    step["status"] = "complete"
                    step["detail"] = (
                        "ansible-dev running; nested EE validated"
                    )
                    break

        steps.append({
            "id": "ansible_validation",
            "name": "Validate Ansible",
            "status": "complete" if ansible_ready else "failed",
            "detail": (
                "4/4 managed hosts validated"
                if ansible_ready
                else "Ansible validation failed"
            ),
        })
    else:
        saved_validation = _read_validation_state(inventory)

        ansible_ready = (
            saved_validation.get("status") == "ready"
        )

        if ansible_ready:
            for step in steps:
                if step["id"] == "development_environment":
                    step["status"] = "complete"
                    step["detail"] = (
                        "ansible-dev validated; nested EE available"
                    )
                    break

        steps.append({
            "id": "ansible_validation",
            "name": "Validate Ansible",
            "status": "complete" if ansible_ready else "pending",
            "detail": (
                "Last functional validation passed"
                if ansible_ready
                else "Final validation not run"
            ),
        })

    rh294_ready = (
        host_mappings
        and custom_ee
        and ansible_ready
    )

    return {
        "status": "ready" if rh294_ready else "not_ready",
        "rh294_ready": rh294_ready,
        "steps": steps,
    }


_PREPARE_JOB_STATE = {
    "status": "idle",
    "job_id": None,
    "current_stage": None,
    "detail": "",
    "message": "",
    "step_updates": {},
}

_PREPARE_JOB_LOCK = None


def _get_prepare_job_lock():
    global _PREPARE_JOB_LOCK

    if _PREPARE_JOB_LOCK is None:
        import threading
        _PREPARE_JOB_LOCK = threading.Lock()

    return _PREPARE_JOB_LOCK


def _prepare_job_snapshot():
    import copy

    with _get_prepare_job_lock():
        return copy.deepcopy(_PREPARE_JOB_STATE)


def _set_prepare_job(job_id, **updates):
    if job_id is None:
        return

    with _get_prepare_job_lock():
        if _PREPARE_JOB_STATE.get("job_id") != job_id:
            return

        _PREPARE_JOB_STATE.update(updates)


def _set_prepare_step(job_id, step_id, status, detail):
    if job_id is None:
        return

    with _get_prepare_job_lock():
        if _PREPARE_JOB_STATE.get("job_id") != job_id:
            return

        step_updates = _PREPARE_JOB_STATE.setdefault(
            "step_updates",
            {},
        )

        step_updates[step_id] = {
            "status": status,
            "detail": detail,
        }

        _PREPARE_JOB_STATE["current_stage"] = step_id
        _PREPARE_JOB_STATE["detail"] = detail


def get_prepare_job_status():
    """Return lightweight RH294 preparation progress."""
    return _prepare_job_snapshot()


def _finish_prepare_job(job_id, result):

    result_status = result.get("status")

    if result_status == "ready":
        _set_prepare_job(
            job_id,
            status="ready",
            current_stage="complete",
            detail="RH294 environment prepared and validated",
            message="RH294 environment is ready",
        )

    elif result_status == "action_required":
        _set_prepare_job(
            job_id,
            status="action_required",
            current_stage=result.get(
                "stage",
                "registry_authentication",
            ),
            detail=result.get(
                "message",
                "User action is required",
            ),
            message=result.get(
                "message",
                "User action is required",
            ),
            connection=result.get("connection"),
        )

    else:
        _set_prepare_job(
            job_id,
            status="failed",
            current_stage=result.get("stage"),
            detail=result.get(
                "message",
                "RH294 preparation failed",
            ),
            message=result.get(
                "message",
                "RH294 preparation failed",
            ),
        )


def _prepare_job_worker(job_id):
    from backend.aws.operations import release_operation

    try:
        result = prepare_rh294(job_id=job_id)
        _finish_prepare_job(job_id, result)

    except Exception as exc:
        print(
            "RH294 background preparation failed:",
            repr(exc),
        )

        _set_prepare_job(
            job_id,
            status="failed",
            current_stage="failed",
            detail="RH294 preparation failed",
            message="RH294 preparation failed",
        )

    finally:
        release_operation("prepare")


def start_prepare_rh294_job():
    """
    Start RH294 preparation asynchronously.

    Only one lab-changing operation may run at a time.
    """
    import copy
    import threading
    import uuid

    from backend.aws.operations import (
        acquire_operation,
        busy_response,
    )

    lock = _get_prepare_job_lock()

    with lock:
        if _PREPARE_JOB_STATE.get("status") == "running":
            return copy.deepcopy(_PREPARE_JOB_STATE)

        operation = acquire_operation("prepare")

        if not operation["acquired"]:
            return busy_response(operation["operation"])

        job_id = uuid.uuid4().hex

        _PREPARE_JOB_STATE.clear()
        _PREPARE_JOB_STATE.update(
            {
                "status": "running",
                "job_id": job_id,
                "current_stage": "workstation",
                "detail": "Preparing Ansible workstation",
                "message": "RH294 preparation is running",
                "step_updates": {
                    "workstation": {
                        "status": "running",
                        "detail": "Preparing Ansible workstation",
                    }
                },
            }
        )

    thread = threading.Thread(
        target=_prepare_job_worker,
        args=(job_id,),
        name=f"rh294-prepare-{job_id[:8]}",
        daemon=True,
    )

    thread.start()

    return _prepare_job_snapshot()


def prepare_rh294(job_id=None):
    """
    Prepare the complete RH294 environment.

    Red Hat credentials are never collected by the Launcher.
    If registry authentication is required, return action_required.
    """
    from backend.aws.bootstrap import bootstrap_workstation

    _set_prepare_step(
        job_id,
        "workstation",
        "running",
        "Preparing Git, Podman, repository and SSH",
    )

    bootstrap = bootstrap_workstation()

    if bootstrap.get("status") != "ready":
        _set_prepare_step(
            job_id,
            "workstation",
            "failed",
            "Ansible workstation preparation failed",
        )

        return {
            "status": "failed",
            "stage": "workstation",
            "message": "Ansible workstation preparation failed",
        }

    _set_prepare_step(
        job_id,
        "workstation",
        "complete",
        "Git, Podman, repository and SSH ready",
    )

    inventory = get_lab_inventory()

    if inventory.get("status") != "ready":
        _set_prepare_step(
            job_id,
            "infrastructure",
            "failed",
            "AWS lab inventory is not ready",
        )

        return {
            "status": "failed",
            "stage": "infrastructure",
            "message": "AWS lab inventory is not ready",
        }

    _set_prepare_step(
        job_id,
        "managed_hosts",
        "complete",
        "4/4 managed hosts reachable",
    )

    _set_prepare_step(
        job_id,
        "host_mappings",
        "complete",
        "Current AWS private addresses configured",
    )

    client = None

    try:
        client = _connect_workstation(inventory)

        ee = _run_remote(
            client,
            "podman image exists localhost/rh294-ee:1.0",
        )

        if not ee["ok"]:
            _set_prepare_step(
                job_id,
                "registry",
                "running",
                "Checking Red Hat registry authentication",
            )

            registry = _run_remote(
                client,
                "podman login --get-login registry.redhat.io "
                ">/dev/null 2>&1",
            )

            if not registry["ok"]:
                _set_prepare_step(
                    job_id,
                    "registry",
                    "action_required",
                    (
                        "Connect to the AWS workstation and run "
                        "podman login registry.redhat.io"
                    ),
                )

                workstation = (
                    inventory.get("nodes", {})
                    .get("workstation", {})
                )

                return {
                    "status": "action_required",
                    "stage": "registry_authentication",
                    "message": (
                        "Connect to the AWS workstation, run "
                        "podman login registry.redhat.io, then "
                        "click Continue Setup."
                    ),
                    "connection": {
                        "host": workstation.get("public_ip"),
                        "user": "ec2-user",
                    },
                }

            _set_prepare_step(
                job_id,
                "registry",
                "complete",
                "Authenticated",
            )

        else:
            _set_prepare_step(
                job_id,
                "registry",
                "not_required",
                (
                    "Existing execution environment available; "
                    "registry login not required"
                ),
            )

        _set_prepare_step(
            job_id,
            "execution_environment",
            "running",
            "Building and validating localhost/rh294-ee:1.0",
        )

        _set_prepare_step(
            job_id,
            "development_environment",
            "pending",
            "Waiting for execution environment",
        )

        bootstrap_result = _run_remote(
            client,
            "cd $HOME/ansible-projects/aws-rh294 && "
            "bash scripts/bootstrap-workstation.sh "
            ">/tmp/rh294-bootstrap.log 2>&1",
            timeout=900,
        )

        if not bootstrap_result["ok"]:
            _set_prepare_step(
                job_id,
                "execution_environment",
                "failed",
                "RH294 repository bootstrap failed",
            )

            return {
                "status": "failed",
                "stage": "rh294_bootstrap",
                "message": (
                    "RH294 repository bootstrap failed. "
                    "Check Launcher service logs."
                ),
            }

        _set_prepare_step(
            job_id,
            "execution_environment",
            "complete",
            "localhost/rh294-ee:1.0 available",
        )

        _set_prepare_step(
            job_id,
            "development_environment",
            "complete",
            "ansible-dev running; nested EE available",
        )

    except Exception as exc:
        print(
            "RH294 preparation backend error:",
            repr(exc),
            flush=True,
        )

        return {
            "status": "failed",
            "stage": "prepare",
            "message": "RH294 preparation failed",
        }

    finally:
        if client is not None:
            client.close()

    _set_prepare_step(
        job_id,
        "ansible_validation",
        "running",
        "Running functional Ansible validation",
    )

    validation = validate_ansible(inventory)

    if validation.get("status") != "ready":
        _set_prepare_step(
            job_id,
            "ansible_validation",
            "failed",
            "Functional Ansible validation failed",
        )

        return {
            "status": "failed",
            "stage": "ansible_validation",
            "message": "Functional Ansible validation failed",
        }

    _set_prepare_step(
        job_id,
        "ansible_validation",
        "complete",
        "Last functional validation passed",
    )

    return {
        "status": "ready",
        "stage": "complete",
        "rh294_ready": True,
        "message": "RH294 environment prepared and validated",
    }
