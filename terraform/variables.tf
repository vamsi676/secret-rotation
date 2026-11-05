variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Local AWS CLI profile used by Terraform."
  type        = string
  default     = "sandbox"
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "secret-rotation-demo"
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
  default     = "t3.micro"
}

variable "vpc_cidr" {
  description = "CIDR for the demo VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet."
  type        = string
  default     = "10.42.1.0/24"
}

variable "target_tag_key" {
  description = "Tag key the rotation Lambda uses to target the EC2 instance via SSM."
  type        = string
  default     = "SecretRotationTarget"
}

variable "target_tag_value" {
  description = "Tag value the rotation Lambda uses to target the EC2 instance via SSM."
  type        = string
  default     = "demo"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda and SSM output."
  type        = number
  default     = 14
}

variable "rotation_schedule" {
  description = "Secrets Manager rotation schedule (rate() or cron()). Minimum is rate(4 hours)."
  type        = string
  default     = "rate(4 hours)"
}

variable "rotate_immediately" {
  description = "Rotate once as soon as rotation is enabled/changed."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email subscribed to the alerts SNS topic (\"\" = no subscription). Must confirm via email."
  type        = string
  default     = ""
}

variable "fault_injection_step" {
  description = "DEMO ONLY: rotation step that the Lambda fails on purpose (\"\", createSecret, setSecret, testSecret, finishSecret)."
  type        = string
  default     = ""
  validation {
    condition     = contains(["", "createSecret", "setSecret", "testSecret", "finishSecret"], var.fault_injection_step)
    error_message = "fault_injection_step must be empty or a rotation step name."
  }
}
