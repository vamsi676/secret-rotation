# Run Command output (stdout/stderr of refresh_secrets.sh) is shipped here by the SSM agent.
resource "aws_cloudwatch_log_group" "ssm" {
  name              = local.ssm_log_group_name
  retention_in_days = var.log_retention_days
}
