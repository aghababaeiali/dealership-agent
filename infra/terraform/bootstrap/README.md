# Bootstrap (run once)

Creates the S3 bucket and DynamoDB table the main config (`../`) uses as
its remote state backend. This module's own state stays **local**
(`terraform.tfstate` in this directory) - it cannot use the backend it's
creating.

Run this exactly once per AWS account/region, before ever running
`terraform init` in `../`:

```bash
cd infra/terraform/bootstrap
terraform init
terraform apply
```

Note the two outputs (`state_bucket_name`, `lock_table_name`) and paste
them into `../main.tf`'s `backend "s3"` block (`bucket` and
`dynamodb_table` fields), then continue with the main config's own
`terraform init` (see `../../../docs/DEPLOYMENT.md`).

Keep this directory's `terraform.tfstate` file safe (e.g. a private
location outside version control, or your own separate small remote
backend) - losing it means Terraform loses track of the state bucket and
lock table it created, though the AWS resources themselves would still
exist and could be re-imported.

**Never re-run `terraform destroy` here casually** - it deletes the state
backend that every other Terraform config in this project depends on.
See the main runbook's teardown section for the correct order.
