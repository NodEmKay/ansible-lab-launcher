import os

from backend.aws.labs import get_lab_inventory


KEY_PATH = "/data/keys/ansible-lab-key.pem"
REMOTE_KEY_PATH = "/home/ec2-user/.ssh/ansible-lab-key.pem"


def get_bootstrap_plan():
    inventory = get_lab_inventory()

    if inventory["status"] != "ready":
        return {
            "status": "failed",
            "message": "Lab inventory is not ready",
        }

    workstation = inventory["nodes"]["workstation"]

    managed_nodes = {
        name: data
        for name, data in inventory["nodes"].items()
        if name != "workstation"
    }

    if not os.path.exists(KEY_PATH):
        return {
            "status": "failed",
            "message": "Launcher SSH private key is missing",
        }

    return {
        "status": "ready",
        "workstation": {
            "public_ip": workstation["public_ip"],
            "private_ip": workstation["private_ip"],
            "ssh_user": "ec2-user",
        },
        "managed_nodes": {
            name: {
                "private_ip": data["private_ip"],
                "ssh_user": "ec2-user",
            }
            for name, data in managed_nodes.items()
        },
        "local_key_path": KEY_PATH,
        "remote_key_path": REMOTE_KEY_PATH,
    }


def get_ssh_commands():
    plan = get_bootstrap_plan()

    if plan["status"] != "ready":
        return plan

    workstation = plan["workstation"]
    public_ip = workstation["public_ip"]

    return {
        "status": "ready",
        "workstation_public_ip": public_ip,
        "ssh_user": workstation["ssh_user"],
        "local_key_path": KEY_PATH,
        "remote_key_path": REMOTE_KEY_PATH,
    }


def generate_inventory():
    plan = get_bootstrap_plan()

    if plan["status"] != "ready":
        return plan

    nodes = plan["managed_nodes"]

    inventory = [
        "[prod]",
        f"servera.lab.com ansible_host={nodes['servera']['private_ip']}",
        f"serverb.lab.com ansible_host={nodes['serverb']['private_ip']}",
        "",
        "[test]",
        f"serverc.lab.com ansible_host={nodes['serverc']['private_ip']}",
        f"serverd.lab.com ansible_host={nodes['serverd']['private_ip']}",
        "",
        "[all:vars]",
        "ansible_user=ec2-user",
        "ansible_ssh_private_key_file=/root/.ssh/ansiblelab.pem",
        "ansible_become=true",
        "",
    ]

    return {
        "status": "ready",
        "content": "\n".join(inventory),
    }


def generate_workstation_bootstrap():
    plan = get_bootstrap_plan()

    if plan["status"] != "ready":
        return plan

    nodes = plan["managed_nodes"]

    inventory = f"""[prod]
servera.lab.com ansible_host={nodes['servera']['private_ip']}
serverb.lab.com ansible_host={nodes['serverb']['private_ip']}

[test]
serverc.lab.com ansible_host={nodes['serverc']['private_ip']}
serverd.lab.com ansible_host={nodes['serverd']['private_ip']}

[all:vars]
ansible_user=ec2-user
ansible_ssh_private_key_file=/root/.ssh/ansible-lab-key.pem
ansible_become=true
"""

    hosts = "\n".join(
        f"{data['private_ip']} {name} {name}.lab.com"
        for name, data in sorted(nodes.items())
    )

    script = f"""#!/bin/bash
set -euo pipefail

PROJECT="$HOME/ansible-projects/aws-rh294"

sudo dnf -y install git podman

mkdir -p "$HOME/ansible-projects"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# RH294 compatibility key name.
# Relative target works both on workstation and inside ansible-dev.
ln -sfn ansible-lab-key.pem "$HOME/.ssh/ansiblelab.pem"

RH294_REVISION="2bffc2e5288eb876c313ce68ae844fb1fb57b93d"

if [ -d "$PROJECT/.git" ]; then
    git -C "$PROJECT" fetch origin
else
    git clone https://github.com/NodEmKay/aws-rh294-lab.git "$PROJECT"
fi

git -C "$PROJECT" checkout --detach "$RH294_REVISION"

cat > "$PROJECT/inventory" <<'INVENTORY'
{inventory}
INVENTORY

# Compatibility file consumed by the RH294 bootstrap script.
mkdir -p "$PROJECT/scripts"

cat > "$PROJECT/scripts/lab-hosts.txt" <<'LABHOSTS'
{hosts}
LABHOSTS

while read -r ip name fqdn; do
    # Remove any previous Launcher-managed mapping for this host.
    sudo sed -i -E "/[[:space:]]${{name}}([[:space:]]|$)/d" /etc/hosts

    # Write the current AWS private address with both names.
    echo "$ip $name $fqdn" |
        sudo tee -a /etc/hosts >/dev/null
done <<'HOSTS'
{hosts}
HOSTS

echo "Bootstrap files prepared."
"""

    return {
        "status": "ready",
        "script": script,
        "inventory": inventory,
    }


def bootstrap_workstation():
    """Prepare the current AWS workstation for the Ansible lab."""

    import time
    import paramiko

    inventory_data = get_lab_inventory()

    if inventory_data.get("status") != "ready":
        return {
            "status": "failed",
            "stage": "inventory",
            "message": "Lab inventory is not ready",
        }

    nodes = inventory_data.get("nodes", {})
    workstation = nodes.get("workstation")

    if not workstation:
        return {
            "status": "failed",
            "stage": "inventory",
            "message": "Workstation was not found",
        }

    workstation_ip = workstation.get("public_ip")

    if not workstation_ip:
        return {
            "status": "failed",
            "stage": "inventory",
            "message": "Workstation has no public IP",
        }

    if not os.path.isfile(KEY_PATH):
        return {
            "status": "failed",
            "stage": "ssh_key",
            "message": "Launcher-managed SSH key is unavailable",
        }

    bootstrap = generate_workstation_bootstrap()

    if bootstrap.get("status") != "ready":
        return {
            "status": "failed",
            "stage": "bootstrap_generation",
            "message": "Unable to generate workstation bootstrap",
        }

    script = bootstrap["script"]

    remote_script = "/home/ec2-user/bootstrap-launcher.sh"
    remote_key = REMOTE_KEY_PATH

    client = paramiko.SSHClient()

    # New lab instances do not yet exist in a persistent
    # Launcher known_hosts database.
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    try:

        # A newly-created EC2 instance may report "running"
        # before sshd is ready, so retry for a short period.
        last_error = None

        for attempt in range(12):

            try:

                client.connect(
                    hostname=workstation_ip,
                    username="ec2-user",
                    key_filename=KEY_PATH,
                    timeout=10,
                    banner_timeout=10,
                    auth_timeout=10,
                    look_for_keys=False,
                    allow_agent=False,
                )

                last_error = None
                break

            except Exception as exc:

                last_error = exc

                if attempt < 11:
                    time.sleep(5)

        if last_error is not None:
            return {
                "status": "failed",
                "stage": "ssh_connect",
                "message": "Unable to connect to workstation",
            }

        # Transfer the private key directly over SFTP.
        # Its contents are never returned through the API.
        sftp = client.open_sftp()

        try:

            sftp.put(
                KEY_PATH,
                remote_key,
            )

            sftp.chmod(
                remote_key,
                0o600,
            )

            with sftp.file(remote_script, "w") as remote_file:
                remote_file.write(script)

            sftp.chmod(
                remote_script,
                0o700,
            )

        finally:
            sftp.close()

        stdin, stdout, stderr = client.exec_command(
            remote_script,
            timeout=600,
        )

        exit_code = stdout.channel.recv_exit_status()

        stdout_text = stdout.read().decode(
            "utf-8",
            errors="replace",
        )

        stderr_text = stderr.read().decode(
            "utf-8",
            errors="replace",
        )

        if exit_code != 0:

            # Do not expose excessive remote output through
            # the API. Keep enough information for diagnosis.
            return {
                "status": "failed",
                "stage": "remote_bootstrap",
                "exit_code": exit_code,
                "message": "Workstation bootstrap failed",
                "stderr": stderr_text[-2000:],
            }

        return {
            "status": "ready",
            "stage": "workstation_prepared",
            "workstation": "workstation",
            "message": "Workstation bootstrap completed",
            "output": stdout_text[-2000:],
        }

    except Exception:

        return {
            "status": "failed",
            "stage": "bootstrap",
            "message": "Unexpected workstation bootstrap failure",
        }

    finally:

        client.close()


def get_bootstrap_status():
    """Check whether the current workstation bootstrap is complete."""

    import paramiko

    inventory_data = get_lab_inventory()

    if inventory_data.get("status") != "ready":
        return {
            "status": "not_ready",
            "bootstrap": "incomplete",
            "stage": "infrastructure",
        }

    nodes = inventory_data.get("nodes", {})
    workstation = nodes.get("workstation")

    if not workstation or not workstation.get("public_ip"):
        return {
            "status": "not_ready",
            "bootstrap": "incomplete",
            "stage": "workstation",
        }

    if not os.path.isfile(KEY_PATH):
        return {
            "status": "not_ready",
            "bootstrap": "incomplete",
            "stage": "ssh_key",
        }

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    try:

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

        command = r'''
set -e

command -v git >/dev/null
command -v podman >/dev/null

test -d "$HOME/ansible-projects/aws-rh294/.git"
test -f "$HOME/ansible-projects/aws-rh294/inventory"

test -f "$HOME/.ssh/ansible-lab-key.pem"
test "$(stat -c '%a' "$HOME/.ssh/ansible-lab-key.pem")" = "600"

test -L "$HOME/.ssh/ansiblelab.pem"
test "$(readlink "$HOME/.ssh/ansiblelab.pem")" = "ansible-lab-key.pem"

echo BOOTSTRAP_READY
'''

        stdin, stdout, stderr = client.exec_command(
            command,
            timeout=30,
        )

        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode(
            "utf-8",
            errors="replace",
        ).strip()

        if exit_code == 0 and output == "BOOTSTRAP_READY":
            return {
                "status": "ready",
                "bootstrap": "complete",
                "workstation": "workstation",
            }

        return {
            "status": "not_ready",
            "bootstrap": "incomplete",
            "stage": "validation",
        }

    except Exception:
        return {
            "status": "not_ready",
            "bootstrap": "incomplete",
            "stage": "ssh_connect",
        }

    finally:
        client.close()


def validate_managed_hosts():
    """Validate SSH connectivity from workstation to all managed hosts."""

    import paramiko

    inventory_data = get_lab_inventory()

    if inventory_data.get("status") != "ready":
        return {
            "status": "failed",
            "stage": "inventory",
            "message": "Lab inventory is not ready",
        }

    nodes = inventory_data.get("nodes", {})
    workstation = nodes.get("workstation")

    if not workstation or not workstation.get("public_ip"):
        return {
            "status": "failed",
            "stage": "workstation",
            "message": "Workstation is unavailable",
        }

    if not os.path.isfile(KEY_PATH):
        return {
            "status": "failed",
            "stage": "ssh_key",
            "message": "Launcher-managed SSH key is unavailable",
        }

    managed_hosts = [
        name
        for name in ("servera", "serverb", "serverc", "serverd")
        if name in nodes
    ]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
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

        results = {}

        for host in managed_hosts:
            command = (
                "ssh "
                "-i ~/.ssh/ansible-lab-key.pem "
                "-o BatchMode=yes "
                "-o StrictHostKeyChecking=no "
                "-o ConnectTimeout=10 "
                f"ec2-user@{host} "
                "'hostname; id -un'"
            )

            stdin, stdout, stderr = client.exec_command(
                command,
                timeout=30,
            )

            output = stdout.read().decode(
                "utf-8",
                errors="replace",
            ).strip()

            error = stderr.read().decode(
                "utf-8",
                errors="replace",
            ).strip()

            exit_code = stdout.channel.recv_exit_status()

            results[host] = {
                "reachable": exit_code == 0,
                "output": output,
            }

            if exit_code != 0:
                results[host]["error"] = error[-500:]

        all_ready = (
            len(managed_hosts) == 4
            and all(
                result["reachable"]
                for result in results.values()
            )
        )

        return {
            "status": "ready" if all_ready else "failed",
            "stage": "managed_hosts",
            "host_count": len(managed_hosts),
            "results": results,
        }

    except Exception:
        return {
            "status": "failed",
            "stage": "ssh_connect",
            "message": "Unable to validate managed hosts",
        }

    finally:
        client.close()
