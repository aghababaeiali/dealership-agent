# HTTP only for now, Step 11 Part B2 - TLS is a documented follow-up
# (docs/DEPLOYMENT.md): it needs a real domain name and an ACM
# certificate, neither of which exists yet for this portfolio project.
# Swapping in HTTPS later is a listener change, not a redesign - see that
# doc for the exact steps.

resource "aws_lb" "app" {
  name               = local.name_prefix
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # A portfolio project's ALB doesn't need deletion protection - the
  # runbook's teardown section is the deliberate, intended way this gets
  # removed, not something to guard against.
  enable_deletion_protection = false

  tags = { Name = "${local.name_prefix}-alb" }
}

resource "aws_lb_target_group" "app" {
  name        = local.name_prefix
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # required for awsvpc network mode (Fargate)

  health_check {
    path                = "/readyz" # checks DB + MCP reachability, not just process liveness - see api/app.py
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    # /readyz spawns a fresh MCP tool-server subprocess per call (real
    # process spawn + torch/sqlalchemy/mcp imports, not free) - Step 12
    # live deployment showed even 1 full vCPU wasn't enough to complete
    # that whole round trip (fresh interpreter + torch/sqlalchemy/mcp
    # imports + DB connect + stdio handshake) within a 10s timeout, EVERY
    # single time, with zero successful checks ever observed - this is a
    # timeout-budget problem, not a resource-starvation one. 30s/60s
    # gives real margin for a consistently-heavy cold subprocess spawn.
    timeout  = 30
    interval = 60
    matcher  = "200"
  }

  tags = { Name = "${local.name_prefix}-tg" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
