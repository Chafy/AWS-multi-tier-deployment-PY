# AWS Multi-Tier Deployment & Cleanup Utility

## 🚀 Overview
This project is an automated Infrastructure-as-Code (IaC) utility built with **Python** and **Boto3**. It manages the full lifecycle of a multi-tier AWS architecture, ensuring that complex environments can be deployed and—most importantly—completely cleaned up without leaving behind orphaned resources.

## 💡 The Engineering Challenge: "Dependency Hell"
AWS resources are highly interdependent. Manually tearing down a VPC often leads to `DependencyViolation` errors because resources (like Network Interfaces, NAT Gateways, and Route Tables) must be deleted in a specific order[cite: 1].

**Key technical challenges addressed:**
* **Strict Deletion Order:** The script implements a precise, logical teardown sequence to satisfy AWS dependency requirements[cite: 1].
* **Intelligent Rollback:** Using `try-except` blocks and `boto3` waiters, the script ensures that if a deployment fails, the environment is automatically reverted to a clean state, preventing "zombie" resources[cite: 1].
* **Resource Observability:** Integrated a professional logging system that tracks changes in real-time, outputting to both the console and a dedicated `aws_deployment.log` file for post-mortem debugging[cite: 1].

## 🛠 Features
* **Full-Stack Automation:** Handles the lifecycle of VPCs, Subnets, Security Groups, NAT Gateways, Application Load Balancers (ALB), Auto Scaling Groups (ASG), and RDS instances[cite: 1].
* **Defensive Coding:** Includes robust error handling and wait-logic to manage API latency and ensure resources are fully provisioned (or terminated) before the next step begins[cite: 1].
* **Observability:** Dual-stream logging (Console + File) provides traceability for every infrastructure change[cite: 1].

## ⚙️ How to Use
1. **Requirements:** Ensure you have `boto3` installed (`pip install boto3`).
2. **Environment:** Set your `AWS_REGION` and `DB_PASSWORD` as environment variables.
3. **Execution:** 
```bash
   python project.py
