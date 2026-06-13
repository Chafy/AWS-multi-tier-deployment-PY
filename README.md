# AWS Multi-Tier Deployment & Automation Utility

## 🚀 Overview
This project is an automated infrastructure cleanup utility built with **Python** and **Boto3**. It was developed as a capstone project after completing advanced courses in Python Automation and Git/GitHub.

The objective of this script is to perform a clean tear-down of complex AWS multi-tier infrastructure, ensuring that no orphaned resources remain in the AWS account after a project or testing cycle is finished.

## 💡 The Challenge: Dependency Hell
AWS infrastructure is highly interdependent. Manually deleting a VPC often leads to the dreaded `DependencyViolation` error because resources are linked in a specific hierarchy. 

**Key issues resolved in this project:**
* **Resource Dependencies:** AWS prevents VPC deletion if Subnets, Route Tables, or Network Interfaces are still active.
* **Orphaned Resources:** Manually cleaning up RDS Subnet Groups, Launch Templates, and Auto Scaling Groups is tedious and prone to human error.
* **Visibility:** Identifying multiple VPCs and distinguishing between default and custom environments.

## 🛠 Features
* **Full Stack Cleanup:** Automatically terminates/deletes:
    * **RDS:** Instances and custom DB Subnet Groups.
    * **Compute:** Auto Scaling Groups and Launch Templates.
    * **Networking:** Load Balancers (ALB), Target Groups, NAT Gateways, and ENIs.
    * **Security:** Custom Security Groups (including `ALBSG`).
    * **Routing:** Iterates through and removes custom Route Tables to ensure successful VPC deletion.
* **Safety First:** Includes error handling (`try-except` blocks) to ensure the script continues even if a resource has already been manually deleted.
* **VPC-Awareness:** Intelligently identifies non-default VPCs to prevent accidental deletion of essential account networking.

## ⚙️ How to use
1. Ensure you have the `boto3` library installed: `pip install boto3`
2. Configure your AWS credentials using `aws configure`.
3. Run the script: `python smart_cleanup.py`

## 🎓 Learning Journey
This project was the final result of my transition from learning Python basics to building real-world automation tools. It represents my progress in:
* AWS SDK (Boto3) implementation.
* Handling complex API dependencies.
* Version control best practices using Git/GitHub.

---
*Built by Chafy*
