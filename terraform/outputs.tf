output "instance_id" {
  description = "Demo EC2 instance ID (SSM-managed, no SSH)."
  value       = aws_instance.app.id
}

output "secret_name" {
  description = "Secrets Manager secret name the instance reads."
  value       = local.secret_name
}

output "secret_arn" {
  description = "Secrets Manager secret ARN."
  value       = aws_secretsmanager_secret.app.arn
}

output "kms_key_arn" {
  description = "Customer-managed KMS key encrypting the secret."
  value       = aws_kms_key.secret.arn
}

output "ssm_log_group_name" {
  description = "CloudWatch log group for Run Command output."
  value       = aws_cloudwatch_log_group.ssm.name
}

output "target_tag" {
  description = "Tag used by the rotation Lambda to target the instance via SSM."
  value       = "${var.target_tag_key}=${var.target_tag_value}"
}

output "lambda_name" {
  description = "Rotation Lambda function name."
  value       = aws_lambda_function.rotation.function_name
}

output "lambda_log_group_name" {
  description = "CloudWatch log group for the rotation Lambda."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "sns_topic_arn" {
  description = "SNS topic for rotation / EC2 refresh failure alerts."
  value       = aws_sns_topic.alerts.arn
}

output "rotation_schedule" {
  description = "Rotation schedule expression."
  value       = var.rotation_schedule
}
