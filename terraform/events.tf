# Secrets Manager emits RotationFailed (and related) service events to EventBridge via CloudTrail
# (no trail required). Route them to the alerts topic.
resource "aws_cloudwatch_event_rule" "rotation_failed" {
  name        = "${var.name_prefix}-rotation-failed"
  description = "Secrets Manager rotation failures -> SNS alerts"
  event_pattern = jsonencode({
    source        = ["aws.secretsmanager"]
    "detail-type" = ["AWS Service Event via CloudTrail"]
    detail = {
      eventName = ["RotationFailed", "RotationAbandoned"]
    }
  })
}

resource "aws_cloudwatch_event_target" "rotation_failed_sns" {
  rule      = aws_cloudwatch_event_rule.rotation_failed.name
  target_id = "sns-alerts"
  arn       = aws_sns_topic.alerts.arn
}
