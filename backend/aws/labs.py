from backend.aws.client import get_ec2_client, get_session


def discover_environment():
    ec2 = get_ec2_client()

    # Find the default VPC
    vpcs = ec2.describe_vpcs(
        Filters=[
            {
                "Name": "is-default",
                "Values": ["true"],
            }
        ]
    ).get("Vpcs", [])

    if not vpcs:
        return {
            "status": "error",
            "message": "No default VPC found",
        }

    vpc = vpcs[0]
    vpc_id = vpc["VpcId"]

    # Find subnets belonging to the default VPC
    subnet_response = ec2.describe_subnets(
        Filters=[
            {
                "Name": "vpc-id",
                "Values": [vpc_id],
            }
        ]
    )

    subnets = []

    for subnet in subnet_response.get("Subnets", []):
        subnets.append(
            {
                "subnet_id": subnet["SubnetId"],
                "availability_zone": subnet["AvailabilityZone"],
                "cidr": subnet["CidrBlock"],
            }
        )

    subnets.sort(key=lambda item: item["availability_zone"])

    return {
        "status": "ready",
        "vpc_id": vpc_id,
        "vpc_cidr": vpc["CidrBlock"],
        "subnets": subnets,
    }


def validate_ami(ami_id):
    ec2 = get_ec2_client()

    try:
        response = ec2.describe_images(
            ImageIds=[ami_id]
        )

        images = response.get("Images", [])

        if not images:
            return {
                "status": "error",
                "message": "AMI not found",
            }

        image = images[0]

        return {
            "status": "valid",
            "ami_id": image["ImageId"],
            "name": image.get("Name"),
            "architecture": image.get("Architecture"),
            "state": image.get("State"),
            "root_device_type": image.get("RootDeviceType"),
        }

    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }


def ensure_security_group(vpc_id, allowed_ssh_cidr):
    ec2 = get_ec2_client()

    group_name = "ansible-lab-launcher-sg"

    # Reuse the security group if our launcher already created it
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [group_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    ).get("SecurityGroups", [])

    if existing:
        return {
            "status": "existing",
            "group_id": existing[0]["GroupId"],
            "group_name": group_name,
        }

    # Create the security group
    response = ec2.create_security_group(
        GroupName=group_name,
        Description="Security group for Ansible Lab Launcher",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": group_name},
                    {"Key": "ManagedBy", "Value": "ansible-lab-launcher"},
                    {"Key": "Lab", "Value": "ansible-lab"},
                ],
            }
        ],
    )

    group_id = response["GroupId"]

    # SSH from the user's approved CIDR
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [
                    {
                        "CidrIp": allowed_ssh_cidr,
                        "Description": "SSH access to Ansible lab",
                    }
                ],
            }
        ],
    )

    # Allow communication between machines in the same lab SG
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "-1",
                "UserIdGroupPairs": [
                    {
                        "GroupId": group_id,
                        "Description": "Internal Ansible lab communication",
                    }
                ],
            }
        ],
    )

    return {
        "status": "created",
        "group_id": group_id,
        "group_name": group_name,
        "ssh_cidr": allowed_ssh_cidr,
    }


LAB_NODES = {
    "workstation": {
        "instance_type": "t2.medium",
        "practice_disks": [],
        "root_disk_size": 30,
    },
    "servera": {
        "instance_type": "t2.medium",
        "practice_disks": [2],
    },
    "serverb": {
        "instance_type": "t2.medium",
        "practice_disks": [2],
    },
    "serverc": {
        "instance_type": "t2.medium",
        "practice_disks": [2],
    },
    "serverd": {
        "instance_type": "t2.medium",
        "practice_disks": [2, 1],
    },
}


def get_lab_plan():
    return {
        "status": "ready",
        "node_count": len(LAB_NODES),
        "nodes": LAB_NODES,
    }


def preflight_lab(ami_id, key_name):
    ec2 = get_ec2_client()

    # 1. Validate AMI
    ami = validate_ami(ami_id)

    if ami.get("status") != "valid":
        return {
            "status": "failed",
            "check": "ami",
            "details": ami,
        }

    # 2. Discover network
    environment = discover_environment()

    if environment.get("status") != "ready":
        return {
            "status": "failed",
            "check": "network",
            "details": environment,
        }

    # 3. Verify SSH key exists in AWS
    keys = ec2.describe_key_pairs(
        Filters=[
            {
                "Name": "key-name",
                "Values": [key_name],
            }
        ]
    ).get("KeyPairs", [])

    if not keys:
        return {
            "status": "failed",
            "check": "ssh_key",
            "message": f"SSH key '{key_name}' does not exist",
        }

    # 4. Select a subnet
    subnets = environment["subnets"]

    if not subnets:
        return {
            "status": "failed",
            "check": "subnet",
            "message": "No subnet available",
        }

    subnet = subnets[0]

    # 5. Return the complete proposed plan
    return {
        "status": "ready",
        "region": get_session().region_name,
        "ami": ami,
        "key_name": key_name,
        "vpc_id": environment["vpc_id"],
        "subnet": subnet,
        "lab": get_lab_plan(),
    }




def create_lab(ami_id, key_name, security_group_id):
    ec2 = get_ec2_client()

    preflight = preflight_lab(ami_id, key_name)

    if preflight.get("status") != "ready":
        return preflight

    subnet_id = preflight["subnet"]["subnet_id"]

    # Prevent duplicate launcher-managed labs
    existing = ec2.describe_instances(
        Filters=[
            {
                "Name": "tag:ManagedBy",
                "Values": ["ansible-lab-launcher"],
            },
            {
                "Name": "tag:Lab",
                "Values": ["ansible-lab"],
            },
            {
                "Name": "instance-state-name",
                "Values": [
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                ],
            },
        ]
    )

    existing_instances = [
        instance
        for reservation in existing["Reservations"]
        for instance in reservation["Instances"]
    ]

    if existing_instances:
        return {
            "status": "exists",
            "message": "An Ansible Lab Launcher environment already exists",
            "instance_count": len(existing_instances),
        }

    created = []

    try:
        for node_name, node_config in LAB_NODES.items():

            block_device_mappings = []

            if "root_disk_size" in node_config:
                image = ec2.describe_images(ImageIds=[ami_id])["Images"][0]
                root_device = image["RootDeviceName"]

                block_device_mappings = [
                    {
                        "DeviceName": root_device,
                        "Ebs": {
                            "VolumeSize": node_config["root_disk_size"],
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ]

            run_args = {
                "ImageId": ami_id,
                "InstanceType": node_config["instance_type"],
                "KeyName": key_name,
                "MinCount": 1,
                "MaxCount": 1,
                "SubnetId": subnet_id,
                "SecurityGroupIds": [security_group_id],
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": node_name},
                            {
                                "Key": "ManagedBy",
                                "Value": "ansible-lab-launcher",
                            },
                            {
                                "Key": "Lab",
                                "Value": "ansible-lab",
                            },
                        ],
                    }
                ],
            }

            if block_device_mappings:
                run_args["BlockDeviceMappings"] = block_device_mappings

            response = ec2.run_instances(**run_args)

            instance = response["Instances"][0]

            created.append(
                {
                    "name": node_name,
                    "instance_id": instance["InstanceId"],
                    "instance_type": node_config["instance_type"],
                    "practice_disks": node_config["practice_disks"],
                }
            )

    except Exception as exc:

        # Roll back only instances created during this attempt
        instance_ids = [
            item["instance_id"]
            for item in created
        ]

        if instance_ids:
            ec2.terminate_instances(
                InstanceIds=instance_ids
            )

        return {
            "status": "failed",
            "message": "Lab creation failed. Created instances were terminated.",
            "created_before_failure": len(created),
            "error_type": type(exc).__name__,
        }

    return {
        "status": "created",
        "instance_count": len(created),
        "instances": created,
    }




def create_practice_disks():
    ec2 = get_ec2_client()

    # Discover launcher-managed running instances
    response = ec2.describe_instances(
        Filters=[
            {
                "Name": "tag:ManagedBy",
                "Values": ["ansible-lab-launcher"],
            },
            {
                "Name": "instance-state-name",
                "Values": ["running"],
            },
        ]
    )

    instances = {}

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            name = next(
                (
                    tag["Value"]
                    for tag in instance.get("Tags", [])
                    if tag["Key"] == "Name"
                ),
                None,
            )

            if name in LAB_NODES:
                instances[name] = instance

    results = []

    for node_name, node_config in LAB_NODES.items():

        instance = instances.get(node_name)

        if not instance:
            return {
                "status": "failed",
                "message": f"Instance {node_name} not found",
            }

        for disk_number, size_gb in enumerate(
            node_config["practice_disks"],
            start=1,
        ):
            volume_name = f"{node_name}-practice-{disk_number}"

            # Check whether this practice volume already exists
            existing = ec2.describe_volumes(
                Filters=[
                    {
                        "Name": "tag:ManagedBy",
                        "Values": ["ansible-lab-launcher"],
                    },
                    {
                        "Name": "tag:Name",
                        "Values": [volume_name],
                    },
                    {
                        "Name": "status",
                        "Values": ["creating", "available", "in-use"],
                    },
                ]
            ).get("Volumes", [])

            if existing:
                volume = existing[0]
                volume_id = volume["VolumeId"]
                action = "existing"

            else:
                az = instance["Placement"]["AvailabilityZone"]

                volume = ec2.create_volume(
                    AvailabilityZone=az,
                    Size=size_gb,
                    VolumeType="gp3",
                    TagSpecifications=[
                        {
                            "ResourceType": "volume",
                            "Tags": [
                                {
                                    "Key": "Name",
                                    "Value": volume_name,
                                },
                                {
                                    "Key": "ManagedBy",
                                    "Value": "ansible-lab-launcher",
                                },
                                {
                                    "Key": "Lab",
                                    "Value": "ansible-lab",
                                },
                                {
                                    "Key": "Node",
                                    "Value": node_name,
                                },
                                {
                                    "Key": "PracticeDisk",
                                    "Value": str(disk_number),
                                },
                            ],
                        }
                    ],
                )

                volume_id = volume["VolumeId"]
                action = "created"

            # Wait until AWS says the volume is available
            volume_info = ec2.describe_volumes(
                VolumeIds=[volume_id]
            )["Volumes"][0]

            if volume_info["State"] == "creating":
                waiter = ec2.get_waiter("volume_available")
                waiter.wait(VolumeIds=[volume_id])

                volume_info = ec2.describe_volumes(
                    VolumeIds=[volume_id]
                )["Volumes"][0]

            # Attach only if it is not already attached
            attachments = volume_info.get("Attachments", [])

            if not attachments:
                device_name = f"/dev/sd{chr(ord('f') + disk_number - 1)}"

                ec2.attach_volume(
                    VolumeId=volume_id,
                    InstanceId=instance["InstanceId"],
                    Device=device_name,
                )

                attachment = "attached"

            else:
                attachment = "existing"

            results.append(
                {
                    "node": node_name,
                    "volume_id": volume_id,
                    "size_gb": size_gb,
                    "volume_action": action,
                    "attachment": attachment,
                }
            )

    return {
        "status": "ready",
        "volume_count": len(results),
        "volumes": results,
    }


def get_lab_inventory():
    ec2 = get_ec2_client()

    response = ec2.describe_instances(
        Filters=[
            {
                "Name": "tag:ManagedBy",
                "Values": ["ansible-lab-launcher"],
            },
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )

    nodes = {}

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            name = next(
                (
                    tag["Value"]
                    for tag in instance.get("Tags", [])
                    if tag["Key"] == "Name"
                ),
                None,
            )

            if name not in LAB_NODES:
                continue

            nodes[name] = {
                "instance_id": instance["InstanceId"],
                "state": instance["State"]["Name"],
                "private_ip": instance.get("PrivateIpAddress"),
                "public_ip": instance.get("PublicIpAddress"),
                "availability_zone": instance["Placement"]["AvailabilityZone"],
                "instance_type": instance["InstanceType"],
            }

    return {
        "status": "ready" if len(nodes) == len(LAB_NODES) else "incomplete",
        "node_count": len(nodes),
        "nodes": nodes,
    }


def get_destroy_plan():
    """Return launcher-owned AWS resources that would be destroyed."""
    ec2 = get_ec2_client()

    ownership_filters = [
        {
            "Name": "tag:ManagedBy",
            "Values": ["ansible-lab-launcher"],
        },
        {
            "Name": "tag:Lab",
            "Values": ["ansible-lab"],
        },
    ]

    # Launcher-owned EC2 instances
    response = ec2.describe_instances(
        Filters=ownership_filters
        + [
            {
                "Name": "instance-state-name",
                "Values": [
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                ],
            }
        ]
    )

    instances = []

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            name = next(
                (
                    tag["Value"]
                    for tag in instance.get("Tags", [])
                    if tag["Key"] == "Name"
                ),
                None,
            )

            instances.append(
                {
                    "name": name,
                    "instance_id": instance["InstanceId"],
                    "state": instance["State"]["Name"],
                }
            )

    # Launcher-owned practice volumes only
    volume_response = ec2.describe_volumes(
        Filters=ownership_filters
        + [
            {
                "Name": "tag-key",
                "Values": ["PracticeDisk"],
            }
        ]
    )

    volumes = []

    for volume in volume_response.get("Volumes", []):
        name = next(
            (
                tag["Value"]
                for tag in volume.get("Tags", [])
                if tag["Key"] == "Name"
            ),
            None,
        )

        volumes.append(
            {
                "name": name,
                "volume_id": volume["VolumeId"],
                "state": volume["State"],
            }
        )

    # Launcher-owned security groups
    sg_response = ec2.describe_security_groups(
        Filters=ownership_filters
    )

    security_groups = [
        {
            "name": sg["GroupName"],
            "group_id": sg["GroupId"],
        }
        for sg in sg_response.get("SecurityGroups", [])
    ]

    return {
        "status": "preview",
        "instance_count": len(instances),
        "volume_count": len(volumes),
        "security_group_count": len(security_groups),
        "instances": sorted(
            instances,
            key=lambda item: item["name"] or "",
        ),
        "volumes": sorted(
            volumes,
            key=lambda item: item["name"] or "",
        ),
        "security_groups": security_groups,
        "preserved": {
            "aws_key_pair": True,
            "local_private_key": True,
        },
    }


def destroy_lab():
    """Safely destroy resources owned by the Ansible Lab Launcher."""
    import time
    from botocore.exceptions import ClientError

    ec2 = get_ec2_client()

    # Capture the exact resource set before termination.
    plan = get_destroy_plan()

    instance_ids = [
        item["instance_id"]
        for item in plan["instances"]
    ]

    volume_ids = [
        item["volume_id"]
        for item in plan["volumes"]
    ]

    security_group_ids = [
        item["group_id"]
        for item in plan["security_groups"]
    ]

    # 1. Terminate launcher-owned EC2 instances.
    if instance_ids:
        ec2.terminate_instances(
            InstanceIds=instance_ids
        )

        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(
            InstanceIds=instance_ids,
            WaiterConfig={
                "Delay": 10,
                "MaxAttempts": 60,
            },
        )

    # 2. Wait for practice volumes to detach, then delete them.
    deleted_volumes = []

    for volume_id in volume_ids:
        waiter = ec2.get_waiter("volume_available")

        try:
            waiter.wait(
                VolumeIds=[volume_id],
                WaiterConfig={
                    "Delay": 5,
                    "MaxAttempts": 60,
                },
            )

            ec2.delete_volume(
                VolumeId=volume_id
            )

            deleted_volumes.append(volume_id)

        except ClientError as exc:
            raise RuntimeError(
                f"Unable to delete practice volume {volume_id}"
            ) from exc

    # 3. Delete launcher security groups after instance ENIs disappear.
    deleted_security_groups = []

    for group_id in security_group_ids:
        last_error = None

        for _ in range(30):
            try:
                ec2.delete_security_group(
                    GroupId=group_id
                )

                deleted_security_groups.append(group_id)
                last_error = None
                break

            except ClientError as exc:
                error_code = exc.response.get(
                    "Error", {}
                ).get("Code")

                if error_code == "DependencyViolation":
                    last_error = exc
                    time.sleep(5)
                    continue

                raise

        if last_error is not None:
            raise RuntimeError(
                f"Security group {group_id} still has dependencies"
            ) from last_error

    return {
        "status": "destroyed",
        "terminated_instance_count": len(instance_ids),
        "deleted_volume_count": len(deleted_volumes),
        "deleted_security_group_count": len(
            deleted_security_groups
        ),
        "preserved": {
            "aws_key_pair": True,
            "local_private_key": True,
        },
    }




def build_lab(ami_id, key_name, allowed_ssh_cidr):
    """Build the complete AWS infrastructure for an Ansible lab."""
    ec2 = get_ec2_client()

    # 1. Preflight validation
    preflight = preflight_lab(ami_id, key_name)

    if preflight.get("status") != "ready":
        return preflight

    # 2. Create or reuse the launcher security group
    security_group = ensure_security_group(
        preflight["vpc_id"],
        allowed_ssh_cidr,
    )

    security_group_id = security_group["group_id"]

    # 3. Create the five EC2 instances
    lab = create_lab(
        ami_id,
        key_name,
        security_group_id,
    )

    if lab.get("status") != "created":
        return lab

    instance_ids = [
        instance["instance_id"]
        for instance in lab["instances"]
    ]

    # 4. Wait until all instances are running
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(
        InstanceIds=instance_ids,
        WaiterConfig={
            "Delay": 10,
            "MaxAttempts": 60,
        },
    )

    # 5. Create and attach the RH294 practice disks
    disks = create_practice_disks()

    if disks.get("status") != "ready":
        return {
            "status": "failed",
            "stage": "practice_disks",
            "details": disks,
        }

    # 6. Discover the completed lab
    inventory = get_lab_inventory()

    return {
        "status": "ready",
        "region": preflight["region"],
        "instance_count": len(instance_ids),
        "security_group_id": security_group_id,
        "instances": inventory,
        "practice_disks": disks,
        "message": "AWS lab infrastructure is ready",
    }
