import boto3
import logging
import sys
import os
import base64
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. Configuration & Logging
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("aws_deployment.log"),
        logging.StreamHandler()
    ]
)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
UNIQUE_ID = datetime.now().strftime("%Y%m%d%H%M%S")

# Resource names with unique suffix
VPC_NAME = f"multi-tier-vpc-{UNIQUE_ID}"
DB_SUBNET_GROUP = f"db-subnet-group-{UNIQUE_ID}"
LAUNCH_TEMPLATE_NAME = f"web-lt-{UNIQUE_ID}"
ASG_NAME = f"web-asg-{UNIQUE_ID}"
TG_NAME = f"web-tg-{UNIQUE_ID}"
ALB_NAME = f"web-alb-{UNIQUE_ID}"
DB_INSTANCE_ID = f"web-db-{UNIQUE_ID}"

# Database credentials from environment
DB_USERNAME = os.getenv("DB_USERNAME", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD")
if not DB_PASSWORD:
    logging.error("DB_PASSWORD environment variable not set. Exiting.")
    sys.exit(1)

# Your specific AMI (Amazon Linux 2 in us-east-1)
AMI_ID = 'ami-00e801948462f718a'
logging.info(f"Using AMI: {AMI_ID}")

# Track created resources for rollback
deployed_resources = {
    'vpc_id': None,
    'public_subnets': [],
    'private_subnets': [],
    'security_groups': [],
    'internet_gateway': None,
    'nat_gateways': [],
    'elastic_ips': [],
    'load_balancer_arn': None,
    'target_group_arn': None,
    'auto_scaling_group': ASG_NAME,
    'db_instance': DB_INSTANCE_ID,
    'db_subnet_group': DB_SUBNET_GROUP,
    'launch_template': LAUNCH_TEMPLATE_NAME,
    'route_tables': []
}

# ---------------------------------------------------------
# 2. Rollback (Correct Order with Dependency Handling)
# ---------------------------------------------------------
def rollback():
    logging.warning("Starting rollback process...")

    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    elbv2 = boto3.client('elbv2', region_name=AWS_REGION)
    autoscaling = boto3.client('autoscaling', region_name=AWS_REGION)
    rds = boto3.client('rds', region_name=AWS_REGION)

    # ------------------------------------------------------------------
    # 1. RDS (takes longest, start early and wait for completion)
    # ------------------------------------------------------------------
    if deployed_resources['db_instance']:
        try:
            logging.info(f"Deleting RDS instance {deployed_resources['db_instance']} (skip final snapshot)")
            rds.delete_db_instance(DBInstanceIdentifier=deployed_resources['db_instance'], SkipFinalSnapshot=True)
            logging.info("Waiting for RDS to be deleted (this takes ~10-15 minutes)...")
            waiter = rds.get_waiter('db_instance_deleted')
            waiter.wait(DBInstanceIdentifier=deployed_resources['db_instance'])
            logging.info("RDS instance deleted successfully")
        except Exception as e:
            logging.error(f"RDS deletion error: {e}")

    # ------------------------------------------------------------------
    # 2. DB subnet group (only after RDS is gone)
    # ------------------------------------------------------------------
    if deployed_resources['db_subnet_group']:
        try:
            logging.info(f"Deleting DB subnet group {deployed_resources['db_subnet_group']}")
            rds.delete_db_subnet_group(DBSubnetGroupName=deployed_resources['db_subnet_group'])
        except Exception as e:
            logging.error(f"DB subnet group deletion error: {e}")

    # ------------------------------------------------------------------
    # 3. Auto Scaling Group (force delete, terminates instances)
    # ------------------------------------------------------------------
    if deployed_resources['auto_scaling_group']:
        try:
            logging.info(f"Deleting ASG {deployed_resources['auto_scaling_group']} (force delete)")
            autoscaling.delete_auto_scaling_group(
                AutoScalingGroupName=deployed_resources['auto_scaling_group'],
                ForceDelete=True
            )
            time.sleep(30)  # let instances terminate
        except Exception as e:
            logging.error(f"ASG deletion error: {e}")

    # ------------------------------------------------------------------
    # 4. Launch Template
    # ------------------------------------------------------------------
    if deployed_resources['launch_template']:
        try:
            logging.info(f"Deleting launch template {deployed_resources['launch_template']}")
            ec2.delete_launch_template(LaunchTemplateName=deployed_resources['launch_template'])
        except Exception as e:
            logging.error(f"Launch template deletion error: {e}")

    # ------------------------------------------------------------------
    # 5. Load Balancer & Target Group
    # ------------------------------------------------------------------
    if deployed_resources['load_balancer_arn']:
        try:
            logging.info(f"Deleting load balancer {deployed_resources['load_balancer_arn']}")
            elbv2.delete_load_balancer(LoadBalancerArn=deployed_resources['load_balancer_arn'])
            waiter = elbv2.get_waiter('load_balancers_deleted')
            waiter.wait(LoadBalancerArns=[deployed_resources['load_balancer_arn']])
            logging.info("Load balancer deleted")
        except Exception as e:
            logging.error(f"ALB deletion error: {e}")

    if deployed_resources['target_group_arn']:
        try:
            logging.info(f"Deleting target group {deployed_resources['target_group_arn']}")
            elbv2.delete_target_group(TargetGroupArn=deployed_resources['target_group_arn'])
        except Exception as e:
            logging.error(f"Target group deletion error: {e}")

    # ------------------------------------------------------------------
    # 6. EC2 instances (if any still running – safety)
    # ------------------------------------------------------------------
    if deployed_resources.get('running_instances'):
        instance_ids = deployed_resources['running_instances']
        try:
            logging.info(f"Terminating any remaining instances: {instance_ids}")
            ec2.terminate_instances(InstanceIds=instance_ids)
            waiter = ec2.get_waiter('instance_terminated')
            waiter.wait(InstanceIds=instance_ids)
            logging.info("Instances terminated")
        except Exception as e:
            logging.error(f"Instance termination error: {e}")

    # ------------------------------------------------------------------
    # 7. NAT Gateways (wait for completion)
    # ------------------------------------------------------------------
    for nat_id in deployed_resources['nat_gateways']:
        try:
            logging.info(f"Deleting NAT Gateway {nat_id}")
            ec2.delete_nat_gateway(NatGatewayId=nat_id)
        except Exception as e:
            logging.error(f"NAT deletion error: {e}")
    if deployed_resources['nat_gateways']:
        try:
            logging.info("Waiting for NAT gateways to be deleted...")
            waiter = ec2.get_waiter('nat_gateway_deleted')
            waiter.wait(NatGatewayIds=deployed_resources['nat_gateways'])
        except Exception:
            time.sleep(60)
        logging.info("NAT gateways deleted")

    # ------------------------------------------------------------------
    # 8. Release Elastic IPs (robust, with disassociation and error handling)
    # ------------------------------------------------------------------
    def release_eip(allocation_id):
        """Release a single EIP, ignoring already-released or invalid IDs."""
        try:
            eips = ec2.describe_addresses(AllocationIds=[allocation_id])['Addresses']
            if not eips:
                logging.info(f"EIP {allocation_id} not found (already released?)")
                return
            eip = eips[0]
            if eip.get('AssociationId'):
                assoc_id = eip['AssociationId']
                try:
                    logging.info(f"Disassociating EIP {allocation_id} (association {assoc_id})")
                    ec2.disassociate_address(AssociationId=assoc_id)
                    time.sleep(2)
                except Exception as e:
                    err = str(e)
                    if 'AuthFailure' in err or 'InvalidAssociationID' in err:
                        logging.info(f"EIP {allocation_id} already disassociated or association invalid")
                    else:
                        logging.warning(f"Unexpected error during disassociation: {e}")
            logging.info(f"Releasing EIP {allocation_id}")
            ec2.release_address(AllocationId=allocation_id)
            logging.info(f"✅ Released EIP {allocation_id}")
        except Exception as e:
            err = str(e)
            if 'InvalidAllocationID' in err or 'AuthFailure' in err:
                logging.info(f"EIP {allocation_id} already released or not found")
            else:
                logging.error(f"Failed to release EIP {allocation_id}: {e}")

    for alloc_id in set(deployed_resources['elastic_ips']):
        release_eip(alloc_id)

    # ------------------------------------------------------------------
    # 9. Delete ENIs (critical – most common VPC deletion blocker)
    # ------------------------------------------------------------------
    if deployed_resources['vpc_id']:
        vpc_id = deployed_resources['vpc_id']
        for attempt in range(20):
            enis = ec2.describe_network_interfaces(
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
            )['NetworkInterfaces']
            if not enis:
                logging.info("No ENIs found in VPC.")
                break
            logging.info(f"Found {len(enis)} ENIs (attempt {attempt+1}/20), deleting...")
            for eni in enis:
                try:
                    ec2.delete_network_interface(NetworkInterfaceId=eni['NetworkInterfaceId'])
                    logging.info(f"  Deleted ENI: {eni['NetworkInterfaceId']}")
                except Exception as e:
                    logging.warning(f"  Could not delete ENI {eni['NetworkInterfaceId']}: {e}")
            time.sleep(5)
        else:
            logging.warning("Some ENIs may still exist after 20 attempts.")

    # ------------------------------------------------------------------
    # 10. Delete Subnets (must be after ENIs)
    # ------------------------------------------------------------------
    all_subnets = deployed_resources['public_subnets'] + deployed_resources['private_subnets']
    for subnet_id in all_subnets:
        try:
            logging.info(f"Deleting subnet {subnet_id}")
            ec2.delete_subnet(SubnetId=subnet_id)
        except Exception as e:
            logging.error(f"Subnet deletion error for {subnet_id}: {e}")

    # ------------------------------------------------------------------
    # 11. Delete Custom Route Tables (non‑main)
    # ------------------------------------------------------------------
    for rt_id in deployed_resources['route_tables']:
        try:
            logging.info(f"Deleting route table {rt_id}")
            ec2.delete_route_table(RouteTableId=rt_id)
        except Exception as e:
            logging.error(f"Route table deletion error for {rt_id}: {e}")

    # ------------------------------------------------------------------
    # 12. Detach & Delete Internet Gateway
    # ------------------------------------------------------------------
    if deployed_resources['internet_gateway'] and deployed_resources['vpc_id']:
        try:
            logging.info("Detaching IGW")
            ec2.detach_internet_gateway(
                InternetGatewayId=deployed_resources['internet_gateway'],
                VpcId=deployed_resources['vpc_id']
            )
            time.sleep(5)
            logging.info("Deleting IGW")
            ec2.delete_internet_gateway(InternetGatewayId=deployed_resources['internet_gateway'])
        except Exception as e:
            logging.error(f"IGW deletion error: {e}")

    # ------------------------------------------------------------------
    # 13. Revoke security group cross‑references, then delete SGs
    # ------------------------------------------------------------------
    if deployed_resources['security_groups'] and deployed_resources['vpc_id']:
        vpc_id = deployed_resources['vpc_id']
        sg_ids = deployed_resources['security_groups']
        # Get full SG details
        sgs_full = ec2.describe_security_groups(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['SecurityGroups']
        for sg_full in sgs_full:
            if sg_full['GroupId'] not in sg_ids:
                continue
            sg_id = sg_full['GroupId']
            # Revoke ingress rules with UserIdGroupPairs
            for permission in sg_full.get('IpPermissions', []):
                if 'UserIdGroupPairs' in permission and permission['UserIdGroupPairs']:
                    try:
                        ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=[permission])
                        logging.info(f"  Revoked ingress rule from SG {sg_id}")
                    except Exception as e:
                        logging.debug(f"  Could not revoke ingress rule: {e}")
            # Revoke egress rules with UserIdGroupPairs
            for permission in sg_full.get('IpPermissionsEgress', []):
                if 'UserIdGroupPairs' in permission and permission['UserIdGroupPairs']:
                    try:
                        ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=[permission])
                        logging.info(f"  Revoked egress rule from SG {sg_id}")
                    except Exception as e:
                        logging.debug(f"  Could not revoke egress rule: {e}")

        # Now delete the security groups
        for sg_id in sg_ids:
            try:
                logging.info(f"Deleting security group {sg_id}")
                ec2.delete_security_group(GroupId=sg_id)
            except Exception as e:
                logging.error(f"SG deletion error for {sg_id}: {e}")

    # ------------------------------------------------------------------
    # 14. Delete VPC
    # ------------------------------------------------------------------
    if deployed_resources['vpc_id']:
        try:
            logging.info(f"Deleting VPC {deployed_resources['vpc_id']}")
            ec2.delete_vpc(VpcId=deployed_resources['vpc_id'])
            logging.info(f"VPC {deployed_resources['vpc_id']} deleted successfully")
        except Exception as e:
            logging.error(f"VPC deletion error: {e}")

    logging.warning("Rollback completed. Exiting.")
    sys.exit(1)
# ---------------------------------------------------------
# 3. Infrastructure Creation Functions
# ---------------------------------------------------------
def create_vpc():
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        logging.info("Creating VPC 10.0.0.0/16")
        vpc = ec2.create_vpc(CidrBlock='10.0.0.0/16')
        vpc_id = vpc['Vpc']['VpcId']
        deployed_resources['vpc_id'] = vpc_id
        ec2.get_waiter('vpc_available').wait(VpcIds=[vpc_id])
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})
        ec2.create_tags(Resources=[vpc_id], Tags=[{'Key': 'Name', 'Value': VPC_NAME}])
        logging.info(f"VPC {vpc_id} created")
        return vpc_id
    except Exception as e:
        logging.error(f"VPC creation failed: {e}")
        rollback()

def create_subnets(vpc_id):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        pub1 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.10.0/24', AvailabilityZone=f'{AWS_REGION}a')
        pub2 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.20.0/24', AvailabilityZone=f'{AWS_REGION}b')
        pub1_id, pub2_id = pub1['Subnet']['SubnetId'], pub2['Subnet']['SubnetId']
        deployed_resources['public_subnets'].extend([pub1_id, pub2_id])
        ec2.modify_subnet_attribute(SubnetId=pub1_id, MapPublicIpOnLaunch={'Value': True})
        ec2.modify_subnet_attribute(SubnetId=pub2_id, MapPublicIpOnLaunch={'Value': True})

        priv1 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.100.0/24', AvailabilityZone=f'{AWS_REGION}a')
        priv2 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.200.0/24', AvailabilityZone=f'{AWS_REGION}b')
        priv1_id, priv2_id = priv1['Subnet']['SubnetId'], priv2['Subnet']['SubnetId']
        deployed_resources['private_subnets'].extend([priv1_id, priv2_id])

        ec2.get_waiter('subnet_available').wait(SubnetIds=[pub1_id, pub2_id, priv1_id, priv2_id])
        for sid in [pub1_id, pub2_id, priv1_id, priv2_id]:
            ec2.create_tags(Resources=[sid], Tags=[{'Key': 'Name', 'Value': f'{VPC_NAME}-subnet-{sid[-4:]}'}])
        return pub1_id, pub2_id, priv1_id, priv2_id
    except Exception as e:
        logging.error(f"Subnet creation failed: {e}")
        rollback()

def create_security_groups(vpc_id):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        alb_sg = ec2.create_security_group(GroupName=f'alb-sg-{UNIQUE_ID}', Description='ALB SG', VpcId=vpc_id)
        alb_sg_id = alb_sg['GroupId']
        ec2.authorize_security_group_ingress(GroupId=alb_sg_id,
            IpPermissions=[{'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}])

        web_sg = ec2.create_security_group(GroupName=f'web-sg-{UNIQUE_ID}', Description='Web SG', VpcId=vpc_id)
        web_sg_id = web_sg['GroupId']
        ec2.authorize_security_group_ingress(GroupId=web_sg_id,
            IpPermissions=[
                {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'UserIdGroupPairs': [{'GroupId': alb_sg_id}]},
                {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443, 'UserIdGroupPairs': [{'GroupId': alb_sg_id}]}
            ])

        rds_sg = ec2.create_security_group(GroupName=f'rds-sg-{UNIQUE_ID}', Description='RDS SG', VpcId=vpc_id)
        rds_sg_id = rds_sg['GroupId']
        ec2.authorize_security_group_ingress(GroupId=rds_sg_id,
            IpPermissions=[{'IpProtocol': 'tcp', 'FromPort': 3306, 'ToPort': 3306, 'UserIdGroupPairs': [{'GroupId': web_sg_id}]}])

        deployed_resources['security_groups'] = [alb_sg_id, web_sg_id, rds_sg_id]
        for sg in [alb_sg_id, web_sg_id, rds_sg_id]:
            ec2.create_tags(Resources=[sg], Tags=[{'Key': 'Name', 'Value': f'{VPC_NAME}-{sg}'}])
        return alb_sg_id, web_sg_id, rds_sg_id
    except Exception as e:
        logging.error(f"SG creation failed: {e}")
        rollback()

def create_igw_and_attach(vpc_id):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        igw = ec2.create_internet_gateway()
        igw_id = igw['InternetGateway']['InternetGatewayId']
        deployed_resources['internet_gateway'] = igw_id
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        ec2.create_tags(Resources=[igw_id], Tags=[{'Key': 'Name', 'Value': f'{VPC_NAME}-igw'}])
        return igw_id
    except Exception as e:
        logging.error(f"IGW creation failed: {e}")
        rollback()

def create_nat_gateways(pub_sub1, pub_sub2):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        eip1 = ec2.allocate_address(Domain='vpc')['AllocationId']
        eip2 = ec2.allocate_address(Domain='vpc')['AllocationId']
        deployed_resources['elastic_ips'] = [eip1, eip2]

        nat1 = ec2.create_nat_gateway(SubnetId=pub_sub1, AllocationId=eip1)['NatGateway']['NatGatewayId']
        nat2 = ec2.create_nat_gateway(SubnetId=pub_sub2, AllocationId=eip2)['NatGateway']['NatGatewayId']
        deployed_resources['nat_gateways'] = [nat1, nat2]

        ec2.get_waiter('nat_gateway_available').wait(NatGatewayIds=[nat1, nat2])
        return nat1, nat2
    except Exception as e:
        logging.error(f"NAT/EIP creation failed: {e}")
        rollback()

def setup_routing(vpc_id, pub_sub1, pub_sub2, priv_sub1, priv_sub2, nat1, nat2, igw_id):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        # Public route table
        pub_rt = ec2.create_route_table(VpcId=vpc_id)['RouteTable']['RouteTableId']
        ec2.create_route(RouteTableId=pub_rt, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
        ec2.associate_route_table(SubnetId=pub_sub1, RouteTableId=pub_rt)
        ec2.associate_route_table(SubnetId=pub_sub2, RouteTableId=pub_rt)
        deployed_resources['route_tables'].append(pub_rt)

        # Private route tables (one per AZ)
        priv_rt1 = ec2.create_route_table(VpcId=vpc_id)['RouteTable']['RouteTableId']
        ec2.create_route(RouteTableId=priv_rt1, DestinationCidrBlock='0.0.0.0/0', NatGatewayId=nat1)
        ec2.associate_route_table(SubnetId=priv_sub1, RouteTableId=priv_rt1)
        deployed_resources['route_tables'].append(priv_rt1)

        priv_rt2 = ec2.create_route_table(VpcId=vpc_id)['RouteTable']['RouteTableId']
        ec2.create_route(RouteTableId=priv_rt2, DestinationCidrBlock='0.0.0.0/0', NatGatewayId=nat2)
        ec2.associate_route_table(SubnetId=priv_sub2, RouteTableId=priv_rt2)
        deployed_resources['route_tables'].append(priv_rt2)
    except Exception as e:
        logging.error(f"Routing setup failed: {e}")
        rollback()

def create_launch_template(web_sg_id):
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        user_data = '''#!/bin/bash
yum update -y
yum install -y httpd -q
systemctl start httpd
systemctl enable httpd

# Function to get metadata using IMDSv2
get_metadata() {
    local path=$1
    # Get token (valid for 6 seconds)
    TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 6")
    if [ -n "$TOKEN" ]; then
        curl -s -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$path"
    else
        # Fallback to IMDSv1
        curl -s "http://169.254.169.254/latest/meta-data/$path"
    fi
}

# Retry loop for instance ID
for i in {1..15}; do
    INSTANCE_ID=$(get_metadata "instance-id")
    if [ -n "$INSTANCE_ID" ]; then
        break
    fi
    echo "Attempt $i: Waiting for metadata service..."
    sleep 2
done

# Get availability zone
AZ=$(get_metadata "placement/availability-zone")

# Final fallback
INSTANCE_ID=${INSTANCE_ID:-"unknown-instance"}
AZ=${AZ:-"unknown-az"}

cat > /var/www/html/index.html <<EOF
<h1>Hello from $INSTANCE_ID in $AZ</h1>
<p>Metadata fetch status: $([ -n "$INSTANCE_ID" ] && echo "OK" || echo "FAILED")</p>
EOF
'''
        # Encode user data to Base64
        encoded_user_data = base64.b64encode(user_data.encode('utf-8')).decode('utf-8')

        ec2.create_launch_template(
            LaunchTemplateName=LAUNCH_TEMPLATE_NAME,
            LaunchTemplateData={
                'ImageId': AMI_ID,
                'InstanceType': 't3.micro',
                'SecurityGroupIds': [web_sg_id],
                'UserData': encoded_user_data,
                'BlockDeviceMappings': [{
                    'DeviceName': '/dev/xvda',
                    'Ebs': {'VolumeSize': 8, 'VolumeType': 'gp2', 'Encrypted': True}
                }]
            }
        )
        logging.info(f"Launch template {LAUNCH_TEMPLATE_NAME} created")
    except Exception as e:
        logging.error(f"Launch template creation failed: {e}")
        rollback()

def create_alb_and_target_group(vpc_id, pub_sub1, pub_sub2, alb_sg_id):
    elbv2 = boto3.client('elbv2', region_name=AWS_REGION)
    try:
        tg = elbv2.create_target_group(Name=TG_NAME, Protocol='HTTP', Port=80, VpcId=vpc_id, HealthCheckPath='/')
        tg_arn = tg['TargetGroups'][0]['TargetGroupArn']
        deployed_resources['target_group_arn'] = tg_arn

        alb = elbv2.create_load_balancer(Name=ALB_NAME, Subnets=[pub_sub1, pub_sub2], SecurityGroups=[alb_sg_id])
        alb_arn = alb['LoadBalancers'][0]['LoadBalancerArn']
        deployed_resources['load_balancer_arn'] = alb_arn

        elbv2.create_listener(LoadBalancerArn=alb_arn, Protocol='HTTP', Port=80,
                              DefaultActions=[{'Type': 'forward', 'TargetGroupArn': tg_arn}])
        elbv2.get_waiter('load_balancer_available').wait(LoadBalancerArns=[alb_arn])
        return tg_arn, alb_arn
    except Exception as e:
        logging.error(f"ALB/TG creation failed: {e}")
        rollback()

def create_auto_scaling_group(priv_sub1, priv_sub2, tg_arn):
    autoscaling = boto3.client('autoscaling', region_name=AWS_REGION)
    try:
        autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=ASG_NAME,
            LaunchTemplate={'LaunchTemplateName': LAUNCH_TEMPLATE_NAME, 'Version': '$Latest'},
            MinSize=2,
            MaxSize=5,
            DesiredCapacity=2,
            TargetGroupARNs=[tg_arn],
            VPCZoneIdentifier=f"{priv_sub1},{priv_sub2}"
        )
        logging.info(f"Auto Scaling Group {ASG_NAME} created")
    except Exception as e:
        logging.error(f"ASG creation failed: {e}")
        rollback()

def create_rds(priv_sub1, priv_sub2, rds_sg_id):
    rds_client = boto3.client('rds', region_name=AWS_REGION)
    try:
        rds_client.create_db_subnet_group(
            DBSubnetGroupName=DB_SUBNET_GROUP,
            DBSubnetGroupDescription='RDS subnet group',
            SubnetIds=[priv_sub1, priv_sub2]
        )
        rds_client.create_db_instance(
            DBInstanceIdentifier=DB_INSTANCE_ID,
            DBInstanceClass='db.t3.micro',
            Engine='mysql',
            AllocatedStorage=20,
            MasterUsername=DB_USERNAME,
            MasterUserPassword=DB_PASSWORD,
            DBSubnetGroupName=DB_SUBNET_GROUP,
            VpcSecurityGroupIds=[rds_sg_id],
            PubliclyAccessible=False,
            StorageEncrypted=True
        )
        logging.info(f"RDS instance {DB_INSTANCE_ID} creation initiated")
    except Exception as e:
        logging.error(f"RDS creation failed: {e}")
        rollback()

# ---------------------------------------------------------
# 4. Main Workflow
# ---------------------------------------------------------
def main():
    logging.info("Starting Multi-Tier AWS Deployment")
    try:
        vpc_id = create_vpc()
        pub1, pub2, priv1, priv2 = create_subnets(vpc_id)
        alb_sg, web_sg, rds_sg = create_security_groups(vpc_id)
        igw_id = create_igw_and_attach(vpc_id)
        nat1, nat2 = create_nat_gateways(pub1, pub2)
        setup_routing(vpc_id, pub1, pub2, priv1, priv2, nat1, nat2, igw_id)

        create_launch_template(web_sg)
        tg_arn, alb_arn = create_alb_and_target_group(vpc_id, pub1, pub2, alb_sg)
        create_auto_scaling_group(priv1, priv2, tg_arn) 
        create_rds(priv1, priv2, rds_sg)

        logging.info("Deployment completed successfully!")
        alb_dns = boto3.client('elbv2', region_name=AWS_REGION).describe_load_balancers(LoadBalancerArns=[alb_arn])['LoadBalancers'][0]['DNSName']
        logging.info(f"Access your application at: http://{alb_dns}")
    except Exception as e:
        logging.error(f"Deployment failed: {e}")
        rollback()

if __name__ == "__main__":
    main()