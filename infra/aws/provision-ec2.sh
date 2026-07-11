#!/usr/bin/env bash
# Provisions the EC2 instance that hosts the public Quorum demo.
#
# Run this from AWS CloudShell (uses the console session's own credentials --
# no static keys needed) once signed into the AWS console, account
# 458443189848, region us-east-1. Idempotent-ish: re-running skips steps
# whose resource already exists by name/tag.
#
# What it creates: an IAM role scoped to S3 PutObject/GetObject/ListBucket on
# the audit bucket only (no broad permissions, no static keys on the box),
# a key pair, a security group open on 22/80/443 only, a t3.large Ubuntu
# instance with that role attached, and an Elastic IP.
#
# It does NOT configure DNS, clone the repo, or start docker compose --
# that happens over SSH after this script prints the Elastic IP (see the
# "Next steps" it prints at the end).

set -euo pipefail

REGION="us-east-1"
BUCKET="quorum-hackathon-audit-458443189848"
ROLE_NAME="quorum-ec2-role"
POLICY_NAME="quorum-ec2-s3-audit-policy"
PROFILE_NAME="quorum-ec2-profile"
KEY_NAME="quorum-key"
SG_NAME="quorum-sg"
INSTANCE_NAME="quorum-demo"
INSTANCE_TYPE="t3.large"

echo "== IAM role =="
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }' >/dev/null
  echo "created role $ROLE_NAME"
else
  echo "role $ROLE_NAME already exists"
fi

cat > /tmp/quorum-s3-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::${BUCKET}", "arn:aws:s3:::${BUCKET}/*"]
  }]
}
EOF
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" \
  --policy-document file:///tmp/quorum-s3-policy.json
echo "attached scoped S3 policy to $ROLE_NAME"

if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME"
  echo "created instance profile $PROFILE_NAME"
  echo "waiting 10s for IAM propagation..."
  sleep 10
else
  echo "instance profile $PROFILE_NAME already exists"
fi

echo "== Key pair =="
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws ec2 create-key-pair --key-name "$KEY_NAME" --region "$REGION" \
    --query 'KeyMaterial' --output text > /tmp/"$KEY_NAME".pem
  chmod 400 /tmp/"$KEY_NAME".pem
  echo "created key pair, private key at /tmp/${KEY_NAME}.pem -- download this from CloudShell before it's lost (Actions > Download file)"
else
  echo "key pair $KEY_NAME already exists (private key not re-downloadable if you don't already have it)"
fi

echo "== Security group =="
VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  SG_ID=$(aws ec2 create-security-group --region "$REGION" --group-name "$SG_NAME" \
    --description "Quorum demo -- 22/80/443 only" --vpc-id "$VPC_ID" --query 'GroupId' --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --ip-permissions \
      'IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]' \
      'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
      'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]' >/dev/null
  echo "created security group $SG_ID"
else
  echo "security group $SG_ID already exists"
fi

echo "== AMI (latest Ubuntu 24.04 LTS, amd64) =="
AMI_ID=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)
echo "AMI: $AMI_ID"

USER_DATA='#!/bin/bash
apt-get update -y
apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu
git clone https://github.com/calderbuild/quorum.git /home/ubuntu/quorum
chown -R ubuntu:ubuntu /home/ubuntu/quorum
'

echo "== Launching instance =="
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --iam-instance-profile Name="$PROFILE_NAME" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3"}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
  --user-data "$USER_DATA" \
  --query 'Instances[0].InstanceId' --output text)
echo "instance $INSTANCE_ID launching..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

echo "== Elastic IP =="
ALLOC_ID=$(aws ec2 allocate-address --region "$REGION" --domain vpc --query 'AllocationId' --output text)
aws ec2 associate-address --region "$REGION" --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
ELASTIC_IP=$(aws ec2 describe-addresses --region "$REGION" --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)

echo ""
echo "=================================================="
echo "Instance: $INSTANCE_ID"
echo "Elastic IP: $ELASTIC_IP"
echo "DOMAIN=quorum.${ELASTIC_IP}.sslip.io"
echo "API_DOMAIN=api.quorum.${ELASTIC_IP}.sslip.io"
echo "=================================================="
echo ""
echo "Next steps (download /tmp/${KEY_NAME}.pem from CloudShell first if this is a fresh key):"
echo "  ssh -i ${KEY_NAME}.pem ubuntu@${ELASTIC_IP}"
echo "  cd quorum"
echo "  cp .env.template backend/.env   # fill in BEDROCK_API_KEY, AUDIT_S3_BUCKET=${BUCKET}"
echo "  cp .env.template .env           # fill in DOMAIN/API_DOMAIN from above"
echo "  docker compose --profile prod up -d"
