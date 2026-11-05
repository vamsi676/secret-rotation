"""Secrets Manager rotation Lambda for the secret-rotation demo.

Generic (non-database) JSON secret with one key per service. Steps:
  createSecret  - generate new values -> AWSPENDING            (idempotent)
  setSecret     - no downstream system for a generic secret    (no-op)
  testSecret    - validate AWSPENDING                          (raises on any problem)
  finishSecret  - promote AWSPENDING -> AWSCURRENT, verify, then refresh EC2 via SSM

Any exception in create/set/test propagates, so Secrets Manager never calls
finishSecret and the EC2 refresh never runs. The EC2 refresh happens only after
AWSCURRENT is verified to point at the new version; its failure is logged and
published to SNS but does NOT fail the rotation (the secret is already rotated).

Secret values are never logged: only version IDs, key names and SSM output
(which itself contains only 8-char hashes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SERVICE_KEYS = ("svc_a", "svc_b", "svc_c")
PASSWORD_LENGTH = 32
# Keep values safe for a systemd EnvironmentFile and shell: allowed specials are
# !#%*+,-.:=?@^_~ (same set Terraform uses for the initial value).
EXCLUDE_CHARACTERS = "\"$&'()/;<>[\\]`{|} "

SSM_DELIVERY_TIMEOUT_S = 60       # SendCommand TimeoutSeconds (time to start on the instance)
SSM_EXECUTION_TIMEOUT_S = 200     # AWS-RunShellScript executionTimeout
SSM_POLL_INTERVAL_S = 5
LAMBDA_SAFETY_MARGIN_MS = 20_000  # stop waiting for SSM this long before the Lambda timeout
# Secrets Manager reads are eventually consistent: right after a stage move,
# GetSecretValue(AWSCURRENT) can still return the previous version for a few seconds.
VERIFY_TIMEOUT_S = 30
VERIFY_INTERVAL_S = 2
VERSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

_CFG = Config(retries={"mode": "standard", "max_attempts": 5})
secrets = boto3.client("secretsmanager", config=_CFG)
ssm = boto3.client("ssm", config=_CFG)
sns = boto3.client("sns", config=_CFG)


def _log(level: int, event: str, **fields) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


# --------------------------------------------------------------------------- handler

def lambda_handler(event, context):
    arn = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]
    _log(logging.INFO, "STEP_START", step=step, version=token)

    meta = secrets.describe_secret(SecretId=arn)
    if not meta.get("RotationEnabled"):
        raise ValueError(f"Rotation is not enabled for secret {arn}")
    versions = meta.get("VersionIdsToStages", {})
    if token not in versions:
        raise ValueError(f"Version {token} has no stage for rotation of secret {arn}")
    stages = versions[token]
    if "AWSCURRENT" in stages:
        if step != "finishSecret":
            _log(logging.INFO, "ALREADY_CURRENT", step=step, version=token)
            return
        # finishSecret retry after promotion: fall through to verify + (idempotent) refresh.
    elif "AWSPENDING" not in stages:
        raise ValueError(f"Version {token} not set as AWSPENDING for rotation of secret {arn}")

    fault = os.environ.get("FAULT_INJECTION_STEP", "")
    if fault and fault == step:
        raise RuntimeError(f"FAULT_INJECTION: forced failure in {step} (demo)")

    if step == "createSecret":
        create_secret(arn, token)
    elif step == "setSecret":
        set_secret(arn, token)
    elif step == "testSecret":
        test_secret(arn, token)
    elif step == "finishSecret":
        finish_secret(arn, token, context)
    else:
        raise ValueError(f"Invalid step parameter: {step}")
    _log(logging.INFO, "STEP_OK", step=step, version=token)


# --------------------------------------------------------------------------- steps

def create_secret(arn: str, token: str) -> None:
    current = _load_json(secrets.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT"))
    try:
        secrets.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
        _log(logging.INFO, "PENDING_EXISTS", version=token)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    new_value = dict(current)  # preserve any extra keys
    for key in SERVICE_KEYS:
        new_value[key] = secrets.get_random_password(
            PasswordLength=PASSWORD_LENGTH,
            ExcludeCharacters=EXCLUDE_CHARACTERS,
            RequireEachIncludedType=True,
        )["RandomPassword"]
    secrets.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps(new_value),
        VersionStages=["AWSPENDING"],
    )
    _log(logging.INFO, "PENDING_CREATED", version=token, keys=list(SERVICE_KEYS))


def set_secret(arn: str, token: str) -> None:
    # Generic secret: there is no downstream system (DB, API) to update.
    # In a real integration this is where the new credential is installed on the target.
    _log(logging.INFO, "SET_NOOP", version=token)


def test_secret(arn: str, token: str) -> None:
    pending = _load_json(secrets.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING"))
    current = _load_json(secrets.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT"))
    _validate(pending)
    same = [k for k in SERVICE_KEYS if pending[k] == current.get(k)]
    if same:
        raise ValueError(f"AWSPENDING values unchanged from AWSCURRENT for keys: {same}")
    _log(logging.INFO, "TEST_PASSED", version=token)


def finish_secret(arn: str, token: str, context) -> None:
    meta = secrets.describe_secret(SecretId=arn)
    current_version = _version_with_stage(meta, "AWSCURRENT")
    if current_version == token:
        _log(logging.INFO, "ALREADY_PROMOTED", version=token)
    else:
        secrets.update_secret_version_stage(
            SecretId=arn,
            VersionStage="AWSCURRENT",
            MoveToVersionId=token,
            RemoveFromVersionId=current_version,
        )
        _log(logging.INFO, "PROMOTED", version=token, previous=current_version)

    # Verify before touching EC2: both the stage map and an actual AWSCURRENT read must agree.
    # Retry briefly to ride out eventual consistency; fail (no SSM) if it never converges.
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    attempt = 0
    while True:
        attempt += 1
        staged = _version_with_stage(secrets.describe_secret(SecretId=arn), "AWSCURRENT")
        read = secrets.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT")["VersionId"]
        if staged == token and read == token:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"AWSCURRENT verification failed after {attempt} attempts: "
                               f"expected {token}, staged={staged}, read={read}")
        _log(logging.INFO, "VERIFY_WAIT", attempt=attempt, expected=token, staged=staged, read=read)
        time.sleep(VERIFY_INTERVAL_S)
    _log(logging.INFO, "VERIFIED_CURRENT", version=token, attempts=attempt)

    refresh_ec2(arn, token, context)


# --------------------------------------------------------------------------- EC2 refresh

def refresh_ec2(arn: str, version: str, context) -> dict:
    """Run the refresh script on tagged instances. Never raises."""
    tag_key = os.environ["TARGET_TAG_KEY"]
    tag_value = os.environ["TARGET_TAG_VALUE"]
    result: dict = {"secret": arn, "version": version, "target": f"{tag_key}={tag_value}"}
    try:
        targets = [{"Key": f"tag:{tag_key}", "Values": [tag_value]}]
        if not VERSION_ID_RE.fullmatch(version):
            raise ValueError(f"unexpected version id format: {version!r}")
        # Pass the expected version so the script refuses to load a stale AWSCURRENT.
        commands = [f"{os.environ.get('REFRESH_COMMAND', '/root/refresh_secrets.sh')} {version}"]
        log_group = os.environ["SSM_LOG_GROUP"]
        _log(logging.INFO, "SSM_SEND_COMMAND", document="AWS-RunShellScript", targets=targets,
             commands=commands, execution_timeout_s=SSM_EXECUTION_TIMEOUT_S,
             delivery_timeout_s=SSM_DELIVERY_TIMEOUT_S, output_log_group=log_group)
        resp = ssm.send_command(
            Targets=targets,
            DocumentName="AWS-RunShellScript",
            Comment=f"secret refresh {version}"[:100],
            TimeoutSeconds=SSM_DELIVERY_TIMEOUT_S,
            Parameters={
                "commands": commands,
                "executionTimeout": [str(SSM_EXECUTION_TIMEOUT_S)],
            },
            CloudWatchOutputConfig={
                "CloudWatchOutputEnabled": True,
                "CloudWatchLogGroupName": log_group,
            },
        )
        command_id = resp["Command"]["CommandId"]
        result["command_id"] = command_id
        _log(logging.INFO, "SSM_SENT", command_id=command_id, version=version)

        cmd = _wait_for_command(command_id, context)
        result.update(
            status=cmd.get("Status"),
            target_count=cmd.get("TargetCount", 0),
            completed_count=cmd.get("CompletedCount", 0),
            error_count=cmd.get("ErrorCount", 0),
            delivery_timed_out_count=cmd.get("DeliveryTimedOutCount", 0),
        )
        result["instances"] = _invocations(command_id)
        _log_ssm_output(command_id, result["instances"], log_group)

        ok = (
            result["status"] == "Success"
            and result["target_count"] > 0
            and result["instances"]
            and all(i["status"] == "Success" for i in result["instances"])
        )
        if ok:
            # Output was already logged line by line above; keep the summary compact.
            summary = {**result, "instances": [{k: v for k, v in i.items() if k != "output_tail"}
                                               for i in result["instances"]]}
            _log(logging.INFO, "SSM_REFRESH_OK", **summary)
        else:
            if result["target_count"] == 0:
                result["reason"] = "no instances matched the target tag (stopped, terminated or not SSM-managed)"
            elif result["status"] in ("Pending", "InProgress", "Delayed"):
                result["reason"] = "still running when the Lambda stopped waiting"
            else:
                result["reason"] = "refresh command did not succeed on all instances"
            _report_failure(result)
    except Exception as e:  # noqa: BLE001 - must never fail the rotation here
        result.update(status="ERROR", reason=f"{type(e).__name__}: {e}")
        _report_failure(result)
    return result


def _wait_for_command(command_id: str, context) -> dict:
    terminal = {"Success", "Failed", "TimedOut", "Cancelled"}
    cmd: dict = {}
    last_status = None
    while True:
        cmds = ssm.list_commands(CommandId=command_id).get("Commands", [])
        cmd = cmds[0] if cmds else {}
        if cmd.get("Status") != last_status:
            last_status = cmd.get("Status")
            _log(logging.INFO, "SSM_STATUS", command_id=command_id, status=last_status,
                 targets=cmd.get("TargetCount"), completed=cmd.get("CompletedCount"))
        if cmd.get("Status") in terminal:
            return cmd
        if context.get_remaining_time_in_millis() < LAMBDA_SAFETY_MARGIN_MS + SSM_POLL_INTERVAL_S * 1000:
            return cmd
        time.sleep(SSM_POLL_INTERVAL_S)


def _invocations(command_id: str) -> list[dict]:
    out = []
    pages = ssm.get_paginator("list_command_invocations").paginate(CommandId=command_id, Details=True)
    for page in pages:
        for inv in page.get("CommandInvocations", []):
            plugin = (inv.get("CommandPlugins") or [{}])[0]
            out.append({
                "instance_id": inv.get("InstanceId"),
                "status": inv.get("Status"),
                "status_details": inv.get("StatusDetails"),
                "response_code": plugin.get("ResponseCode"),
                # Output holds only PASS/FAIL lines with 8-char hashes and version ids.
                "output_tail": (plugin.get("Output") or "")[-2500:],
            })
    return out


def _log_ssm_output(command_id: str, instances: list[dict], log_group: str) -> None:
    """Echo the remote script output as readable, one-line-per-line log entries."""
    for inst in instances:
        iid = inst.get("instance_id")
        logger.info("===== SSM output: command %s on %s (status=%s, exit=%s) =====",
                    command_id, iid, inst.get("status"), inst.get("response_code"))
        for line in (inst.get("output_tail") or "").splitlines():
            logger.info("[ssm %s] %s", iid, line)
        logger.info("===== full output: CloudWatch %s, stream %s/%s/aws-runShellScript/stdout =====",
                    log_group, command_id, iid)


def _report_failure(result: dict) -> None:
    _log(logging.ERROR, "SSM_REFRESH_FAILED", **result)
    topic = os.environ.get("SNS_TOPIC_ARN")
    if not topic:
        return
    body = {
        "summary": "Secret was rotated (AWSCURRENT updated) but the EC2 refresh did not succeed.",
        "action": "Check the SSM output log group, fix the instance, then rerun /root/refresh_secrets.sh "
                  "(idempotent). A stopped instance refreshes automatically on next boot.",
        "ssm_log_group": os.environ.get("SSM_LOG_GROUP"),
        **result,
    }
    try:
        sns.publish(
            TopicArn=topic,
            Subject="[secret-rotation-demo] EC2 secret refresh FAILED"[:100],
            Message=json.dumps(body, indent=2, default=str),
        )
        _log(logging.INFO, "SNS_PUBLISHED", topic=topic)
    except Exception as e:  # noqa: BLE001
        _log(logging.ERROR, "SNS_PUBLISH_FAILED", error=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- helpers

def _load_json(resp: dict) -> dict:
    try:
        data = json.loads(resp["SecretString"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Secret version {resp.get('VersionId')} is not a JSON string") from None
    if not isinstance(data, dict):
        raise ValueError(f"Secret version {resp.get('VersionId')} is not a JSON object")
    return data


def _validate(data: dict) -> None:
    for key in SERVICE_KEYS:
        val = data.get(key)
        if not isinstance(val, str) or len(val) != PASSWORD_LENGTH:
            raise ValueError(f"key '{key}' missing or wrong length")
        if any(c in EXCLUDE_CHARACTERS or ord(c) < 0x20 or ord(c) == 0x7F for c in val):
            raise ValueError(f"key '{key}' contains disallowed characters")


def _version_with_stage(meta: dict, stage: str) -> str | None:
    for version, stages in meta.get("VersionIdsToStages", {}).items():
        if stage in stages:
            return version
    return None
