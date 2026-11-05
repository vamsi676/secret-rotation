"""Offline unit tests for rotation.py (botocore Stubber, no AWS calls).

Run:  python -m unittest discover -s lambda/rotation/tests -v
"""
import io
import json
import logging
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.update({
    "TARGET_TAG_KEY": "SecretRotationTarget",
    "TARGET_TAG_VALUE": "demo",
    "SSM_LOG_GROUP": "/aws/ssm/test",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:111111111111:test",
    "REFRESH_COMMAND": "/root/refresh_secrets.sh",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from botocore.stub import ANY, Stubber  # noqa: E402

import rotation  # noqa: E402

ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:demo-AbCdEf"
OLD = "old-version-0000000000000000000000"
NEW = "new-version-1111111111111111111111"
CMD_ID = "11111111-2222-3333-4444-555555555555"
OLD_VALUES = {"svc_a": "A" * 32, "svc_b": "B" * 32, "svc_c": "C" * 32}
NEW_VALUES = {"svc_a": "a1!" + "x" * 29, "svc_b": "b2#" + "y" * 29, "svc_c": "c3%" + "z" * 29}


class Ctx:
    def __init__(self, ms=300_000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


def describe(stages, enabled=True):
    return {"ARN": ARN, "Name": "demo", "RotationEnabled": enabled, "VersionIdsToStages": stages}


def gsv(version, values):
    return {"ARN": ARN, "Name": "demo", "VersionId": version, "SecretString": json.dumps(values)}


class Base(unittest.TestCase):
    def setUp(self):
        self.sm = Stubber(rotation.secrets)
        self.ssm = Stubber(rotation.ssm)
        self.sns = Stubber(rotation.sns)
        for s in (self.sm, self.ssm, self.sns):
            s.activate()
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger().addHandler(self.handler)
        os.environ.pop("FAULT_INJECTION_STEP", None)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)
        for s in (self.sm, self.ssm, self.sns):
            s.deactivate()

    def assert_no_secret_values_logged(self):
        text = self.log.getvalue()
        for v in list(OLD_VALUES.values()) + list(NEW_VALUES.values()):
            self.assertNotIn(v, text)

    def event(self, step):
        return {"SecretId": ARN, "ClientRequestToken": NEW, "Step": step}


class TestEntryChecks(Base):
    def test_rotation_disabled_raises(self):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"]}, enabled=False))
        with self.assertRaises(ValueError):
            rotation.lambda_handler(self.event("createSecret"), Ctx())

    def test_unknown_token_raises(self):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"]}))
        with self.assertRaises(ValueError):
            rotation.lambda_handler(self.event("createSecret"), Ctx())

    def test_fault_injection_raises_before_work(self):
        os.environ["FAULT_INJECTION_STEP"] = "testSecret"
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))
        with self.assertRaisesRegex(RuntimeError, "FAULT_INJECTION"):
            rotation.lambda_handler(self.event("testSecret"), Ctx())
        self.sm.assert_no_pending_responses()


class TestCreate(Base):
    def test_creates_pending(self):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))
        self.sm.add_response("get_secret_value", gsv(OLD, OLD_VALUES), {"SecretId": ARN, "VersionStage": "AWSCURRENT"})
        self.sm.add_client_error("get_secret_value", "ResourceNotFoundException",
                                 expected_params={"SecretId": ARN, "VersionId": NEW, "VersionStage": "AWSPENDING"})
        for k in rotation.SERVICE_KEYS:
            self.sm.add_response("get_random_password", {"RandomPassword": NEW_VALUES[k]},
                                 {"PasswordLength": 32, "ExcludeCharacters": rotation.EXCLUDE_CHARACTERS,
                                  "RequireEachIncludedType": True})
        self.sm.add_response("put_secret_value", {"ARN": ARN, "VersionId": NEW},
                             {"SecretId": ARN, "ClientRequestToken": NEW,
                              "SecretString": json.dumps(NEW_VALUES), "VersionStages": ["AWSPENDING"]})
        rotation.lambda_handler(self.event("createSecret"), Ctx())
        self.sm.assert_no_pending_responses()
        self.assert_no_secret_values_logged()

    def test_idempotent_when_pending_exists(self):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))
        self.sm.add_response("get_secret_value", gsv(OLD, OLD_VALUES))
        self.sm.add_response("get_secret_value", gsv(NEW, NEW_VALUES))
        rotation.lambda_handler(self.event("createSecret"), Ctx())  # no put_secret_value stubbed
        self.sm.assert_no_pending_responses()


class TestTest(Base):
    def _run(self, pending):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))
        self.sm.add_response("get_secret_value", gsv(NEW, pending))
        self.sm.add_response("get_secret_value", gsv(OLD, OLD_VALUES))
        rotation.lambda_handler(self.event("testSecret"), Ctx())

    def test_passes(self):
        self._run(NEW_VALUES)
        self.assert_no_secret_values_logged()

    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            self._run({"svc_a": NEW_VALUES["svc_a"], "svc_b": NEW_VALUES["svc_b"]})

    def test_unchanged_value_raises(self):
        with self.assertRaises(ValueError):
            self._run({**NEW_VALUES, "svc_c": OLD_VALUES["svc_c"]})

    def test_bad_char_raises(self):
        with self.assertRaises(ValueError):
            self._run({**NEW_VALUES, "svc_a": "$" + "x" * 31})

    def test_error_message_has_no_value(self):
        try:
            self._run({**NEW_VALUES, "svc_a": "$" + "x" * 31})
        except ValueError as e:
            self.assertNotIn("x" * 31, str(e))


class TestFinish(Base):
    def _promote_and_verify(self, read_version=NEW):
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))  # handler
        self.sm.add_response("describe_secret", describe({OLD: ["AWSCURRENT"], NEW: ["AWSPENDING"]}))  # finish
        self.sm.add_response("update_secret_version_stage", {"ARN": ARN},
                             {"SecretId": ARN, "VersionStage": "AWSCURRENT",
                              "MoveToVersionId": NEW, "RemoveFromVersionId": OLD})
        self.sm.add_response("describe_secret", describe({OLD: ["AWSPREVIOUS"], NEW: ["AWSCURRENT", "AWSPENDING"]}))
        self.sm.add_response("get_secret_value", gsv(read_version, NEW_VALUES),
                             {"SecretId": ARN, "VersionStage": "AWSCURRENT"})

    def _ssm_command(self, status, targets=1, inv_status=None, output="RESULT OK"):
        self.ssm.add_response("send_command", {"Command": {"CommandId": CMD_ID}},
                              {"Targets": [{"Key": "tag:SecretRotationTarget", "Values": ["demo"]}],
                               "DocumentName": "AWS-RunShellScript", "Comment": ANY, "TimeoutSeconds": 60,
                               "Parameters": {"commands": [f"/root/refresh_secrets.sh {NEW}"],
                                              "executionTimeout": ["200"]},
                               "CloudWatchOutputConfig": {"CloudWatchOutputEnabled": True,
                                                          "CloudWatchLogGroupName": "/aws/ssm/test"}})
        self.ssm.add_response("list_commands", {"Commands": [{"CommandId": CMD_ID, "Status": status,
                                                              "TargetCount": targets, "CompletedCount": targets,
                                                              "ErrorCount": 0 if status == "Success" else targets}]})
        invs = [] if targets == 0 else [{"CommandId": CMD_ID, "InstanceId": "i-123", "Status": inv_status or status,
                                         "CommandPlugins": [{"ResponseCode": 0 if status == "Success" else 2,
                                                             "Output": output}]}]
        self.ssm.add_response("list_command_invocations", {"CommandInvocations": invs})

    def test_promote_verify_refresh_ok(self):
        self._promote_and_verify()
        self._ssm_command("Success")
        with mock.patch.object(rotation, "_report_failure") as rf:
            rotation.lambda_handler(self.event("finishSecret"), Ctx())
        rf.assert_not_called()
        self.sm.assert_no_pending_responses()
        self.ssm.assert_no_pending_responses()
        self.assert_no_secret_values_logged()

    def test_eventually_consistent_read_is_retried(self):
        # First AWSCURRENT read still returns OLD (the real-world case seen in AWS), second returns NEW.
        self._promote_and_verify(read_version=OLD)
        self.sm.add_response("describe_secret", describe({OLD: ["AWSPREVIOUS"], NEW: ["AWSCURRENT", "AWSPENDING"]}))
        self.sm.add_response("get_secret_value", gsv(NEW, NEW_VALUES))
        with mock.patch.object(rotation.time, "sleep"), mock.patch.object(rotation, "refresh_ec2") as refresh:
            rotation.lambda_handler(self.event("finishSecret"), Ctx())
        refresh.assert_called_once()
        self.assertIn("VERIFY_WAIT", self.log.getvalue())

    def test_verification_mismatch_raises_and_skips_ssm(self):
        self._promote_and_verify(read_version=OLD)
        with mock.patch.object(rotation, "VERIFY_TIMEOUT_S", 0), \
             mock.patch.object(rotation, "refresh_ec2") as refresh:
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                rotation.lambda_handler(self.event("finishSecret"), Ctx())
        refresh.assert_not_called()

    def test_ssm_failure_publishes_sns_but_does_not_raise(self):
        self._promote_and_verify()
        self._ssm_command("Failed", output="FAIL svc-b port=8082 expected=abcd1234 got='OK 00000000'\nRESULT FAIL")
        self.sns.add_response("publish", {"MessageId": "m-1"},
                              {"TopicArn": os.environ["SNS_TOPIC_ARN"], "Subject": ANY, "Message": ANY})
        rotation.lambda_handler(self.event("finishSecret"), Ctx())  # must not raise
        self.sns.assert_no_pending_responses()
        self.assertIn("SSM_REFRESH_FAILED", self.log.getvalue())

    def test_no_targets_reports_reason(self):
        self._promote_and_verify()
        self._ssm_command("Success", targets=0)
        with mock.patch.object(rotation, "_report_failure") as rf:
            rotation.lambda_handler(self.event("finishSecret"), Ctx())
        self.assertIn("no instances matched", rf.call_args[0][0]["reason"])

    def test_send_command_access_denied_does_not_raise(self):
        self._promote_and_verify()
        self.ssm.add_client_error("send_command", "AccessDeniedException")
        self.sns.add_response("publish", {"MessageId": "m-1"})
        rotation.lambda_handler(self.event("finishSecret"), Ctx())
        self.assertIn("AccessDeniedException", self.log.getvalue())

    def test_retry_after_promotion_skips_move_and_refreshes(self):
        current = describe({OLD: ["AWSPREVIOUS"], NEW: ["AWSCURRENT"]})
        self.sm.add_response("describe_secret", current)  # handler
        self.sm.add_response("describe_secret", current)  # finish: already promoted, no update call
        self.sm.add_response("describe_secret", current)  # verify
        self.sm.add_response("get_secret_value", gsv(NEW, NEW_VALUES))
        self._ssm_command("Success")
        rotation.lambda_handler(self.event("finishSecret"), Ctx())
        self.sm.assert_no_pending_responses()
        self.assertIn("ALREADY_PROMOTED", self.log.getvalue())

    def test_already_current_non_finish_step_is_noop(self):
        self.sm.add_response("describe_secret", describe({NEW: ["AWSCURRENT"]}))
        rotation.lambda_handler(self.event("createSecret"), Ctx())
        self.sm.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
