# Two roles, per the ECS Fargate model:
#   - execution role: used BY THE ECS AGENT ITSELF (not application code)
#     to pull the image from ECR, write to CloudWatch, and fetch the
#     `secrets` this task definition references from SSM before the
#     container ever starts.
#   - task role: assumed BY THE APPLICATION CODE at runtime (boto3's
#     default credential chain picks this up automatically via the ECS
#     task metadata endpoint - see llm/bedrock_provider.py's docstring:
#     "never hardcoded, never read from Settings").
#
# Step 11 Part B2: no wildcards on resources anywhere below - every ARN
# is either a specific SSM parameter this config created (ssm.tf), the
# specific ECR repository (ecr.tf), or the specific Bedrock models this
# project's config.py actually names (variables.tf).

data "aws_kms_key" "ssm" {
  key_id = "alias/aws/ssm" # the AWS-managed key SecureString parameters are encrypted under by default
}

# --- Execution role ---

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid     = "ReadTaskSecrets"
    actions = ["ssm:GetParameters", "ssm:GetParameter"]
    # All 4 parameters, not just the 2 the running service's task
    # definition (ecs.tf) actually references - this role is reused
    # as-is for the one-off migration task (docs/DEPLOYMENT.md), which
    # needs DATABASE_MIGRATION_URL/APP_DB_PASSWORD too.
    resources = [
      aws_ssm_parameter.database_url.arn,
      aws_ssm_parameter.database_migration_url.arn,
      aws_ssm_parameter.app_db_password.arn,
      aws_ssm_parameter.groq_api_key.arn,
    ]
  }

  statement {
    sid       = "DecryptSecureStringParameters"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_key.ssm.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "${local.name_prefix}-ecs-execution-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# --- Task role ---

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

locals {
  # Cross-region Bedrock inference profiles (see docs/adr/0007) require
  # bedrock:InvokeModel on BOTH the inference profile ARN itself and the
  # underlying foundation-model ARN it invokes - foundation-model ARNs
  # have no account id segment (they're AWS-owned, shared resources).
  #
  # The inference-profile ARNs stay pinned to var.aws_region (that's
  # where OUR profile resource lives), but the foundation-model region
  # must be a wildcard: Step 12's live deployment hit a real
  # AccessDeniedException because the "eu." cross-region profile actually
  # routed a request to eu-north-1, not eu-west-1, and the foundation-
  # model resource is checked against whichever region the request
  # lands in - not the caller's own region. This is AWS's own documented
  # pattern for cross-region inference IAM policies. It doesn't broaden
  # which models or accounts are reachable (foundation-model ARNs are
  # AWS-owned, not account-scoped) - only which AWS region is allowed to
  # serve the exact same already-named model.
  bedrock_model_arns = [
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_classifier}",
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_synthesis}",
    "arn:aws:bedrock:*::foundation-model/${replace(var.bedrock_model_classifier, "eu.", "")}",
    "arn:aws:bedrock:*::foundation-model/${replace(var.bedrock_model_synthesis, "eu.", "")}",
  ]
}

data "aws_iam_policy_document" "ecs_task" {
  statement {
    sid       = "InvokeConfiguredBedrockModelsOnly"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_model_arns
  }

  statement {
    sid     = "ReadOwnSsmParameters"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.database_url.arn,
      aws_ssm_parameter.database_migration_url.arn,
      aws_ssm_parameter.app_db_password.arn,
      aws_ssm_parameter.groq_api_key.arn,
    ]
  }

  statement {
    sid       = "DecryptSecureStringParameters"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_key.ssm.arn]
  }
}

resource "aws_iam_role_policy" "ecs_task" {
  name   = "${local.name_prefix}-ecs-task"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task.json
}
