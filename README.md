# Ansible Lab Launcher

A containerized web application for provisioning and preparing a reproducible AWS lab for hands-on Ansible and Red Hat Enterprise Linux automation practice.

The launcher creates an AWS environment consisting of one Ansible workstation and four managed nodes, prepares the workstation, configures the execution environment, and validates the lab with Ansible before reporting it as ready.

## Architecture

                    Ansible Lab Launcher
                    FastAPI + Web UI
                           |
                           v
                          AWS
                           |
        +------------------+------------------+
        |                  |                  |
        v                  v                  v
   Workstation          servera           serverb
   Ansible host
        |
        +-------------> serverc
        |
        +-------------> serverd

The current lab contains:

- 1 Ansible workstation
- 4 managed RHEL nodes
- Dedicated practice EBS volumes
- SSH access restricted to a configured IPv4 CIDR
- Podman-based Ansible development environment
- Custom RH294 execution environment
- Automated Ansible connectivity validation

## Workflow

AWS Infrastructure
       |
       v
Prepare Workstation
       |
       v
Validate Managed Hosts
       |
       v
Configure Host Mappings
       |
       v
Red Hat Registry Authentication
       |
       v
Build Execution Environment
       |
       v
Start Development Environment
       |
       v
Run Ansible Validation
       |
       v
RH294 LAB READY

Preparation runs asynchronously and the UI displays live progress.

If authentication to `registry.redhat.io` is required, the launcher pauses and displays an **Action Required** panel.

The Red Hat credentials are entered directly on the AWS workstation:

podman login registry.redhat.io

The launcher does not request or store Red Hat credentials.

## Prerequisites

You need:

- Linux or WSL
- Podman
- AWS CLI configuration
- AWS credentials with permissions required to create the lab resources
- Internet connectivity
- Red Hat registry credentials when a required execution-environment image must be pulled

The current implementation has been developed and tested in `us-east-1`.

The configured RHEL AMI is region-specific. Verify the AMI before using another AWS region.

## Run the Launcher

Clone the repository:

git clone https://github.com/NodEmKay/ansible-lab-launcher.git
cd ansible-lab-launcher

Ensure your AWS CLI credentials are configured on the host.

Start the development container:

./run-dev.sh

The FastAPI service listens on port `8000`.

Open the launcher UI in your browser.

For WSL environments, use the WSL address if Windows localhost forwarding is unavailable.

## Build the Lab

The Build operation:

1. Validates the AMI ID and SSH CIDR.
2. Reconciles the launcher-managed SSH key.
3. Discovers the current AWS environment.
4. Creates the launcher security group.
5. Creates the workstation and four managed nodes.
6. Waits for the instances to become available.
7. Creates and attaches the practice EBS volumes.
8. Discovers the resulting lab inventory.

The launcher rejects malformed AMI IDs, malformed IPv4 CIDRs, IPv6 SSH CIDRs, and `0.0.0.0/0` SSH access.

## Prepare the RH294 Environment

After the infrastructure is active, select **Prepare RH294 Environment**.

The launcher prepares:

- Git
- Podman
- Lab repository
- SSH configuration
- Current managed-node host mappings
- Red Hat registry authentication state
- Custom execution environment
- `ansible-dev`
- Nested execution environment
- Functional Ansible validation

Preparation is performed as a background job so the UI can report progress while the operation is running.

## Registry Authentication

Registry credentials are intentionally kept outside the launcher.

When authentication is required:

1. Connect to the AWS workstation using the SSH instruction shown by the launcher.
2. Run `podman login registry.redhat.io`.
3. Enter the Red Hat credentials directly on the workstation.
4. Return to the launcher.
5. Select **Continue Setup**.

The launcher then resumes preparation.

## Ready State

Infrastructure being active does not mean the RH294 environment is ready.

The launcher reports `RH294 LAB READY` only after functional Ansible validation succeeds against the managed nodes.

## Destroy the Lab

Use **Destroy Lab** when the environment is no longer required.

The launcher removes the AWS resources managed by the lab while preserving the launcher-managed AWS SSH key pair and its local private-key state for reconciliation.

Always verify your AWS account after using the lab to ensure that no unexpected billable resources remain.

## Operation Safety

Build, Prepare, and Destroy are mutually exclusive operations.

The backend maintains an operation coordinator so competing lab-changing operations are rejected rather than running concurrently.

This protection is enforced server-side and does not depend solely on disabled UI buttons.

## AWS Sandbox Changes

The launcher does not assume that an AWS account remains permanent.

At runtime it checks the current AWS identity and region and reconciles its launcher-managed SSH key state accordingly.

This supports temporary AWS sandbox environments where the AWS account or credentials can change between sessions.

## Security Design

The launcher follows several security boundaries:

- AWS credentials are not stored in the repository.
- Local AWS credentials are mounted read-only during local development.
- SSH private keys are not exposed through the web UI.
- Private-key files are excluded from Git.
- Red Hat passwords are never collected by the launcher.
- SSH ingress does not permit `0.0.0.0/0`.
- Build inputs are validated server-side.
- Raw AWS exception details are not returned by public-facing AWS status/environment API responses.
- AWS account IDs and caller ARNs are not exposed by the status API.
- Lab-changing operations are mutually exclusive.
- Launcher-managed resources are identified using AWS tags.

Before an Internet-facing deployment, use an IAM role rather than mounted long-lived AWS credentials.

## Current V1 Scope

V1 is designed as a self-hosted, single-operator lab launcher and as a public source-code/portfolio project.

It is **not** intended to be deployed as an anonymous public AWS provisioning service.

A multi-user Internet-facing deployment would require additional controls such as:

- Authentication and authorization
- Per-user or per-lab ownership
- Persistent job state
- Stronger isolation
- IAM least-privilege deployment roles
- HTTPS
- CSRF protection where cookie authentication is used
- Rate and cost controls
- Durable operation queues
- Additional audit logging and monitoring

## Technology

- Python 3.12
- FastAPI
- Uvicorn
- boto3
- Paramiko
- Podman
- HTML / CSS / JavaScript
- AWS EC2
- AWS EBS
- AWS Security Groups
- Ansible
- Ansible Navigator
- Red Hat Ansible Automation Platform execution environments

## Repository Safety

Credential material must never be committed.

The repository ignores:

*.pem
*.key
.aws/
.env
.env.*
data/
keys/

Before publishing changes, perform a secret scan of both the working tree and Git history.

## Status

The launcher currently supports:

- AWS environment discovery
- AWS account-aware SSH-key reconciliation
- Complete five-node lab provisioning
- Practice EBS disk provisioning
- Secure SSH CIDR configuration
- Automated workstation bootstrap
- Dynamic managed-host inventory
- RH294 execution-environment preparation
- Persistent registry Action Required workflow
- Live preparation progress
- Functional Ansible validation
- Server-side operation locking
- Build input validation
- Lab destruction
- Public API response sanitization

## License

No license has been selected yet.

Until a license is added, normal copyright rules apply to this repository.
