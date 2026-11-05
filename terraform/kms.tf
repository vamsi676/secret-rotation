# Customer-managed key for the demo secret (and, in Phase 3, the SNS topic).
data "aws_iam_policy_document" "kms" {
  statement {
    # Standard: the account controls the key through IAM, so IAM policies take effect.
    sid       = "EnableIamPolicies"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
  statement {
    # EventBridge must encrypt messages it publishes to the KMS-encrypted alerts topic.
    sid       = "EventBridgePublishToEncryptedTopic"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "secret" {
  description             = "${var.name_prefix}: encrypts the demo app secret"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "secret" {
  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.secret.key_id
}
