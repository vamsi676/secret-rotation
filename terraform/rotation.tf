resource "aws_secretsmanager_secret_rotation" "app" {
  secret_id           = aws_secretsmanager_secret.app.id
  rotation_lambda_arn = aws_lambda_function.rotation.arn
  rotate_immediately  = var.rotate_immediately

  rotation_rules {
    schedule_expression = var.rotation_schedule
  }

  depends_on = [
    aws_lambda_permission.secretsmanager,
    aws_iam_role_policy.lambda,
    aws_sns_topic_policy.alerts,
  ]
}
