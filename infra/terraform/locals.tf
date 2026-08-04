locals {
  # Step 11, Part B3: applied to every resource, either via this map
  # directly or via the provider's default_tags (main.tf) - default_tags
  # covers most resources automatically; a few resource types that don't
  # inherit default_tags cleanly (e.g. some data sources, or resources
  # needing per-resource tag overrides) reference local.common_tags
  # explicitly instead.
  common_tags = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "terraform"
  }

  name_prefix = "${var.project_name}-${var.environment}"
}
