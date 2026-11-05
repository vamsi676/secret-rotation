# Secrets Manager rotation demo

A Secrets Manager secret (JSON, one key per service, KMS CMK) is rotated by a single
Python 3.12 Lambda. After the new version is promoted to AWSCURRENT and verified, the
Lambda uses SSM Run Command to run `/root/refresh_secrets.sh` on a tagged EC2 instance.
That script reloads 3 sample services and checks their health.

```
Secrets Manager ──schedule──► Rotation Lambda: create → set → test → finish
  (KMS CMK)                                     finish: promote → verify → SSM SendCommand
      ▲ GetSecretValue (AWSCURRENT)                                         │
EC2 (AL2023, t3.micro, SSM only, no inbound) ◄──── AWS-RunShellScript ─────┘
  refresh-secrets.service (every boot) → /root/refresh_secrets.sh
  svc@a:8081  svc@b:8082  svc@c:8083   (127.0.0.1, user demoapp)
```

## Layout
| Path | What |
|---|---|
| `terraform/` | All infrastructure (`us-east-1`, profile `sandbox`) |
| `ec2/demo_service.py` | Stdlib HTTP service; `/health` → `OK <sha256[:8]>` or `503 NO_SECRET` |
| `ec2/svc@.service` | systemd template unit (`svc@a`, `svc@b`, `svc@c`), runs as `demoapp` |
| `ec2/refresh_secrets.sh` | Fetch AWSCURRENT → write `/etc/demo-app/secrets.env` (root 0600) → restart → verify hashes |
| `ec2/refresh-secrets.service` | Oneshot that runs the refresh script on every boot |
| `ec2/user-data.sh.tftpl` | Bootstrap (code + units + secret *name*; no secret values) |
| `lambda/rotation/` | Rotation Lambda (Phase 3) |

## Security notes
- No SSH key and no inbound SG rules. The SG allows outbound 443 only, and the VPC's default SG is locked down.
- IMDSv2 is required, and the root volume is encrypted.
- `secrets.env` is `root:root 0600`. systemd reads it as root and passes it to the
  unprivileged service via `EnvironmentFile`. Each service reads only its own variable and removes
  the others from its environment at startup. (Trade-off: systemd still hands all 3 variables to each
  process at exec time. That is acceptable for a demo; per-service files or `LoadCredential=`
  would be stricter.)
- Logs only ever contain version IDs and 8-character hash prefixes.

## Phase 1 test steps (EC2 + services + refresh script; no secret yet)
```bash
cd terraform
terraform init && terraform plan        # then apply (after approval)
IID=$(terraform output -raw instance_id)

# Wait until the instance is registered with SSM (1-3 min):
aws ssm describe-instance-information --profile sandbox --region us-east-1 \
  --filters Key=InstanceIds,Values=$IID --query 'InstanceInformationList[].PingStatus'

# Run checks on the box:
CMD=$(aws ssm send-command --profile sandbox --region us-east-1 \
  --instance-ids $IID --document-name AWS-RunShellScript \
  --cloud-watch-output-config CloudWatchOutputEnabled=true,CloudWatchLogGroupName=$(terraform output -raw ssm_log_group_name) \
  --parameters 'commands=["systemctl is-active svc@a svc@b svc@c","for p in 8081 8082 8083; do curl -s -w \" %{http_code}\\n\" 127.0.0.1:$p/health; done","ls -l /root/refresh_secrets.sh /etc/demo-app","/root/refresh_secrets.sh; echo exit=$?"]' \
  --query Command.CommandId --output text)
aws ssm get-command-invocation --profile sandbox --region us-east-1 \
  --command-id $CMD --instance-id $IID --query '[Status,StandardOutputContent]' --output text
```
Expected in Phase 1:
- all 3 units `active`
- each `/health` returns `NO_SECRET 503`
- `refresh_secrets.sh` logs `ERROR: cannot read secret 'secret-rotation-demo/app-secrets'`
  and exits non-zero (the secret is created in Phase 2, and the instance role can't read it yet)
- the output also appears in the `/aws/ssm/secret-rotation-demo` log group

## Phase 2 test steps (KMS + secret, rotation not enabled)
The initial value comes from `ephemeral "random_password"` and is passed to `secret_string_wo`
(write-only), so it is never written to plan or state files.

Run on the box (with the same `send-command` pattern as Phase 1):
```bash
/root/refresh_secrets.sh; echo exit=$?      # expect 3x PASS, RESULT OK, exit=0
/root/refresh_secrets.sh; echo exit=$?      # idempotent: same version, same hashes, exit=0
stat -c '%U:%G %a' /etc/demo-app/secrets.env   # root:root 600
# Independent hash check (prints hashes only):
aws secretsmanager get-secret-value --region us-east-1 --secret-id secret-rotation-demo/app-secrets \
  --version-stage AWSCURRENT --query SecretString --output text \
 | python3 -c 'import sys,json,hashlib; [print(k, hashlib.sha256(v.encode()).hexdigest()[:8]) for k,v in sorted(json.load(sys.stdin).items())]'
# Least privilege: without --version-stage AWSCURRENT the role is denied (AccessDeniedException).
```
State check (locally): `terraform state list | grep random_` returns nothing, and the secret version's
`secret_string` is empty in `terraform show -json`.

## Phase 3: rotation Lambda + IAM + alerts + rotation enabled
Local settings are in `terraform/terraform.tfvars` (gitignored): `aws_profile` and `alert_email`.
The email subscription must be confirmed from the inbox before alerts are delivered.

Unit tests (offline, botocore Stubber; needs boto3):
```bash
python -m unittest discover -s lambda/rotation/tests -v
```
How rotation behaves:
- `createSecret` generates 3 new 32-character values with `GetRandomPassword` and stores them as AWSPENDING.
  If AWSPENDING already exists for this token, the step does nothing.
- `setSecret` does nothing (generic secret).
- `testSecret` checks the shape, the length and the allowed characters, and that every value changed.
- `finishSecret` promotes the new version, then checks that both `DescribeSecret` and a `GetSecretValue(AWSCURRENT)` read
  point to the new version, and only then calls SSM `SendCommand` on `tag:SecretRotationTarget=demo`.
- A failed or timed-out SSM run, or zero matching instances, logs `SSM_REFRESH_FAILED` and publishes to SNS. The rotation still succeeds.
- EventBridge sends `RotationFailed` and `RotationAbandoned` events to the same SNS topic.

Watch a rotation:
```bash
aws secretsmanager rotate-secret --secret-id secret-rotation-demo/app-secrets --profile SuryaMac --region us-east-1
aws logs tail /aws/lambda/secret-rotation-demo-rotation --follow --profile SuryaMac --region us-east-1
aws secretsmanager describe-secret --secret-id secret-rotation-demo/app-secrets \
  --query VersionIdsToStages --profile SuryaMac --region us-east-1
```
Note: after a successful rotation the new version can still show `AWSPENDING` next to `AWSCURRENT`. That is harmless.
The next `createSecret` moves `AWSPENDING` to the new version.

## Where to see what happened during a rotation
| What | Where |
|---|---|
| Rotation steps, the SSM call (document, tag target, command), status changes, and the script output line by line (`[ssm i-…]`) | CloudWatch → `/aws/lambda/secret-rotation-demo-rotation` |
| Raw script output for each run | CloudWatch → `/aws/ssm/secret-rotation-demo`, stream `<commandId>/<instanceId>/aws-runShellScript/stdout` |
| Command history and per-instance output | Systems Manager → Run Command → Command history (comment `secret refresh <version>`) |
| Boot-time refreshes | On the box: `journalctl -u refresh-secrets` (Session Manager shell) |
| Who called SendCommand | CloudTrail → Event history → `SendCommand` |

The script prints 5 numbered steps:
1. fetch AWSCURRENT
2. validate and render the env file
3. install the file
4. restart the services, showing each `pid before -> after`
5. health check, restarting an unhealthy service up to 2 more times

It ends with `RESULT OK|FAIL version=…`.

**Consistency guard:** Secrets Manager reads are eventually consistent. The Lambda retries its AWSCURRENT check for up to 30s
(`VERIFY_WAIT`), then runs `refresh_secrets.sh <version>`. The script refuses to load any version other than that one.

## Cleanup
`terraform destroy -var aws_profile=SuryaMac` (after approval).
- The secret is deleted immediately (`recovery_window_in_days = 0`).
- The KMS key is scheduled for deletion after 7 days, the minimum. It is disabled immediately and is
  billed prorated until then.
