output "alb_dns_name" {
  description = "Public URL (HTTP only - see alb.tf) for the deployed API."
  value       = aws_lb.app.dns_name
}

output "ecr_repository_url" {
  description = "Push images here - referenced by the deploy workflow (.github/workflows/deploy.yml)."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.app.name
}

output "ecs_task_definition_family" {
  value = aws_ecs_task_definition.app.family
}

output "rds_endpoint" {
  description = "RDS host:port - only reachable from inside the VPC (private subnet, no public access)."
  value       = aws_db_instance.main.endpoint
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "ecs_task_security_group_id" {
  description = "Needed for a one-off migration task run in the same subnets (docs/DEPLOYMENT.md)."
  value       = aws_security_group.ecs_task.id
}

output "deploy_role_arn" {
  description = "Paste into the AWS_DEPLOY_ROLE_ARN GitHub Actions repository variable - see docs/DEPLOYMENT.md Part C."
  value       = aws_iam_role.github_actions_deploy.arn
}
