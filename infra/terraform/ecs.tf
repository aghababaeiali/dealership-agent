resource "aws_ecs_cluster" "main" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "disabled" # Container Insights bills per-metric - not worth it for a single-task portfolio deployment
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name_prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc" # required for Fargate
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name = "app"
      # Placeholder tag - the first real image must be built and pushed
      # before this service can start (see docs/DEPLOYMENT.md's apply
      # steps). Routine deploys after that go through the GitHub Actions
      # workflow (deploy.yml), which registers new task definition
      # revisions directly via the AWS CLI/SDK rather than through
      # Terraform - see the `ignore_changes` lifecycle block below for
      # why Terraform must not fight that.
      image     = "${aws_ecr_repository.app.repository_url}:initial"
      essential = true

      portMappings = [
        { containerPort = var.container_port, protocol = "tcp" }
      ]

      environment = [
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = "INFO" },
        { name = "LLM_PROVIDER", value = "bedrock" }, # Bedrock is prod, Groq is local dev - CLAUDE.md
        { name = "AWS_REGION", value = var.aws_region },
        { name = "BEDROCK_MODEL_CLASSIFIER", value = var.bedrock_model_classifier },
        { name = "BEDROCK_MODEL_SYNTHESIS", value = var.bedrock_model_synthesis },
        { name = "EMBEDDING_MODEL_NAME", value = "sentence-transformers/all-MiniLM-L6-v2" },
        { name = "JWT_ALGORITHM", value = "RS256" },
        { name = "JWT_ISSUER", value = "dealership-agent" },
        { name = "JWT_AUDIENCE", value = "dealership-agent-api" },
        { name = "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", value = "30" },
        # Real production JWT key provisioning is a documented follow-up
        # (docs/DEPLOYMENT.md), same as the ALB's TLS follow-up - there is
        # no real external identity provider in this project yet, only
        # the dev-only scripts/mint_dev_token.py. Until a real keypair is
        # provisioned at this path inside the container, authenticated
        # endpoints will fail closed (never fail open) with a 401.
        { name = "JWT_PUBLIC_KEY_PATH", value = "/app/prod_keys/jwt_public.pem" },
        { name = "RATE_LIMIT_PER_CUSTOMER_PER_MINUTE", value = "20" },
        { name = "RATE_LIMIT_GLOBAL_PER_MINUTE", value = "200" },
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.database_url.arn },
        { name = "GROQ_API_KEY", valueFrom = aws_ssm_parameter.groq_api_key.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "app"
        }
      }
    }
  ])

  lifecycle {
    # The GitHub Actions deploy workflow (C2) registers new task
    # definition revisions directly (new image tag per deploy) and
    # updates the service to point at them - Terraform manages the
    # container's structure/config, not its rolling image tag, so it
    # must not revert a CI-driven deploy back to ":initial" on the next
    # `terraform apply`.
    ignore_changes = [container_definitions]
  }

  tags = { Name = "${local.name_prefix}-task" }
}

resource "aws_ecs_service" "app" {
  name            = local.name_prefix
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = true # public subnet, no NAT Gateway - see vpc.tf
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  # Same reasoning as the task definition's ignore_changes - the deploy
  # workflow calls `aws ecs update-service` directly with a new task
  # definition revision.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.http]
}
