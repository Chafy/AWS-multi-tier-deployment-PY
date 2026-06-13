import boto3
import logging
import sys
import time
import os

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
AWS_REGION = "us-east-1"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Helper: find resources by suffix
# ---------------------------------------------------------
def find_resources_by_suffix(suffix):
    """Return dict of resource IDs that contain the suffix in their name."""
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    elbv2 = boto3.client('elbv2', region_name=AWS_REGION)
    autoscaling = boto3.client('autoscaling', region_name=AWS_REGION)
    rds = boto3.client('rds', region_name=AWS_REGION)

    resources = {
        'vpc_id': None,
        'public_subnets': [],
        'private_subnets': [],
        'security_groups': [],
        'internet_gateway': None,
        'nat_gateways': [],
        'elastic_ips': [],
        'load_balancer_arn': None,
        'target_group_arn': None,
        'auto_scaling_group': None,
        'launch_template': None,
        'db_instance': None,
        'db_subnet_group': None,
        'route_tables': []
    }

    # 1. Find VPC by name tag
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'tag:Name', 'Values': [f'multi-tier-vpc-{suffix}']}])
    if not vpcs['Vpcs']:
        logger.error(f"No VPC found with name multi-tier-vpc-{suffix}")
        return None
    vpc_id = vpcs['Vpcs'][0]['VpcId']
    resources['vpc_id'] = vpc_id
    logger.info(f"Found VPC: {vpc_id}")

    # 2. Subnets (by VPC)
    subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for subnet in subnets['Subnets']:
        cidr = subnet['CidrBlock']
        if cidr.startswith('10.0.10') or cidr.startswith('10.0.20'):
            resources['public_subnets'].append(subnet['SubnetId'])
        else:
            resources['private_subnets'].append(subnet['SubnetId'])
    logger.info(f"Found {len(resources['public_subnets'])} public and {len(resources['private_subnets'])} private subnets")

    # 3. Security Groups (non-default)
    sgs = ec2.describe_security_groups(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for sg in sgs['SecurityGroups']:
        if sg['GroupName'] != 'default':
            resources['security_groups'].append(sg['GroupId'])
    logger.info(f"Found {len(resources['security_groups'])} security groups")

    # 4. Internet Gateway
    igws = ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])
    if igws['InternetGateways']:
        resources['internet_gateway'] = igws['InternetGateways'][0]['InternetGatewayId']
        logger.info(f"Found IGW: {resources['internet_gateway']}")

    # 5. NAT Gateways
    nat_gws = ec2.describe_nat_gateways(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for nat in nat_gws['NatGateways']:
        if nat['State'] != 'deleted':
            resources['nat_gateways'].append(nat['NatGatewayId'])
            for addr in nat.get('NatGatewayAddresses', []):
                if 'AllocationId' in addr:
                    resources['elastic_ips'].append(addr['AllocationId'])
    logger.info(f"Found {len(resources['nat_gateways'])} NAT gateways")

    # 6. Route Tables (non-main)
    rtbs = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for rtb in rtbs['RouteTables']:
        if not rtb.get('Associations', [{}])[0].get('Main', False):
            resources['route_tables'].append(rtb['RouteTableId'])
    logger.info(f"Found {len(resources['route_tables'])} custom route tables")

    # 7. Load Balancer – ensure we only pick a real load balancer
    lbs = elbv2.describe_load_balancers()['LoadBalancers']
    for lb in lbs:
        if lb['VpcId'] == vpc_id and 'loadbalancer' in lb['LoadBalancerArn']:
            resources['load_balancer_arn'] = lb['LoadBalancerArn']
            logger.info(f"Found ALB: {lb['LoadBalancerName']}")
            break
    if resources['load_balancer_arn'] is None:
        logger.warning("No valid load balancer ARN found (missing or already deleted)")

    # 8. Target Group
    tgs = elbv2.describe_target_groups()['TargetGroups']
    for tg in tgs:
        if tg['VpcId'] == vpc_id:
            resources['target_group_arn'] = tg['TargetGroupArn']
            logger.info(f"Found Target Group: {tg['TargetGroupName']}")
            break

       # 9. Auto Scaling Group (by exact name pattern)
    asg_name_pattern = f'web-asg-{suffix}'
    asgs = autoscaling.describe_auto_scaling_groups()['AutoScalingGroups']
    found_asg = None
    for asg in asgs:
        if asg['AutoScalingGroupName'] == asg_name_pattern:
            found_asg = asg['AutoScalingGroupName']
            break
    if found_asg:
        resources['auto_scaling_group'] = found_asg
        logger.info(f"Found ASG: {found_asg}")
    else:
        logger.warning(f"ASG with name {asg_name_pattern} not found")

    # 10. Launch Template
    try:
        lt = ec2.describe_launch_templates(LaunchTemplateNames=[f'web-lt-{suffix}'])
        resources['launch_template'] = lt['LaunchTemplates'][0]['LaunchTemplateName']
        logger.info(f"Found Launch Template: {resources['launch_template']}")
    except:
        pass

    # 11. RDS DB Instance
    try:
        db = rds.describe_db_instances(DBInstanceIdentifier=f'web-db-{suffix}')
        resources['db_instance'] = db['DBInstances'][0]['DBInstanceIdentifier']
        logger.info(f"Found RDS: {resources['db_instance']}")
    except:
        pass

    # 12. RDS Subnet Group
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=f'db-subnet-group-{suffix}')
        resources['db_subnet_group'] = f'db-subnet-group-{suffix}'
        logger.info(f"Found DB Subnet Group: {resources['db_subnet_group']}")
    except:
        pass

    # 13. Running EC2 instances
    instances = ec2.describe_instances(Filters=[
        {'Name': 'vpc-id', 'Values': [vpc_id]},
        {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
    ])
    for reservation in instances['Reservations']:
        for inst in reservation['Instances']:
            resources.setdefault('running_instances', []).append(inst['InstanceId'])
    if resources.get('running_instances'):
        logger.info(f"Found {len(resources['running_instances'])} running EC2 instances")

    return resources

# ---------------------------------------------------------
# Deletion functions 
# ---------------------------------------------------------
def delete_rds_and_wait(db_id):
    if not db_id:
        return
    rds = boto3.client('rds', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting RDS instance {db_id} (skip final snapshot)")
        rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
        logger.info("Waiting for RDS to be deleted (this takes ~10-15 minutes)...")
        waiter = rds.get_waiter('db_instance_deleted')
        waiter.wait(DBInstanceIdentifier=db_id)
        logger.info("RDS instance deleted successfully")
    except Exception as e:
        logger.error(f"RDS deletion error: {e}")

def delete_db_subnet_group(name):
    if not name:
        return
    rds = boto3.client('rds', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting DB subnet group {name}")
        rds.delete_db_subnet_group(DBSubnetGroupName=name)
    except Exception as e:
        logger.error(f"DB subnet group deletion error: {e}")

def delete_auto_scaling_group(asg_name):
    if not asg_name:
        return
    autoscaling = boto3.client('autoscaling', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting ASG {asg_name} (force delete)")
        autoscaling.delete_auto_scaling_group(AutoScalingGroupName=asg_name, ForceDelete=True)
        time.sleep(30)  # let instances terminate
    except Exception as e:
        logger.error(f"ASG deletion error: {e}")

def delete_launch_template(lt_name):
    if not lt_name:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting launch template {lt_name}")
        ec2.delete_launch_template(LaunchTemplateName=lt_name)
    except Exception as e:
        logger.error(f"Launch template deletion error: {e}")

def delete_load_balancer(alb_arn):
    if not alb_arn:
        return
    elbv2 = boto3.client('elbv2', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting load balancer {alb_arn}")
        elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
        waiter = elbv2.get_waiter('load_balancers_deleted')
        waiter.wait(LoadBalancerArns=[alb_arn])
        logger.info("Load balancer deleted")
    except Exception as e:
        logger.error(f"ALB deletion error: {e}")

def delete_target_group(tg_arn):
    """Delete target group with retry on ResourceInUse (still attached to listener)."""
    if not tg_arn:
        return
    elbv2 = boto3.client('elbv2', region_name=AWS_REGION)
    # Give the ALB a few seconds to fully release the target group
    time.sleep(10)
    try:
        logger.info(f"Deleting target group {tg_arn}")
        elbv2.delete_target_group(TargetGroupArn=tg_arn)
    except Exception as e:
        if 'ResourceInUse' in str(e):
            logger.warning("Target group still in use – waiting 30s and retrying...")
            time.sleep(30)
            try:
                elbv2.delete_target_group(TargetGroupArn=tg_arn)
            except Exception as e2:
                logger.error(f"Target group deletion still failing: {e2}")
        else:
            logger.error(f"Target group deletion error: {e}")

def delete_running_instances(instance_ids):
    if not instance_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    logger.info(f"Terminating instances: {instance_ids}")
    ec2.terminate_instances(InstanceIds=instance_ids)
    # Wait for termination
    waiter = ec2.get_waiter('instance_terminated')
    waiter.wait(InstanceIds=instance_ids)
    logger.info("Instances terminated")

def delete_nat_gateways(nat_ids):
    if not nat_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for nat_id in nat_ids:
        try:
            logger.info(f"Deleting NAT Gateway {nat_id}")
            ec2.delete_nat_gateway(NatGatewayId=nat_id)
        except Exception as e:
            logger.error(f"NAT deletion error: {e}")
    if nat_ids:
        logger.info("Waiting for NAT gateways to be deleted...")
        try:
            waiter = ec2.get_waiter('nat_gateway_deleted')
            waiter.wait(NatGatewayIds=nat_ids)
        except:
            time.sleep(60)
        logger.info("NAT gateways deleted")

def release_elastic_ips(allocation_ids):
    """Release multiple Elastic IPs, ignoring already-released or invalid IDs."""
    if not allocation_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for alloc_id in set(allocation_ids):
        try:
            # Describe the EIP
            eips = ec2.describe_addresses(AllocationIds=[alloc_id])['Addresses']
            if not eips:
                logger.info(f"EIP {alloc_id} not found (already released?)")
                continue
            eip = eips[0]
            # Disassociate if needed
            if eip.get('AssociationId'):
                assoc_id = eip['AssociationId']
                try:
                    logger.info(f"Disassociating EIP {alloc_id} (association {assoc_id})")
                    ec2.disassociate_address(AssociationId=assoc_id)
                    time.sleep(2)
                except Exception as e:
                    err = str(e)
                    if 'AuthFailure' in err or 'InvalidAssociationID' in err:
                        logger.info(f"EIP {alloc_id} already disassociated or association invalid")
                    else:
                        logger.warning(f"Unexpected error during disassociation: {e}")
            # Release
            logger.info(f"Releasing EIP {alloc_id}")
            ec2.release_address(AllocationId=alloc_id)
            logger.info(f"✅ Released EIP {alloc_id}")
        except Exception as e:
            err = str(e)
            if 'InvalidAllocationID' in err or 'AuthFailure' in err:
                logger.info(f"EIP {alloc_id} already released or not found")
            else:
                logger.error(f"Failed to release EIP {alloc_id}: {e}")

def delete_internet_gateway(igw_id, vpc_id):
    if not igw_id:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        # Ensure all EIPs are gone before detaching IGW
        logger.info(f"Detaching IGW {igw_id} from VPC {vpc_id}")
        ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        time.sleep(5)
        logger.info(f"Deleting IGW {igw_id}")
        ec2.delete_internet_gateway(InternetGatewayId=igw_id)
    except Exception as e:
        logger.error(f"IGW deletion error: {e}")

def delete_enis_in_vpc(vpc_id):
    """Retry ENI deletion - critical for VPC cleanup."""
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for attempt in range(20):  # increased attempts
        enis = ec2.describe_network_interfaces(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['NetworkInterfaces']
        if not enis:
            logger.info("No ENIs found in VPC.")
            return True
        logger.info(f"Found {len(enis)} ENIs (attempt {attempt+1}/20), deleting...")
        for eni in enis:
            try:
                ec2.delete_network_interface(NetworkInterfaceId=eni['NetworkInterfaceId'])
                logger.info(f"  Deleted ENI: {eni['NetworkInterfaceId']}")
            except Exception as e:
                logger.warning(f"  Could not delete ENI {eni['NetworkInterfaceId']}: {e}")
        time.sleep(5)
    logger.warning("Some ENIs may still exist after 20 attempts.")
    return False

def delete_subnets(subnet_ids):
    if not subnet_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for sid in subnet_ids:
        try:
            logger.info(f"Deleting subnet {sid}")
            ec2.delete_subnet(SubnetId=sid)
        except Exception as e:
            logger.error(f"Subnet deletion error for {sid}: {e}")

def delete_route_tables(rt_ids):
    if not rt_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for rtid in rt_ids:
        try:
            logger.info(f"Deleting route table {rtid}")
            ec2.delete_route_table(RouteTableId=rtid)
        except Exception as e:
            logger.error(f"Route table deletion error for {rtid}: {e}")

def revoke_security_group_rules(sg_ids, vpc_id):
    """Revoke all ingress/egress rules that reference other security groups."""
    if not sg_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
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
                    logger.info(f"  Revoked ingress rule from SG {sg_id}")
                except Exception as e:
                    logger.debug(f"  Could not revoke ingress rule: {e}")
        # Revoke egress rules with UserIdGroupPairs
        for permission in sg_full.get('IpPermissionsEgress', []):
            if 'UserIdGroupPairs' in permission and permission['UserIdGroupPairs']:
                try:
                    ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=[permission])
                    logger.info(f"  Revoked egress rule from SG {sg_id}")
                except Exception as e:
                    logger.debug(f"  Could not revoke egress rule: {e}")

def delete_security_groups(sg_ids):
    if not sg_ids:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    for sg_id in sg_ids:
        try:
            logger.info(f"Deleting security group {sg_id}")
            ec2.delete_security_group(GroupId=sg_id)
        except Exception as e:
            logger.error(f"SG deletion error for {sg_id}: {e}")

def delete_vpc(vpc_id):
    if not vpc_id:
        return
    ec2 = boto3.client('ec2', region_name=AWS_REGION)
    try:
        logger.info(f"Deleting VPC {vpc_id}")
        ec2.delete_vpc(VpcId=vpc_id)
        logger.info(f"VPC {vpc_id} deleted successfully")
    except Exception as e:
        logger.error(f"VPC deletion error: {e}")

# ---------------------------------------------------------
# Main workflow (correct order)
# ---------------------------------------------------------
def main():
    suffix = os.environ.get('SUFFIX')
    if not suffix and len(sys.argv) > 1:
        suffix = sys.argv[1]
    if not suffix:
        logger.error("No suffix provided. Set environment variable SUFFIX or pass as command line argument.")
        logger.error("Example: SUFFIX=20260607103027 python CLEANUP_FIXED.py")
        sys.exit(1)

    logger.info(f"Using suffix: {suffix}")

    resources = find_resources_by_suffix(suffix)
    if not resources or not resources['vpc_id']:
        logger.error("No resources found for that suffix. Exiting.")
        return

    # Confirmation
    print("\n🔴 The following resources will be DELETED:")
    for key, value in resources.items():
        if value:
            print(f"  {key}: {value}")
    confirm = input("\nType 'DELETE' to proceed: ")
    if confirm != 'DELETE':
        logger.info("Aborted.")
        return

    vpc_id = resources['vpc_id']

    # ---- Order matters ----
    # 1. RDS (takes longest, start early)
    delete_rds_and_wait(resources['db_instance'])

    # 2. DB subnet group (only after RDS is gone)
    delete_db_subnet_group(resources['db_subnet_group'])

    # 3. Auto Scaling Group (stops instances)
    delete_auto_scaling_group(resources['auto_scaling_group'])

    # 4. Launch Template
    delete_launch_template(resources['launch_template'])

    # 5. Load Balancer & Target Group
    # Only delete load balancer if the ARN looks valid
    if resources['load_balancer_arn'] and 'loadbalancer' in resources['load_balancer_arn']:
        delete_load_balancer(resources['load_balancer_arn'])
    else:
        logger.warning("Load balancer ARN missing or invalid – skipping load balancer deletion.")
    delete_target_group(resources['target_group_arn'])

    # 6. EC2 instances (if any still running)
    delete_running_instances(resources.get('running_instances', []))

    # 7. NAT Gateways
    delete_nat_gateways(resources['nat_gateways'])

    # 8. Release Elastic IPs (critical before IGW detach)
    release_elastic_ips(resources['elastic_ips'])

    # 9. Internet Gateway (now EIPs are gone)
    delete_internet_gateway(resources['internet_gateway'], vpc_id)

    # 10. ENIs (retry loop)
    delete_enis_in_vpc(vpc_id)

    # 11. Subnets
    all_subnets = resources['public_subnets'] + resources['private_subnets']
    delete_subnets(all_subnets)

    # 12. Custom Route Tables
    delete_route_tables(resources['route_tables'])

    # 13. Security Groups - revoke cross-references first
    revoke_security_group_rules(resources['security_groups'], vpc_id)
    delete_security_groups(resources['security_groups'])

    # 14. VPC
    delete_vpc(vpc_id)

    logger.info("✅ Full cleanup completed.")

if __name__ == "__main__":
    main()