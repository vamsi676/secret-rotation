# AWS Secrets Manager rotation demo (Terraform + Lambda + SSM)

This demo rotates a JSON secret with a Lambda and then reloads the new values on an EC2 host,
without SSH and without opening any inbound ports.

```
Secrets Manager ──schedule──► Rotation Lambda (Python 3.12)
  JSON secret {svc_a,svc_b,svc_c}     createSecret → setSecret → testSecret → finishSecret
  encrypted with a KMS CMK                   finishSecret: promote → verify AWSCURRENT → SSM SendCommand
        ▲  GetSecretValue (AWSCURRENT only)                                   │ (target by tag)
        │                                                                     ▼
EC2 t3.micro, Amazon Linux 2023 (public subnet, no SSH key, no inbound rules, IMDSv2)
  /root/refresh_secrets.sh: fetch → write /etc/demo-app/secrets.env (0600) → restart → health check
  svc-a :8081   svc-b :8082   svc-c :8083   GET /health → "OK <sha256[:8]>"

Failures go to an SNS topic (email): EC2 refresh failures from the Lambda,
and RotationFailed events from EventBridge.
```

What it shows:
- **Generic rotation:** new values come from `GetRandomPassword`. Every step can safely run twice.
  A failure in create, set or test stops the rotation before the EC2 instance is touched.
- **Refresh after promotion:** the Lambda verifies that AWSCURRENT is the new version, retrying for eventual
  consistency. Only then does it run the refresh script over SSM Run Command, and it waits for the result.
- **The rotation never fails because of EC2:** if the refresh fails, the Lambda logs it and emails you through SNS.
- **The services really load the new value:** the script compares each service's `/health` hash with
  the hash of the new value. An unhealthy service is restarted up to 2 more times.
- **Least-privilege IAM.** Secret values never appear in logs, Terraform state or user-data.

---

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| [Terraform](https://developer.hashicorp.com/terraform/install) | **≥ 1.11** | Ephemeral resources and write-only arguments, so the initial secret never reaches state |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | 2.x | Credentials profile and the test commands below |
| [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) | any | Optional: a shell on the instance from your terminal |
| Python + `boto3` | 3.12+ | Optional: Lambda unit tests |

You need an AWS account and a CLI profile that can create VPC, EC2, IAM, KMS, Secrets Manager, Lambda, SNS,
EventBridge and CloudWatch Logs resources. Everything is created in **us-east-1**.

## 1. Configure

```bash
git clone <this-repo-url>
cd <repo>/terraform
cp terraform.tfvars.example terraform.tfvars   # gitignored
```

Edit `terraform.tfvars`:

```hcl
aws_profile = "default"              # your AWS CLI profile
alert_email = "you@example.com"      # SNS alerts ("" = no email)
# rotation_schedule = "rate(4 hours)" # optional; rate(4 hours) is the minimum
```

Every variable is in [`terraform/variables.tf`](terraform/variables.tf). The most useful ones:

| Variable | Default | Purpose |
|---|---|---|
| `aws_profile` | `sandbox` | AWS CLI profile Terraform uses |
| `alert_email` | `""` | Email subscribed to the alerts topic. You must click the confirmation link. |
| `rotation_schedule` | `rate(4 hours)` | Secrets Manager schedule (`rate()` or `cron()`) |
| `rotate_immediately` | `true` | Rotate once as soon as rotation is enabled |
| `instance_type` | `t3.micro` | EC2 size |
| `fault_injection_step` | `""` | **Demo only:** make the named step fail (e.g. `testSecret`) |

## 2. Deploy

```bash
cd terraform
terraform init
terraform plan
terraform apply          # about 3 minutes, 31 resources
terraform output
```

The outputs include `instance_id`, `secret_name`, `lambda_name`, `lambda_log_group_name`,
`ssm_log_group_name` and `sns_topic_arn`.

What happens on apply:
1. The EC2 instance boots, installs the 3 services and the refresh script, and loads the current secret.
2. Enabling rotation starts the first rotation right away (`rotate_immediately = true`).
3. If you set `alert_email`, confirm the subscription from your inbox.

The commands below use a few shell variables:

```bash
export AWS_PROFILE=<your profile> AWS_REGION=us-east-1
IID=$(terraform output -raw instance_id)
SECRET=$(terraform output -raw secret_name)
LAMBDA_LOGS=$(terraform output -raw lambda_log_group_name)
SSM_LOGS=$(terraform output -raw ssm_log_group_name)
```

## 3. Check the services

```bash
CMD=$(aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
  --parameters 'commands=["for p in 8081 8082 8083; do curl -s 127.0.0.1:$p/health; done"]' \
  --query Command.CommandId --output text)
sleep 5
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$IID" \
  --query StandardOutputContent --output text
# OK 1a2b3c4d
# OK 5e6f7a8b
# OK 9c0d1e2f
```

To open a shell on the instance without SSH, use EC2 console → instance → **Connect → Session Manager**, or:

```bash
aws ssm start-session --target "$IID"
sudo journalctl -u refresh-secrets     # boot-time refresh
sudo /root/refresh_secrets.sh          # manual refresh (safe to run again)
```

## 4. Trigger a rotation and watch it

```bash
aws secretsmanager rotate-secret --secret-id "$SECRET"
aws logs tail "$LAMBDA_LOGS" --follow
```

The Lambda log shows the whole flow. Here is a successful run, shortened:

```
STEP_START createSecret → PENDING_CREATED → STEP_OK
STEP_START setSecret    → SET_NOOP        → STEP_OK
STEP_START testSecret   → TEST_PASSED     → STEP_OK
STEP_START finishSecret → PROMOTED → VERIFIED_CURRENT
SSM_SEND_COMMAND  document=AWS-RunShellScript targets=tag:SecretRotationTarget=demo
                  commands=["/root/refresh_secrets.sh <new-version-id>"]
SSM_STATUS Pending → InProgress → Success
[ssm i-…] ========== [1/5] Fetch AWSCURRENT from Secrets Manager ==========
[ssm i-…] ========== [4/5] Restart services ==========
[ssm i-…] + systemctl restart svc@a.service svc@b.service svc@c.service
[ssm i-…] svc@a.service: pid 2134 -> 2324, state=active
[ssm i-…] PASS svc-a port=8081 hash=34ff3f9e restart_retries=0
[ssm i-…] RESULT OK version=<new-version-id>
SSM_REFRESH_OK → STEP_OK finishSecret
```

Check the version stages afterwards:

```bash
aws secretsmanager describe-secret --secret-id "$SECRET" --query VersionIdsToStages
```

### Where to find the logs

| What | Where |
|---|---|
| Rotation steps, the SSM call, and the script output line by line | CloudWatch Logs → `/aws/lambda/secret-rotation-demo-rotation` |
| Full script output for each run | CloudWatch Logs → `/aws/ssm/secret-rotation-demo`, stream `<commandId>/<instanceId>/aws-runShellScript/stdout` |
| Command history and output for each instance | Systems Manager → Run Command → Command history |
| Boot-time refreshes | On the instance: `journalctl -u refresh-secrets` |
| Who sent the command | CloudTrail → Event history → `SendCommand` |

Both log groups keep logs for 14 days.

## 5. Failure scenarios to try

| Scenario | How | What to expect |
|---|---|---|
| Service unhealthy after restart | On the instance: `sudo touch /etc/demo-app/fault-svc-b`, then rotate | svc-b is restarted 2 more times, then `FAIL`. SSM shows `Failed`, the Lambda logs `SSM_REFRESH_FAILED` and you get an **SNS email**. The rotation still succeeds. To recover: `sudo rm /etc/demo-app/fault-svc-b && sudo /root/refresh_secrets.sh` |
| Instance stopped during rotation | `aws ec2 stop-instances --instance-ids $IID`, then rotate | The rotation succeeds. SSM finds 0 targets and you get an SNS email. After `start-instances`, the boot refresh loads the new version automatically. |
| Rotation step fails | `terraform apply -var fault_injection_step=testSecret`, then rotate | `testSecret` raises. AWSCURRENT is **unchanged** and **no SSM command** is sent. A `RotationFailed` event is emailed. Reset with `terraform apply` (no `-var`). |
| Rotate twice quickly | Run `rotate-secret` twice in a row | The second call is rejected (`A previous rotation isn't complete`). The first completes normally. |

## 6. Unit tests (offline)

The tests use botocore's `Stubber` and make no AWS calls:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install boto3
python -m unittest discover -s lambda/rotation/tests -v
```

## 7. Clean up

```bash
cd terraform
terraform destroy
```

- The secret is deleted **immediately** (`recovery_window_in_days = 0`, demo setting).
- The KMS key enters a **7-day** pending-deletion window, which is the AWS minimum. It is disabled right away.
- Log groups, Lambda, IAM, SNS, VPC and EC2 are all removed.

To check that nothing is left:

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=secret-rotation-demo
```

The tag API can list terminated or deleted items for a while after a destroy.

## Cost

While it is deployed, the cost is about **$0.02/hour**: t3.micro, its public IPv4 address and an 8 GB gp3 volume.
On top of that come about $1/month for the KMS key (prorated) and $0.40/month for the secret (prorated).
Lambda, SNS and CloudWatch usage stays within the free tier. There is no NAT gateway.
Destroy the stack when you are done.

## Repository layout

```
terraform/            all infrastructure (one root module)
  network.tf ec2.tf iam_ec2.tf logs.tf           Phase 1: VPC, EC2, instance role, SSM output logs
  kms.tf secret.tf                               Phase 2: CMK + secret (initial value is write-only)
  lambda.tf iam_lambda.tf sns.tf events.tf rotation.tf   Phase 3: rotation Lambda, alerts, schedule
ec2/                  user-data template, refresh_secrets.sh, demo service, systemd units
lambda/rotation/      rotation.py + offline unit tests
docs/README.md        design notes, IAM detail, edge cases, phase-by-phase test log
```

## Security notes

- **EC2 role:**
  - `GetSecretValue` on this secret, and only when the request asks for `AWSCURRENT`.
  - `kms:Decrypt` on this key, only through Secrets Manager and only for this secret.
  - The standard SSM agent policy.
- **Lambda role:**
  - Rotate only this secret, and use the key only through Secrets Manager or SNS.
  - `SendCommand` only with `AWS-RunShellScript`, and only on instances tagged `SecretRotationTarget=demo`.
  - Write its own logs and publish to its own topic.
- **Who can invoke the Lambda:** only `secretsmanager.amazonaws.com`, for this secret ARN and this account.
- **On the instance:** `secrets.env` is `root:root 0600`. systemd injects it into services that run as the
  unprivileged `demoapp` user. Logs show only version IDs and 8-character hash prefixes.
- **Not in Terraform state:** the initial secret comes from `ephemeral "random_password"` through `secret_string_wo`.

Design details, the full IAM breakdown and the edge-case analysis are in [docs/README.md](docs/README.md).
