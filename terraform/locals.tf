locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Secret name is fixed up front so EC2 user-data never changes between phases.
  secret_name = "${var.name_prefix}/app-secrets"

  ssm_log_group_name = "/aws/ssm/${var.name_prefix}"

  # JSON keys in the secret, one per service (svc-a/b/c).
  service_keys = ["svc_a", "svc_b", "svc_c"]
}
