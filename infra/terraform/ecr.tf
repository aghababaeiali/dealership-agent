resource "aws_ecr_repository" "app" {
  name                 = local.name_prefix
  image_tag_mutability = "IMMUTABLE" # a given tag (e.g. a git SHA) can never be silently overwritten

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the ${var.ecr_image_retention_count} most recent images - each stored image costs storage indefinitely otherwise."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_image_retention_count
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
