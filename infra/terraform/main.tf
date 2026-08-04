# Main infrastructure: ECS Fargate running the dealership-agent API,
# RDS Postgres+pgvector, ALB, ECR, and their supporting resources.
# eu-west-1, per CLAUDE.md.
#
# Remote state: S3 + DynamoDB locking, created by ./bootstrap/ (run once,
# separately - see bootstrap/README.md). Fill in `bucket` and
# `dynamodb_table` below from that module's outputs before the first
# `terraform init` here.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  backend "s3" {
    # Placeholder values - replace with bootstrap's real outputs before
    # `terraform init` (see bootstrap/README.md and
    # docs/DEPLOYMENT.md). Terraform refuses to run `plan`/`apply` at
    # all once a backend block is present but not a real, reachable
    # backend - verified directly while validating this config: even
    # `init -backend=false` isn't enough for `plan`, only for
    # `validate`. To validate/plan this config locally without the real
    # state bucket existing yet, comment this whole block out
    # temporarily, `rm -rf .terraform .terraform.lock.hcl`, and
    # `terraform init` again - then restore it before committing.
    bucket         = "REPLACE-WITH-BOOTSTRAP-STATE-BUCKET-NAME"
    key            = "dealership-agent/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "dealership-agent-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# Needed to construct exact ARNs (account id) for IAM policies below -
# CLAUDE.md/this step: no wildcards on resources, so every ARN this
# config grants access to is built explicitly rather than guessed.
data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}
