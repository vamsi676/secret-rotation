data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.app.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  # No key_name: access is via SSM only.

  credit_specification {
    cpu_credits = "standard" # avoid T3 "unlimited" surplus charges
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    encrypted   = true
  }

  # Gzipped (cloud-init decompresses it) to stay well under the 16 KB user-data limit.
  user_data_base64 = base64gzip(templatefile("${path.module}/../ec2/user-data.sh.tftpl", {
    demo_service_b64   = filebase64("${path.module}/../ec2/demo_service.py")
    refresh_script_b64 = filebase64("${path.module}/../ec2/refresh_secrets.sh")
    svc_unit_b64       = filebase64("${path.module}/../ec2/svc@.service")
    refresh_unit_b64   = filebase64("${path.module}/../ec2/refresh-secrets.service")
    secret_id          = local.secret_name
    region             = var.region
  }))
  # Instance is disposable: a changed bootstrap means a fresh instance.
  user_data_replace_on_change = true

  tags = {
    Name                 = "${var.name_prefix}-app"
    (var.target_tag_key) = var.target_tag_value
  }

  lifecycle {
    # Don't replace the instance every time AWS publishes a new AL2023 AMI.
    ignore_changes = [ami]
  }

  depends_on = [
    aws_route.default_v4,
    aws_route_table_association.public,
    aws_iam_role_policy_attachment.ec2_ssm_core,
  ]
}
