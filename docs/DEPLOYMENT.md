# Deployment runbook

Read [`infra/terraform/COST.md`](../infra/terraform/COST.md) first - this
deployment costs real money the moment it's applied (roughly $45-50/month
if left running, see that doc for why), not just when someone uses it.

## Prerequisites

- An AWS account, with credentials configured locally (e.g. `aws
  configure` or an SSO profile) for a principal allowed to create the
  resources in `infra/terraform/` (VPC, ECS, RDS, IAM, etc.).
- [Terraform](https://developer.hashicorp.com/terraform) >= 1.5.
- [Docker](https://www.docker.com/) (to build/push the image manually
  for the first deploy - later deploys go through the GitHub Actions
  workflow instead).
- [AWS CLI](https://aws.amazon.com/cli/) v2.
- A GitHub repository this code has already been pushed to (Part C below
  needs it), with `gh` CLI access or the ability to set repository
  variables in the GitHub UI.
- **Bedrock model access confirmed, not just listed.** Before relying on
  `var.bedrock_model_classifier`/`bedrock_model_synthesis`'s defaults,
  confirm your AWS account actually has invoke access to them (`aws
  bedrock list-inference-profiles --region eu-west-1`, then a real
  `Converse` call) - see [ADR 0007](adr/0007-bedrock-provider-and-model-routing.md)
  for a live, disclosed case where a model was listed but not actually
  granted.

## Part B: bootstrap, apply, verify

### 1. Bootstrap (run once)

```bash
cd infra/terraform/bootstrap
terraform init
terraform apply
```

Note the two outputs: `state_bucket_name`, `lock_table_name`.

### 2. Configure the main config's backend

Edit `infra/terraform/main.tf`'s `backend "s3"` block: replace
`REPLACE-WITH-BOOTSTRAP-STATE-BUCKET-NAME` with the real
`state_bucket_name` output (the `dynamodb_table` value already matches
the bootstrap module's fixed table name, but double-check it if you
changed anything).

### 3. Apply the main config

```bash
cd infra/terraform
terraform init
terraform plan -var="budget_alert_email=you@example.com" -out=tfplan
terraform apply tfplan
```

`budget_alert_email` has no default on purpose (see `variables.tf`) -
never commit a real email address into this public repo. Pass it via
`-var`, or create a gitignored `terraform.tfvars`
(`budget_alert_email = "you@example.com"`) so you don't have to retype
it every time.

This creates the VPC, RDS instance, ECR repository, ECS cluster/service,
ALB, IAM roles, SSM parameters (with placeholder values), the GitHub
OIDC provider, and the budget alarm. **The ECS service will NOT come up
healthy yet** - two things still need doing:

### 4. Push a real image and set the real secret

The task definition points at `<ecr-repo>:initial`, which doesn't exist
yet:

```bash
cd infra/terraform
ECR_REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin "${ECR_REPO%/*}"
docker build -t "$ECR_REPO:initial" ..
docker push "$ECR_REPO:initial"

# Real secrets - the ones Terraform left as placeholders
aws ssm put-parameter --name "/dealership-agent-production/GROQ_API_KEY" \
  --type SecureString --value "<your real Groq key>" --overwrite
```

Force the ECS service to pick up the newly-pushed `:initial` image:

```bash
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --force-new-deployment
aws ecs wait services-stable \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --services "$(terraform output -raw ecs_service_name)"
```

### 5. Run migrations

RDS is in a private subnet with no public access (by design), so
migrations must run from *inside* the VPC - a one-off Fargate task in
the same public subnets, overriding the container's command:

```bash
aws ecs run-task \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --task-definition "$(terraform output -raw ecs_task_definition_family)" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json public_subnet_ids | jq -r 'join(",")')],securityGroups=[$(terraform output -raw ecs_task_security_group_id)],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"app","command":["alembic","upgrade","head"],"environment":[{"name":"DATABASE_MIGRATION_URL","value":"'"$(aws ssm get-parameter --name /dealership-agent-production/DATABASE_MIGRATION_URL --with-decryption --query Parameter.Value --output text)"'"}]}]}'
```

Watch it complete in CloudWatch Logs
(`/ecs/dealership-agent-production`), then load the vehicle catalog and
policy corpus the same way local dev does (`data/scripts/embed_policies.py`,
and either the real Kaggle pipeline or `scripts/seed_ci_vehicles.py` for
a quick synthetic catalog) - run these as further one-off tasks with the
appropriate command override, the same pattern as above.

### 6. Verify

```bash
ALB_DNS=$(terraform output -raw alb_dns_name)
curl "http://$ALB_DNS/healthz"
curl "http://$ALB_DNS/readyz"
curl "http://$ALB_DNS/listings?query=reliable+family+SUV&limit=3"
```

`/chat` needs a valid JWT - see the known limitation below before
expecting this to work out of the box.

**Known limitation, disclosed rather than solved here (same pattern as
the ALB's TLS follow-up):** this project has no real production identity
provider - only the dev-only `scripts/mint_dev_token.py`. The task
definition points `JWT_PUBLIC_KEY_PATH` at `/app/prod_keys/jwt_public.pem`,
which does not exist in the shipped image. Until a real keypair is
provisioned there (e.g. baked into a custom image build, or written at
container start from an SSM-sourced environment variable via a small
entrypoint script), `/chat` will correctly fail closed with a 401 rather
than silently accepting unverifiable tokens - this is the Core Security
Invariant's fail-closed design working as intended, not a bug, but it
does mean `/chat` needs this follow-up before it's usable in this
deployed environment.

## Part C: enabling the GitHub Actions deploy workflow

`.github/workflows/deploy.yml` is `workflow_dispatch`-only and has
**not** been run. To enable it:

1. Get the deploy role's ARN: `terraform output -raw deploy_role_arn`
   (from `infra/terraform`).
2. In the GitHub repository's Settings -> Secrets and variables ->
   Actions -> Variables, add:
   - `AWS_DEPLOY_ROLE_ARN` = the ARN from step 1
   - `AWS_REGION` = `eu-west-1`
   - `ECR_REPOSITORY_NAME` = `terraform output -raw ecr_repository_url`'s
     final path segment (the repository name, not the full URL)
   - `ECS_CLUSTER_NAME` = `terraform output -raw ecs_cluster_name`
   - `ECS_SERVICE_NAME` = `terraform output -raw ecs_service_name`
   - `ECS_TASK_FAMILY` = `terraform output -raw ecs_task_definition_family`
3. That's it - no AWS access keys to create or rotate. The workflow
   authenticates via OIDC using the role from step 1, whose trust policy
   (`infra/terraform/oidc.tf`) already restricts it to this repository.

To actually run a deploy: Actions tab -> "Deploy" workflow -> "Run
workflow" -> type `deploy` in the confirmation field. It builds the
image, pushes to ECR, registers a new task definition revision, updates
the service, and waits for ECS to report the deployment stable before
finishing.

## Teardown - destroy everything

Order matters - the main config first, then bootstrap (which the main
config's remote state depends on):

```bash
# 1. Empty the ECR repository first - aws_ecr_repository refuses to
#    delete a non-empty repo (no force_delete was set - deliberate, see
#    infra/terraform/COST.md).
aws ecr batch-delete-image \
  --repository-name "$(terraform -chdir=infra/terraform output -raw ecr_repository_url | sed 's#.*/##')" \
  --image-ids "$(aws ecr list-images --repository-name "$(terraform -chdir=infra/terraform output -raw ecr_repository_url | sed 's#.*/##')" --query 'imageIds[*]' --output json)"

# 2. Destroy the main config
cd infra/terraform
terraform destroy -var="budget_alert_email=you@example.com"

# 3. Only after confirming NOTHING else needs this state - destroy the
#    bootstrap module too (the S3 bucket + DynamoDB lock table).
cd bootstrap
terraform destroy
```

**What persists if you stop at step 2 (or skip step 3):**

- The S3 state bucket and DynamoDB lock table (bootstrap's own
  resources, never touched by the main config's destroy) - effectively
  $0/month at this scale, but not literally zero, and they will linger
  indefinitely if step 3 is never run.
- If step 1 is skipped and `terraform destroy` fails on the non-empty
  ECR repository, every other resource in the main config that doesn't
  depend on it will still be destroyed - clean up the images and re-run
  `terraform destroy` to finish.

CloudWatch Logs are **not** in this list - the log group is a normal
Terraform-managed resource and is deleted along with everything else in
step 2, regardless of its 7-day retention setting (retention only
governs when CloudWatch expires old *events* while the group exists).

## Cost checklist - run this before walking away from a deployed environment

- [ ] Is the ECS service's `desired_count` still 1 (not scaled up for a
      demo and forgotten)? `aws ecs describe-services --cluster <cluster> --services <service> --query 'services[0].desiredCount'`
- [ ] Does the AWS Budgets alarm have a real, monitored email address,
      not the placeholder used during `terraform plan` testing?
- [ ] Is anything actually still using this deployment? If not, run the
      teardown steps above rather than leaving it running "just in
      case" - the always-on cost (~$45-50/month, see COST.md) accrues
      identically whether the API is being used or sitting idle.
- [ ] If you plan to leave it running: check the ECR repository image
      count hasn't crept past the lifecycle policy's expectations, and
      that CloudWatch Logs' 7-day retention is still in effect (a future
      Terraform change could accidentally widen it).
- [ ] Check the AWS Billing console's Cost Explorer directly at least
      once - the Budgets alarm is a tripwire for when spend crosses a
      threshold, not a live dashboard; it won't tell you *why* a cost
      changed, only that it did.
