variable "aws_region" {
  description = "AWS region - eu-west-1 per CLAUDE.md."
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Tag value distinguishing this deployment from others sharing the same account."
  type        = string
  default     = "production"
}

variable "project_name" {
  description = "Short name used to prefix/tag every resource in this config."
  type        = string
  default     = "dealership-agent"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread subnets across. RDS and the ALB both require at least 2."
  type        = number
  default     = 2
}

# --- ECS task sizing ---
# Step 11, Part B2 asks for the smallest VIABLE task size, not the
# theoretical Fargate floor (256 CPU units / 512 MB) - that floor was
# rejected: this app loads a sentence-transformers model TWICE per
# conversation turn (once in the main API process, once again in the
# per-turn MCP tool-server subprocess - see agents/mcp_session.py), and
# torch's own baseline memory footprint alone is a meaningful fraction of
# 512 MB even before any model weights or FastAPI/uvicorn overhead. 256
# CPU units / 1024 MB is the smallest pairing Fargate offers that leaves
# real headroom instead of guaranteeing an OOM-kill crash loop - tune
# down further only after observing real CloudWatch memory metrics from
# an actual deployment.
variable "task_cpu" {
  description = "Fargate task vCPU units (256 = 0.25 vCPU)."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate task memory in MB."
  type        = number
  default     = 1024
}

variable "container_port" {
  description = "Port the app listens on inside the container (see Dockerfile's EXPOSE/CMD)."
  type        = number
  default     = 8000
}

variable "desired_count" {
  description = "Number of running ECS tasks. Kept at 1 - this is a portfolio deployment, not a high-availability production service; a second task would roughly double ECS/RDS-connection cost for no real benefit at this scale."
  type        = number
  default     = 1
}

# --- RDS sizing ---
variable "rds_instance_class" {
  description = "db.t4g.micro per Step 11, Part B2 - the cheapest Graviton (arm64) burstable class that still gets pgvector support."
  type        = string
  default     = "db.t4g.micro"
}

variable "rds_allocated_storage_gb" {
  description = "RDS storage in GB. 20 GB is the minimum gp3 allocation and comfortably covers this project's synthetic data + real vehicle catalog."
  type        = number
  default     = 20
}

variable "rds_engine_version" {
  description = "PostgreSQL major version - matches pgvector/pgvector:pg16 used in local dev (docker-compose.yml)."
  type        = string
  default     = "16"
}

# --- ECR ---
variable "ecr_image_retention_count" {
  description = "Number of most-recent images the ECR lifecycle policy keeps."
  type        = number
  default     = 5
}

# --- CloudWatch ---
variable "log_retention_days" {
  description = "CloudWatch log group retention, set explicitly rather than left at the (infinite, silently-accumulating-cost) default."
  type        = number
  default     = 7
}

# --- Budgets ---
variable "monthly_budget_usd" {
  description = "AWS Budgets monthly spend threshold that triggers an email alert."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Email address for the AWS Budgets alert - no default on purpose: never commit a real email address into a public portfolio repo. Set via terraform.tfvars (gitignored) or -var at apply time."
  type        = string
}

# --- LLM model routing (Bedrock) ---
# Kept as variables, not hardcoded into the IAM policy, so the exact
# model ids can change without editing iam.tf directly - but the IAM
# policy still only ever grants access to these two specific ARNs, never
# a wildcard (see iam.tf).
variable "bedrock_model_classifier" {
  description = "Bedrock inference profile id for the cheap classifier/routing model - see docs/adr/0007 for why this must be an inference profile id, not a bare model id, and for the live access-control finding about model listing vs. grant."
  type        = string
  default     = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_model_synthesis" {
  description = "Bedrock inference profile id for the stronger synthesis model."
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-6"
}

# --- GitHub OIDC (Part C) ---
variable "github_repository" {
  description = "GitHub \"owner/repo\" this deploy role trusts - scoped to exactly this repository, no other."
  type        = string
  default     = "aghababaeiali/dealership-agent"
}
