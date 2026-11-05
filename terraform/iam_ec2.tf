data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${var.name_prefix}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Standard SSM agent permissions (Session Manager / Run Command).
resource "aws_iam_role_policy_attachment" "ec2_ssm_core" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# The SSM agent uses the instance credentials to ship Run Command output to CloudWatch Logs.
data "aws_iam_policy_document" "ec2_ssm_logs" {
  statement {
    sid     = "SsmOutputToOurLogGroupOnly"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = [
      aws_cloudwatch_log_group.ssm.arn,
      "${aws_cloudwatch_log_group.ssm.arn}:log-stream:*",
    ]
  }
  statement {
    # The agent checks that the group exists before writing; read-only, no resource scoping beyond the account.
    sid       = "AgentLogGroupLookup"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"]
  }
}

resource "aws_iam_role_policy" "ec2_ssm_logs" {
  name   = "ssm-output-logs"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ec2_ssm_logs.json
}

# Read ONLY the current version of OUR secret, and decrypt ONLY via Secrets Manager for that secret.
data "aws_iam_policy_document" "ec2_secret_read" {
  statement {
    sid       = "ReadCurrentVersionOfDemoSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
    condition {
      test     = "StringEquals"
      variable = "secretsmanager:VersionStage"
      values   = ["AWSCURRENT"]
    }
  }
  statement {
    sid       = "DecryptDemoSecretViaSecretsManager"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.secret.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:SecretARN"
      values   = [aws_secretsmanager_secret.app.arn]
    }
  }
}

resource "aws_iam_role_policy" "ec2_secret_read" {
  name   = "read-demo-secret"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ec2_secret_read.json
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.name_prefix}-ec2"
  role = aws_iam_role.ec2.name
}
