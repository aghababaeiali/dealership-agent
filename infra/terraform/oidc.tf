# Step 11 Part C1: GitHub Actions authenticates to AWS via OIDC - no
# long-lived AWS access keys stored as a GitHub secret anywhere, per
# CLAUDE.md ("CI: GitHub Actions with OIDC, no long-lived AWS keys").
# The trust policy below restricts which GitHub identity can assume this
# role to exactly this one repository; nothing else in GitHub, including
# other repos in the same account/org, can assume it.

# Fetched live rather than hardcoded - GitHub has rotated this
# certificate before, and a stale hardcoded thumbprint is a silent way
# for this provider to start rejecting real GitHub tokens. AWS's own
# current guidance is to source this dynamically for exactly that
# reason; the last certificate in the chain is the root/intermediate CA
# thumbprint AWS expects here.
data "tls_certificate" "github_actions" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github_actions.certificates[length(data.tls_certificate.github_actions.certificates) - 1].sha1_fingerprint]
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      # Scoped to this repository only - any branch/ref within it. The
      # deploy workflow itself is workflow_dispatch-only (manual), which
      # is the deliberate human gate; this condition's job is only to
      # make sure no OTHER GitHub repository can ever assume this role.
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${local.name_prefix}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
}

data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid     = "EcrAuth"
    actions = ["ecr:GetAuthorizationToken"]
    # AWS's own IAM reference for ECR: GetAuthorizationToken does not
    # support resource-level permissions at all - "*" here is the one
    # AWS-documented exception to this config's no-wildcards rule, not an
    # oversight.
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid = "EcsDeploy"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
      "ecs:DescribeTaskDefinition",
    ]
    resources = [
      "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}",
    ]
  }

  statement {
    sid     = "EcsRegisterTaskDefinition"
    actions = ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition"]
    # RegisterTaskDefinition creates a new revision each deploy, so the
    # exact revision number can't be known ahead of time - scoped to
    # every revision of this one task family, not to task definitions in
    # general.
    resources = ["arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name_prefix}:*"]
  }

  statement {
    sid       = "PassEcsRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${local.name_prefix}-github-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
