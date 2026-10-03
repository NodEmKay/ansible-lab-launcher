import os

import boto3


def get_session():
    role_arn = os.getenv("STUDENT_ROLE_ARN")
    external_id = os.getenv("STUDENT_EXTERNAL_ID")

    if not role_arn and not external_id:
        return boto3.Session()

    if not role_arn or not external_id:
        raise ValueError(
            "STUDENT_ROLE_ARN and STUDENT_EXTERNAL_ID must both be configured"
        )

    base_session = boto3.Session()

    sts = base_session.client("sts")

    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="ansible-lab-launcher",
        ExternalId=external_id,
    )

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=base_session.region_name,
    )


def get_ec2_client():
    session = get_session()

    return session.client(
        "ec2",
        region_name=session.region_name
    )
