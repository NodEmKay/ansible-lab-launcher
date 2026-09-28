import boto3


def get_session():
    return boto3.Session()


def get_ec2_client():
    session = get_session()

    return session.client(
        "ec2",
        region_name=session.region_name
    )
