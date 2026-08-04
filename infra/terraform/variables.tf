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
# 512 MB even before any model weights or FastAPI/uvicorn overhead.
#
# Step 12's live deployment showed 256 CPU units is not enough either:
# /readyz's MCP subprocess spawn (a fresh `python -m
# dealership_agent.tools.server`, importing torch/sqlalchemy/mcp) plus
# the main uvicorn process contending for a single 0.25 vCPU routinely
# took longer than the ALB health check's 5s timeout, so the service
# could never stabilize. 512 CPU units (0.5 vCPU) still wasn't enough:
# each /readyz call spawns a brand-new subprocess with no reuse, and
# under 0.5 vCPU, overlapping health-check-triggered spawns (ALB checks
# from multiple AZs, plus the interval being shorter than one spawn's
# import+handshake time) piled up and starved each other of CPU, so none
# of them ever finished the stdio handshake at all. 1024 CPU units (1
# full vCPU) gives each spawn enough isolated time to actually complete -
# confirmed against a real deployment, not a guess.
variable "task_cpu" {
  description = "Fargate task vCPU units (1024 = 1 vCPU)."
  type        = number
  default     = 1024
}

# 1024 MB turned out to be a real OOM, not just a tight fit: every real
# deployment task was SIGKILLed (exit 137) 6-8 minutes in. Root cause -
# the main uvicorn process AND each per-readyz-call / per-conversation-
# turn MCP subprocess each load their own full copy of the
# sentence-transformers model into memory (see agents/mcp_session.py) -
# with the health check spawning a fresh subprocess every 20s, that's a
# lot of concurrent torch runtimes stacking up against a 1GB ceiling.
# 2048 MB confirmed stable against a real deployment.
variable "task_memory" {
  description = "Fargate task memory in MB."
  type        = number
  default     = 2048
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
