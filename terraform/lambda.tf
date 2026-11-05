locals {
  lambda_name = "${var.name_prefix}-rotation"
}

data "archive_file" "rotation" {
  type        = "zip"
  source_file = "${path.module}/../lambda/rotation/rotation.py"
  output_path = "${path.module}/build/rotation.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.lambda_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "rotation" {
  function_name    = local.lambda_name
  description      = "Rotates ${local.secret_name} and refreshes the EC2 services via SSM"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "rotation.lambda_handler"
  architectures    = ["arm64"]
  filename         = data.archive_file.rotation.output_path
  source_code_hash = data.archive_file.rotation.output_base64sha256
  timeout          = 300
  memory_size      = 128

  environment {
    variables = {
      TARGET_TAG_KEY       = var.target_tag_key
      TARGET_TAG_VALUE     = var.target_tag_value
      SSM_LOG_GROUP        = aws_cloudwatch_log_group.ssm.name
      SNS_TOPIC_ARN        = aws_sns_topic.alerts.arn
      REFRESH_COMMAND      = "/root/refresh_secrets.sh"
      FAULT_INJECTION_STEP = var.fault_injection_step
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# Only Secrets Manager, and only for THIS secret in THIS account, may invoke the function.
resource "aws_lambda_permission" "secretsmanager" {
  statement_id   = "AllowSecretsManagerRotation"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.rotation.function_name
  principal      = "secretsmanager.amazonaws.com"
  source_arn     = aws_secretsmanager_secret.app.arn
  source_account = local.account_id
}
