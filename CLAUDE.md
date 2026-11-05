# Project: Secrets Manager rotation demo (personal AWS sandbox)

## Rules
- All infra in Terraform. No manual console changes.
- Region: us-east-1. AWS auth = local AWS CLI profile `sandbox`. Never ask me for keys or account IDs.
- NEVER run `terraform apply` or `terraform destroy` without asking me first.
  `terraform fmt`, `validate`, `plan` are OK.
- No hardcoded secrets anywhere (code, tfvars, user-data, logs). Never print secret values.
- Least-privilege IAM. Tag every resource: Project=secret-rotation-demo.
- Keep cost low: t3.micro, no NAT gateway (public subnet + SSM, no inbound ports).
- After each phase: show what changed, how to test it, then stop and wait for me.

## Structure
terraform/         -> Terraform code
lambda/rotation/   -> Python 3.12 rotation Lambda
ec2/               -> user-data, refresh_secrets.sh, systemd units, sample services
docs/README.md     -> architecture, test steps, cleanup steps