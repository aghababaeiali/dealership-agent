# Cost breakdown (eu-west-1)

Estimates only - verify against the [AWS Pricing Calculator](https://calculator.aws)
or the live console before relying on these numbers. All prices are
on-demand, eu-west-1, at time of writing.

## Per-resource, always-on (accrues whether or not anyone uses the API)

| Resource | Estimate | Why |
|---|---|---|
| **ALB** | ~$16-20/month | Fixed hourly charge (~$0.0252/hr × 730h ≈ $18) plus LCU usage charges - negligible at this traffic level. **One of the three expensive resources.** |
| **RDS db.t4g.micro** | ~$13-16/month | Instance-hours (~$0.018/hr × 730h ≈ $13) + 20 GB gp3 storage (~$2.30) + 1-day backup storage (minimal). Single-AZ, no read replica. **One of the three expensive resources.** |
| **ECS Fargate task** (1 vCPU / 2 GB, 1 task, 24/7) | ~$27-32/month | ~$0.033/vCPU-hr × 1 × 730h + ~$0.0037/GB-hr × 2 × 730h. Sized up from an initial 0.25 vCPU / 1 GB after a live deployment showed that was too small (see `docs/DEPLOYMENT.md`'s troubleshooting section) - the `/readyz` health check spawns a fresh MCP subprocess per call, and under-provisioning caused the service to fail its own health checks indefinitely. **One of the three expensive resources.** |
| NAT Gateway | **$0** | Deliberately not provisioned - see vpc.tf. Would otherwise be ~$33/month fixed + ~$0.045/GB processed, for no benefit at this scale. |
| ECR storage | ~$0.10-0.50/month | 5 images × a few hundred MB compressed × $0.10/GB-month. Lifecycle policy caps this from growing unbounded. |
| CloudWatch Logs | <$1/month | 7-day retention, low request volume - both ingestion ($0.57/GB) and storage ($0.03/GB-month) are small at this scale. |
| SSM Parameter Store (Standard tier) | **$0** | Free, any number of parameters - see ssm.tf's reasoning vs. Secrets Manager. |
| S3 (Terraform state) + DynamoDB (lock table) | <$0.05/month | A handful of small state file versions; DynamoDB is pay-per-request with only a few requests per `terraform` run. |
| Data transfer out | ~$0-2/month | First 1 GB/month free, then ~$0.09/GB - depends on actual traffic; negligible for a low-traffic demo. |
| IAM roles/policies, OIDC provider, AWS Budgets (first 2 budgets) | **$0** | All free. |

**Baseline always-on total: roughly $58-70/month** if left running continuously; the ALB, RDS, and Fargate task together account for essentially all of it.

## Usage-based, NOT always-on

| Resource | Estimate | Why |
|---|---|---|
| Bedrock (Claude Haiku/Sonnet inference) | ~$0.02-0.03 per conversation | Measured live in Step 9/10 testing: one real multi-turn conversation cost ~$0.025 (a few Haiku calls for routing/verification + one Sonnet-tier synthesis call). Scales with actual usage, not idle time - $0 if nothing is talking to the API. |

## The $10 AWS Budgets alarm is a tripwire, not a promise

Be clear-eyed about this: the budget alarm this config creates (`budgets.tf`)
fires at $8 (80%) and $10 (100%) of **monthly** spend - but the baseline
always-on cost above is roughly **6-7x that**. Left running for a full
month, this deployment will cost approximately $58-70, not $10.

The $10 threshold is intentionally an early tripwire, not a cap AWS
enforces - **AWS Budgets can only notify, never stop resources from
running**. Expect the alert email within the first 5-7 days of leaving
this deployed, not at the end of the month. That is by design: it means
"you deployed this and it's costing real money" reaches you fast, not
"you're near your intended monthly limit." See `docs/DEPLOYMENT.md`'s
cost checklist for what to actually do when it fires.

## What `terraform destroy` reclaims - and what it doesn't

Running `terraform destroy` against the main config removes essentially
everything above and stops the always-on cost immediately: VPC, ALB, ECS
service/cluster/task definition, RDS instance, security groups, IAM
roles, the OIDC provider, SSM parameters, and the CloudWatch log group
(log groups are NOT retained after their resource is destroyed, despite
the 7-day retention setting - that setting only governs when CloudWatch
itself expires old log events while the group exists).

**Two things do NOT go away, on purpose or by AWS default behavior:**

- **ECR images.** `aws_ecr_repository` refuses to delete a non-empty
  repository (no `force_delete` was set - a deliberate safety default,
  not an oversight). `terraform destroy` will fail on this resource
  until the repository is emptied manually first - see
  `docs/DEPLOYMENT.md`'s teardown section for the exact command. Storage
  cost while images remain: a few cents/month (see the ECR row above) -
  small, but not zero, and it does not stop on its own.
- **The S3 state bucket + DynamoDB lock table.** These belong to the
  **separate** `bootstrap/` module/state, not the main config -
  `terraform destroy` here never touches them. They must be destroyed
  as their own explicit step, after confirming no other Terraform config
  still needs that state. Cost while they remain: effectively $0 (well
  under a cent/month at this scale), but not literally zero.
