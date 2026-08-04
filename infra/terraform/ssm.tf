# SSM Parameter Store, not Secrets Manager, Step 11 Part B2: Parameter
# Store's Standard tier (used exclusively here - no Advanced-tier
# features needed) is free for any number of parameters, versus Secrets
# Manager's ~$0.40/secret/month plus API call charges. For a handful of
# small, low-rotation-frequency values in a cost-conscious portfolio
# deployment, that ongoing per-secret charge buys nothing this project
# needs (no built-in rotation Lambda is configured either way).
#
# GROQ_API_KEY ships as an empty placeholder - a real key is never
# committed to this repo or generated here; the operator sets the real
# value after apply (see docs/DEPLOYMENT.md). JWT production key
# provisioning is a documented follow-up (docs/DEPLOYMENT.md), same as
# the ALB's TLS follow-up - see that doc rather than iam.tf/ssm.tf for
# the reasoning.

resource "aws_ssm_parameter" "database_url" {
  name  = "/${local.name_prefix}/DATABASE_URL"
  type  = "SecureString"
  value = "postgresql+psycopg://dealership_app:${random_password.app_db.result}@${aws_db_instance.main.address}:5432/dealership"

  tags = { Name = "${local.name_prefix}-database-url" }
}

resource "aws_ssm_parameter" "database_migration_url" {
  name  = "/${local.name_prefix}/DATABASE_MIGRATION_URL"
  type  = "SecureString"
  value = "postgresql+psycopg://${aws_db_instance.main.username}:${random_password.rds_master.result}@${aws_db_instance.main.address}:5432/dealership"

  tags = { Name = "${local.name_prefix}-database-migration-url" }
}

resource "aws_ssm_parameter" "app_db_password" {
  name  = "/${local.name_prefix}/APP_DB_PASSWORD"
  type  = "SecureString"
  value = random_password.app_db.result

  tags = { Name = "${local.name_prefix}-app-db-password" }
}

resource "aws_ssm_parameter" "groq_api_key" {
  name  = "/${local.name_prefix}/GROQ_API_KEY"
  type  = "SecureString"
  value = "REPLACE-ME-AFTER-APPLY"

  lifecycle {
    # Terraform must not fight the operator over this value once they've
    # set the real key by hand (aws ssm put-parameter) - see
    # docs/DEPLOYMENT.md's apply/verify steps.
    ignore_changes = [value]
  }

  tags = { Name = "${local.name_prefix}-groq-api-key" }
}
