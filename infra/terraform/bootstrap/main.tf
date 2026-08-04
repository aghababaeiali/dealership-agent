# RUN-ONCE bootstrap: creates the S3 bucket + DynamoDB table that the
# main Terraform config (../) uses as its remote state backend.
#
# This module deliberately has NO remote backend of its own - it creates
# the remote backend, so it can't depend on one existing yet (the classic
# chicken-and-egg problem). Its own state is local (terraform.tfstate in
# this directory) and is expected to be applied exactly once, by hand,
# before the main config is ever initialized. See README.md in this
# directory for the exact one-time sequence.
#
# Cost: an S3 bucket with a handful of small state files and a
# pay-per-request DynamoDB table both cost cents/month at this scale -
# see infra/terraform/COST.md.

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
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "dealership-agent"
      environment = var.environment
      managed_by  = "terraform"
      component   = "bootstrap"
    }
  }
}

# Random suffix so the bucket name is globally unique without the
# operator having to hand-pick one - S3 bucket names are a global
# namespace across all AWS accounts.
resource "random_id" "state_bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "terraform_state" {
  bucket = "dealership-agent-tfstate-${random_id.state_bucket_suffix.hex}"

  # Deliberately no lifecycle { prevent_destroy = true } here - this is a
  # portfolio project, not a real company's production state; the
  # runbook (docs/DEPLOYMENT.md) already warns this bucket must be
  # emptied and destroyed manually as its own explicit step, since
  # Terraform can't destroy a non-empty bucket.
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    # Versioning, not for compliance - so an accidental `terraform apply`
    # that corrupts state has a prior good version to roll back to.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "terraform_lock" {
  name         = "dealership-agent-tfstate-lock"
  billing_mode = "PAY_PER_REQUEST" # no capacity to size/pay for when idle
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}
