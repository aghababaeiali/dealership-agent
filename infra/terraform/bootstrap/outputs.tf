output "state_bucket_name" {
  description = "S3 bucket name - paste into ../main.tf's backend \"s3\" block."
  value       = aws_s3_bucket.terraform_state.id
}

output "lock_table_name" {
  description = "DynamoDB table name - paste into ../main.tf's backend \"s3\" block."
  value       = aws_dynamodb_table.terraform_lock.name
}
