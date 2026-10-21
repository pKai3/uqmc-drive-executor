import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import METADATA_SCOPE, READONLY_SCOPE
from uqmc_recovery.google_login import login, main, _save_exclusive
from uqmc_recovery.model import SafetyError


class GoogleLoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "client.json"
        self.client.write_text(json.dumps({"installed": {
            "client_id": "fixture.apps.googleusercontent.com", "client_secret": "fixture-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }}))
        self.token = self.root / "protected" / "token.json"
        self.calls = []

    def factory(self, config, *, scopes, **kwargs):
        self.calls.append({"config": config, "scopes": scopes, **kwargs})
        outer = self
        class Credentials:
            granted_scopes = scopes
            refresh_token = "fixture-refresh-secret"
            def to_json(self):
                return json.dumps({"scopes": scopes, "refresh_token": self.refresh_token})
        class Flow:
            def run_local_server(self, **options):
                outer.calls.append(options)
                return Credentials()
        return Flow()

    def test_metadata_login_private_file_and_loopback_only(self):
        result = login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(result["scope"], METADATA_SCOPE)
        self.assertFalse(result["migration_started"])
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token.parent.stat().st_mode), 0o700)
        self.assertEqual(self.calls[0]["scopes"], [METADATA_SCOPE])
        self.assertTrue(self.calls[0]["autogenerate_code_verifier"])
        options = self.calls[1]
        self.assertEqual((options["host"], options["port"]), ("localhost", 0))
        self.assertEqual(options["prompt"], "consent select_account")
        self.assertEqual(options["include_granted_scopes"], "false")
        self.assertNotIn("login_hint", options)
        self.assertNotIn("{url}", options["authorization_prompt_message"])
        self.assertEqual(list(self.token.parent.iterdir()), [self.token])

    def test_readonly_scope_is_only_requested_scope(self):
        login(self.client, self.token, readonly=True, flow_factory=self.factory)
        self.assertEqual(self.calls[0]["scopes"], [READONLY_SCOPE])

    def test_existing_output_is_never_replaced_or_new_consent_started(self):
        self.token.parent.mkdir(mode=0o700)
        self.token.write_text("keep")
        with self.assertRaisesRegex(SafetyError, "TOKEN_ALREADY_EXISTS"):
            login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(self.token.read_text(), "keep")
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(SafetyError, "TOKEN_ALREADY_EXISTS"):
            _save_exclusive(self.token, "do not overwrite")
        self.assertEqual(self.token.read_text(), "keep")
        self.assertEqual(list(self.token.parent.iterdir()), [self.token])

    def test_output_rejects_git_worktree_and_public_directory(self):
        (self.root / ".git").mkdir()
        with self.assertRaisesRegex(SafetyError, "OUTSIDE_GIT"):
            login(self.client, self.token, flow_factory=self.factory)
        (self.root / ".git").rmdir()
        self.token.parent.mkdir(mode=0o755)
        self.token.parent.chmod(0o755)
        with self.assertRaisesRegex(SafetyError, "0700"):
            login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(self.calls, [])

    def test_reject_web_client_or_untrusted_auth_endpoint(self):
        config = json.loads(self.client.read_text())
        self.client.write_text(json.dumps({"web": config["installed"]}))
        with self.assertRaisesRegex(SafetyError, "DESKTOP_CLIENT"):
            login(self.client, self.token, flow_factory=self.factory)
        config["installed"]["token_uri"] = "https://example.invalid/token"
        self.client.write_text(json.dumps(config))
        with self.assertRaisesRegex(SafetyError, "DESKTOP_CLIENT"):
            login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(self.calls, [])

    def test_extra_granted_scope_or_missing_refresh_token_not_saved(self):
        for extra, refresh in [(True, "fixture"), (False, None)]:
            with self.subTest(extra=extra, refresh=bool(refresh)):
                def factory(*args, **kwargs):
                    class Credentials:
                        granted_scopes = [METADATA_SCOPE, "https://www.googleapis.com/auth/spreadsheets"] if extra else [METADATA_SCOPE]
                        refresh_token = refresh
                    class Flow:
                        def run_local_server(self, **options):
                            return Credentials()
                    return Flow()
                with self.assertRaisesRegex(SafetyError, "EXACT_METADATA_SCOPE"):
                    login(self.client, self.token, flow_factory=factory)
                self.assertFalse(self.token.exists())

    def test_error_does_not_echo_provider_secret(self):
        def factory(*args, **kwargs):
            raise ValueError("secret-token-do-not-print")
        with self.assertRaisesRegex(SafetyError, "GOOGLE_LOGIN_FAILED_OR_CANCELLED") as result:
            login(self.client, self.token, flow_factory=factory)
        self.assertNotIn("secret-token", str(result.exception))
        self.assertFalse(self.token.exists())

    def test_cli_prints_safe_result_without_credentials(self):
        output = io.StringIO()
        with patch("uqmc_recovery.google_login.login", return_value={"token_path": "protected/token.json", "migration_started": False}) as call:
            with contextlib.redirect_stdout(output):
                result = main(["--client-json", "client.json", "--token", "protected/token.json", "--readonly"])
        self.assertEqual(result, 0)
        call.assert_called_once_with("client.json", "protected/token.json", readonly=True)
        self.assertIn("does not run", output.getvalue())


if __name__ == "__main__":
    unittest.main()
