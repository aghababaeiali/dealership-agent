resource "aws_cloudwatch_log_group" "app" {
  name = "/ecs/${local.name_prefix}"
  # Explicit, not the provider/AWS default (which is "never expire" -
  # logs accumulate storage cost forever otherwise). 7 days is enough to
  # debug a recent deployment issue without paying to retain months of
  # logs for a low-traffic portfolio service.
  retention_in_days = var.log_retention_days
}
