# Deployment runbook

Read [`infra/terraform/COST.md`](../infra/terraform/COST.md) first - this
deployment costs real money the moment it's applied (roughly $58-70/month
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
- **Cross-region inference profiles can route outside your region.** A
  live deployment hit a real `AccessDeniedException` because the
  `eu.`-prefixed classifier profile routed an actual request to
  `eu-north-1`, not the configured `eu-west-1` - IAM checks the
  foundation-model resource against whichever region the request lands
  in, not the caller's region. `infra/terraform/iam.tf`'s
  `bedrock_model_arns` wildcards the region on the foundation-model ARNs
  specifically (not the inference-profile ARNs) for this reason; if you
  change the model IDs, keep that wildcard.

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
healthy yet** - several things still need doing, **in the order below**.
A live deployment found this order is not optional: `/readyz` checks the
database using the app's own least-privilege role
(`APP_DB_USER`/`dealership_app`), and that role does not exist until the
RLS migration creates it. If you force a new deployment and run
`aws ecs wait services-stable` before migrations have run, the wait will
never succeed. Migrations must run first.

### 4. Push a real image and set the real secret

The task definition points at `<ecr-repo>:initial`, which doesn't exist
yet:

```bash
cd infra/terraform
ECR_REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin "${ECR_REPO%/*}"
```

**Build for ARM64, not your local default.** The task definition sets
`runtime_platform { cpu_architecture = "ARM64" }` (Graviton is cheaper
per vCPU/GB-hour than x86_64 Fargate). A plain `docker build` on an
Apple Silicon machine already produces an arm64 image; on an x86_64
machine, add `--platform linux/arm64`. Pushing an image whose manifest
doesn't match `ARM64` fails the task with `CannotPullContainerError`,
not a build error, so it only surfaces once ECS tries to run it.

```bash
docker build --platform linux/arm64 -t "$ECR_REPO:initial" ..
docker push "$ECR_REPO:initial"

# Real secrets - the ones Terraform left as placeholders
aws ssm put-parameter --name "/dealership-agent-production/GROQ_API_KEY" \
  --type SecureString --value "<your real Groq key>" --overwrite
```

Force the ECS service to pick up the newly-pushed `:initial` image, but
do **not** wait for stability yet - that comes after migrations, below:

```bash
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --force-new-deployment
```

### 5. Run migrations

RDS is in a private subnet with no public access (by design), so
migrations must run from *inside* the VPC - a one-off Fargate task in
the same public subnets, overriding the container's command. The
shipped image does not include `alembic.ini` (only `alembic`'s Python
package and the migration scripts are needed at runtime, so the ini file
is deliberately not copied into the runtime image), so invoke Alembic
programmatically rather than via its CLI. The RLS migration also needs
`APP_DB_USER`/`APP_DB_PASSWORD` (to create the least-privilege app role),
not just `DATABASE_MIGRATION_URL` (to connect as the owner role) -
running with only the latter fails partway through with `APP_DB_USER /
APP_DB_PASSWORD must be set to create the app role`:

```bash
MIGRATION_URL=$(aws ssm get-parameter --name /dealership-agent-production/DATABASE_MIGRATION_URL --with-decryption --query Parameter.Value --output text)
APP_DB_PASSWORD=$(aws ssm get-parameter --name /dealership-agent-production/APP_DB_PASSWORD --with-decryption --query Parameter.Value --output text)

aws ecs run-task \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --task-definition "$(terraform output -raw ecs_task_definition_family)" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json public_subnet_ids | jq -r 'join(",")')],securityGroups=[$(terraform output -raw ecs_task_security_group_id)],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"app","command":["python3","-c","from alembic.config import Config; from alembic import command; cfg = Config(); cfg.set_main_option(\"script_location\", \"src/dealership_agent/db/migrations\"); command.upgrade(cfg, \"head\")"],"environment":[{"name":"DATABASE_MIGRATION_URL","value":"'"$MIGRATION_URL"'"},{"name":"APP_DB_USER","value":"dealership_app"},{"name":"APP_DB_PASSWORD","value":"'"$APP_DB_PASSWORD"'"}]}]}'
```

Watch it complete in CloudWatch Logs
(`/ecs/dealership-agent-production`) - check the task's exit code, since
the migration's own log output can be sparse - then load the vehicle
catalog and policy corpus the same way local dev does
(`data/scripts/embed_policies.py`, and either the real Kaggle pipeline
or `scripts/seed_ci_vehicles.py` for a quick synthetic catalog) - run
these as further one-off tasks with the appropriate command override,
the same pattern as above. If loading a large sample, size the one-off
task's CPU deliberately (`run-task`'s `--overrides` accepts task-level
`cpu`/`memory`, not just the container-level command/environment) -
embedding a few thousand rows on the task definition's default sizing
can take significantly longer than expected.

### 6. Now wait for the service to stabilize

Only after migrations have succeeded:

```bash
aws ecs wait services-stable \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --services "$(terraform output -raw ecs_service_name)"
```

If this still doesn't stabilize, see
[Troubleshooting](#troubleshooting-service-never-becomes-healthy) below
before assuming it's an ordering problem again.

### 7. Verify

```bash
ALB_DNS=$(terraform output -raw alb_dns_name)
curl "http://$ALB_DNS/healthz"
curl "http://$ALB_DNS/readyz"
curl "http://$ALB_DNS/listings?query=reliable+family+SUV&limit=3"
```

`/chat` needs a valid JWT, minted against the same keypair this
deployment trusts - see below for how that keypair gets into the
container.

**JWT public key provisioning.** This project still has no real
production identity provider - only the dev-only
`scripts/mint_dev_token.py` - so a live deployment necessarily reuses
that dev-grade keypair rather than a real IdP's. What the deployment
does solve is getting the *public* half of that keypair into the running
container: `docker-entrypoint.sh` writes the `JWT_PUBLIC_KEY_PEM` secret
(an SSM `SecureString`, injected as a plain environment variable and
written to `/tmp/jwt_public.pem` at container start, since the
non-root `appuser` doesn't own `/app` itself) out to the path
`JWT_PUBLIC_KEY_PATH` expects, before `uvicorn` starts. Mint a token
locally against the matching private key and pass it as a bearer token
to `/chat`:

```bash
uv run python scripts/mint_dev_token.py --customer-id 1 --skip-db-check
```

`--skip-db-check` is required against a live deployment: RDS has no
public access, so the script's default customer-existence check (which
touches the database directly) can't run from a laptop. Use it once the
customer already exists (seeded via a one-off task, same pattern as
migrations above).

## Troubleshooting: service never becomes healthy

If `aws ecs wait services-stable` times out (or the target group never
reports a target as healthy) after migrations have genuinely succeeded,
check these, in order of how often they were the actual cause during a
real deployment:

1. **Task CPU/memory undersized.** `/readyz` spawns a fresh MCP
   subprocess on every single health check call (importing
   torch/sqlalchemy/mcp fresh each time, no reuse). Under-provisioning
   showed up two ways: first as the ALB health check timing out because
   the subprocess spawn plus handshake took longer than the check's
   timeout, and separately as the task being SIGKILLed (exit code 137,
   an out-of-memory kill) 6-8 minutes in, because the main process and
   each spawned subprocess each load their own copy of the embedding
   model concurrently. `var.task_cpu`/`var.task_memory` in
   `infra/terraform/variables.tf` carry the full account of both
   failures and the sizing that resolved them - check `aws ecs
   describe-tasks`' `stoppedReason` and `exitCode` for the specific
   symptom before changing anything.
2. **ALB health check timeout too tight for a cold subprocess spawn.**
   Even correctly sized, a fresh interpreter plus a full MCP stdio
   handshake is not instant. `infra/terraform/alb.tf`'s
   `health_check` block widens the default timeout/interval for this
   reason - if you've changed the container's startup work, re-verify
   the timeout still has margin.
3. **The service is still on an old task-definition revision.** Both
   `aws_ecs_task_definition.app` and `aws_ecs_service.app` set
   `lifecycle { ignore_changes = [...] }` so the GitHub Actions deploy
   workflow can manage the running image tag without Terraform fighting
   it (see Part C below). One consequence: after a Terraform-driven
   task-definition change (a CPU/memory bump, for example),
   `terraform apply` alone does **not** move the running service onto
   the new revision - you must force it explicitly:
   ```bash
   aws ecs update-service --cluster "$(terraform output -raw ecs_cluster_name)" \
     --service "$(terraform output -raw ecs_service_name)" \
     --task-definition "$(terraform output -raw ecs_task_definition_family)" \
     --force-new-deployment
   ```
   Passing the task-definition family name (not a specific revision
   number) resolves to the latest revision.
4. **A synchronous database call blocking the single-worker event
   loop.** Already fixed in `api/app.py` (`/readyz`'s database check
   runs via `asyncio.to_thread`), but worth knowing about if this code
   changes again: a blocking call inside an `async def` handler can
   stall the entire process for any concurrently-arriving request,
   including the ALB's own next health check, which arrives from
   multiple availability zones near-simultaneously - not just from one
   local sequential request at a time the way local testing exercises
   it.

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
      case" - the always-on cost (~$58-70/month, see COST.md) accrues
      identically whether the API is being used or sitting idle.
- [ ] If you plan to leave it running: check the ECR repository image
      count hasn't crept past the lifecycle policy's expectations, and
      that CloudWatch Logs' 7-day retention is still in effect (a future
      Terraform change could accidentally widen it).
- [ ] Check the AWS Billing console's Cost Explorer directly at least
      once - the Budgets alarm is a tripwire for when spend crosses a
      threshold, not a live dashboard; it won't tell you *why* a cost
      changed, only that it did.
