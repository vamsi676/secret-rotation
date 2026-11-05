resource "aws_secretsmanager_secret" "app" {
  name        = local.secret_name
  description = "Demo app secrets (one key per service). Rotated by the rotation Lambda."
  kms_key_id  = aws_kms_key.secret.arn

  # Demo only: delete immediately on destroy so the name can be reused right away.
  recovery_window_in_days = 0
}

# Initial values are generated ephemerally and passed through a write-only
# argument, so they are NEVER stored in Terraform state or plan files.
# Character set matches the rotation Lambda: no quotes, backslash, $, backtick or space.
ephemeral "random_password" "initial" {
  for_each         = toset(local.service_keys)
  length           = 32
  special          = true
  override_special = "!#%*+,-.:=?@^_~"
}

resource "aws_secretsmanager_secret_version" "initial" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string_wo = jsonencode({
    for k in local.service_keys : k => ephemeral.random_password.initial[k].result
  })
  # Bump only to force a new initial value from Terraform (normally rotation owns this).
  secret_string_wo_version = 1

  lifecycle {
    # After rotation starts, Secrets Manager moves stages; Terraform must not fight it.
    ignore_changes = [version_stages]
  }
}
