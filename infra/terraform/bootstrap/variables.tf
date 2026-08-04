variable "aws_region" {
  description = "AWS region for the state bucket/lock table. Should match the main config's region."
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Tag value distinguishing this deployment from others sharing the same account."
  type        = string
  default     = "production"
}
