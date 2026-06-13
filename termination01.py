import boto3
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def release_eip(allocation_id):
    """Release a single Elastic IP, ignoring already-released or invalid IDs."""
    ec2 = boto3.client('ec2', region_name='us-east-1')
    try:
        eips = ec2.describe_addresses(AllocationIds=[allocation_id])['Addresses']
        if not eips:
            logger.info(f"EIP {allocation_id} not found (already released?)")
            return
        eip = eips[0]
        if 'AssociationId' in eip:
            assoc_id = eip['AssociationId']
            try:
                logger.info(f"Disassociating EIP {allocation_id} (association {assoc_id})")
                ec2.disassociate_address(AssociationId=assoc_id)
                time.sleep(2)
            except Exception as e:
                err = str(e)
                if 'AuthFailure' in err or 'InvalidAssociationID' in err:
                    logger.info(f"EIP {allocation_id} already disassociated or association invalid")
                else:
                    logger.warning(f"Unexpected error during disassociation: {e}")
        logger.info(f"Releasing EIP {allocation_id}")
        ec2.release_address(AllocationId=allocation_id)
        logger.info(f"✅ Released EIP {allocation_id}")
    except Exception as e:
        err = str(e)
        if 'InvalidAllocationID' in err or 'AuthFailure' in err:
            logger.info(f"EIP {allocation_id} already released or not found")
        else:
            logger.error(f"Failed to release EIP {allocation_id}: {e}")

def delete_all_non_default_vpcs():
    ec2 = boto3.client('ec2', region_name='us-east-1')
    elbv2 = boto3.client('elbv2', region_name='us-east-1')
    asg = boto3.client('autoscaling', region_name='us-east-1')
    rds = boto3.client('rds', region_name='us-east-1')

    # ----- Global resources -----
    logger.info("Deleting all RDS instances...")
    try:
        for db in rds.describe_db_instances()['DBInstances']:
            db_id = db['DBInstanceIdentifier']
            logger.info(f"  Deleting RDS: {db_id}")
            rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
            time.sleep(2)
    except Exception as e:
        logger.warning(f"RDS cleanup: {e}")

    logger.info("Deleting all DB subnet groups...")
    try:
        for group in rds.describe_db_subnet_groups()['DBSubnetGroups']:
            group_name = group['DBSubnetGroupName']
            logger.info(f"  Deleting DB subnet group: {group_name}")
            rds.delete_db_subnet_group(DBSubnetGroupName=group_name)
            time.sleep(1)
    except Exception as e:
        logger.warning(f"DB subnet group cleanup: {e}")

    logger.info("Deleting all Auto Scaling Groups...")
    try:
        for group in asg.describe_auto_scaling_groups()['AutoScalingGroups']:
            name = group['AutoScalingGroupName']
            logger.info(f"  Deleting ASG: {name}")
            asg.delete_auto_scaling_group(AutoScalingGroupName=name, ForceDelete=True)
            time.sleep(2)
    except Exception as e:
        logger.warning(f"ASG cleanup: {e}")

    logger.info("Deleting all Load Balancers...")
    try:
        for lb in elbv2.describe_load_balancers()['LoadBalancers']:
            arn = lb['LoadBalancerArn']
            logger.info(f"  Deleting ALB: {lb['LoadBalancerName']}")
            elbv2.delete_load_balancer(LoadBalancerArn=arn)
            time.sleep(5)
    except Exception as e:
        logger.warning(f"ALB cleanup: {e}")

    logger.info("Deleting all Target Groups...")
    try:
        for tg in elbv2.describe_target_groups()['TargetGroups']:
            arn = tg['TargetGroupArn']
            logger.info(f"  Deleting Target Group: {tg['TargetGroupName']}")
            elbv2.delete_target_group(TargetGroupArn=arn)
    except Exception as e:
        logger.warning(f"Target Group cleanup: {e}")

    logger.info("Terminating all running EC2 instances...")
    try:
        instances = ec2.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['running', 'pending']}])
        instance_ids = [i['InstanceId'] for r in instances['Reservations'] for i in r['Instances']]
        if instance_ids:
            ec2.terminate_instances(InstanceIds=instance_ids)
            logger.info(f"  Terminated: {instance_ids}")
            time.sleep(30)
    except Exception as e:
        logger.warning(f"Instance termination: {e}")

    # ----- Non‑default VPCs -----
    all_vpcs = ec2.describe_vpcs()['Vpcs']
    non_default_vpcs = [vpc for vpc in all_vpcs if not vpc.get('IsDefault', False)]
    if not non_default_vpcs:
        logger.info("No non-default VPCs found.")
        return

    logger.info(f"Found {len(non_default_vpcs)} non-default VPCs to clean.")

    for vpc in non_default_vpcs:
        vpc_id = vpc['VpcId']
        logger.info(f"\n=== Cleaning VPC: {vpc_id} ===")

        # 1. NAT Gateways
        nats = ec2.describe_nat_gateways(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['NatGateways']
        for nat in nats:
            if nat['State'] != 'deleted':
                logger.info(f"  Deleting NAT: {nat['NatGatewayId']}")
                ec2.delete_nat_gateway(NatGatewayId=nat['NatGatewayId'])
        if nats:
            logger.info("  Waiting 30s for NAT deletion...")
            time.sleep(30)

        # 2. Elastic IPs - now using robust release function
        eips = ec2.describe_addresses()['Addresses']
        for eip in eips:
            release_eip(eip['AllocationId'])

        # 3. Internet Gateway
        igws = ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])['InternetGateways']
        for igw in igws:
            for attach in igw.get('Attachments', []):
                if attach['VpcId'] == vpc_id:
                    ec2.detach_internet_gateway(InternetGatewayId=igw['InternetGatewayId'], VpcId=vpc_id)
                    logger.info(f"  Detached IGW: {igw['InternetGatewayId']}")
                    time.sleep(2)
                    ec2.delete_internet_gateway(InternetGatewayId=igw['InternetGatewayId'])
                    logger.info(f"  Deleted IGW: {igw['InternetGatewayId']}")

        # 4. Network Interfaces (ENIs) – retry until gone (most common blocker)
        for attempt in range(15):
            enis = ec2.describe_network_interfaces(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['NetworkInterfaces']
            if not enis:
                break
            logger.info(f"  Found {len(enis)} ENIs (attempt {attempt+1}/15), deleting...")
            for eni in enis:
                try:
                    ec2.delete_network_interface(NetworkInterfaceId=eni['NetworkInterfaceId'])
                    logger.info(f"    Deleted ENI: {eni['NetworkInterfaceId']}")
                except Exception as e:
                    logger.warning(f"    Could not delete ENI {eni['NetworkInterfaceId']}: {e}")
            time.sleep(5)
        else:
            logger.warning("  Some ENIs may still exist after 15 attempts.")

        # 5. Subnets
        subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['Subnets']
        if subnets:
            logger.info(f"  Found {len(subnets)} subnets, deleting...")
            for subnet in subnets:
                try:
                    ec2.delete_subnet(SubnetId=subnet['SubnetId'])
                    logger.info(f"    Deleted subnet: {subnet['SubnetId']}")
                except Exception as e:
                    logger.warning(f"    Subnet deletion error: {e}")

        # 6. Custom Route Tables (non‑main)
        rts = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['RouteTables']
        for rt in rts:
            is_main = any(assoc.get('Main', False) for assoc in rt.get('Associations', []))
            if not is_main:
                try:
                    ec2.delete_route_table(RouteTableId=rt['RouteTableId'])
                    logger.info(f"  Deleted route table: {rt['RouteTableId']}")
                except Exception as e:
                    logger.warning(f"  Route table deletion error: {e}")

        # 7. Security Groups (non‑default)
        sgs = ec2.describe_security_groups(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['SecurityGroups']
        for sg in sgs:
            if sg['GroupName'] != 'default':
                try:
                    ec2.delete_security_group(GroupId=sg['GroupId'])
                    logger.info(f"  Deleted security group: {sg['GroupId']}")
                except Exception as e:
                    logger.warning(f"  Security group deletion error: {e}")

        # 8. Finally, VPC
        try:
            ec2.delete_vpc(VpcId=vpc_id)
            logger.info(f"  ✅ Deleted VPC: {vpc_id}")
        except Exception as e:
            logger.error(f"  Could not delete VPC {vpc_id}: {e}")
            logger.error("  If still stuck, manually delete from AWS Console using 'Delete with dependencies'.")

    logger.info("✅ Full cleanup completed for all non-default VPCs.")

if __name__ == "__main__":
    delete_all_non_default_vpcs()