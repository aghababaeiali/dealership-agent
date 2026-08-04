# RDS PostgreSQL + pgvector, Step 11 Part B2. Private subnets, no public
# access - the only path to this database is from the ECS task's
# security group (see vpc.tf). Two separate random credentials are
# generated here and pushed to SSM (ssm.tf):
#   - the RDS master/owner role (equivalent to this project's existing
#     POSTGRES_USER/DATABASE_MIGRATION_URL - used only to run Alembic
#     migrations, which need CREATE ROLE / ENABLE ROW LEVEL SECURITY -
#     see db/rls.py and docs/adr/*).
#   - the application's own least-privilege role (APP_DB_USER) - created
#     BY the RLS migration itself, not by Terraform; Terraform only
#     generates and stores the password the migration will use, so both
#     the migration step and the running app agree on it via SSM.

resource "random_password" "rds_master" {
  length  = 32
  special = false # avoid characters that need escaping in a connection-string URL
}

resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${local.name_prefix}-db-subnets" }
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name_prefix}-db"
  engine         = "postgres"
  engine_version = var.rds_engine_version

  instance_class    = var.rds_instance_class
  allocated_storage = var.rds_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "dealership"
  username = "dealership_owner"
  password = random_password.rds_master.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false # single-AZ: this is a portfolio deployment, not HA production - see COST.md

  # Portfolio project, synthetic + public-dataset data only (see
  # CLAUDE.md's Data Honesty section) - no compliance/business need to
  # keep a final snapshot around after destroy (which itself costs
  # storage indefinitely), and a 1-day backup window is enough to
  # recover from an accidental bad migration without paying for a long
  # retention window.
  skip_final_snapshot     = true
  backup_retention_period = 1

  # pgvector doesn't need a custom parameter group / shared_preload_libraries -
  # it's available as a CREATE EXTENSION on RDS Postgres 15.2+/16.1+ directly.
  apply_immediately = true

  tags = { Name = "${local.name_prefix}-db" }
}
