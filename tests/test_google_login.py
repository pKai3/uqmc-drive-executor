import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import METADATA_SCOPE, READONLY_SCOPE, FULL_DRIVE_SCOPE
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

    def test_explicit_full_drive_login_uses_separate_private_token(self):
        self.token.parent.mkdir(mode=0o700)
        self.token.write_text("existing metadata fixture")
        full_token = self.token.with_name("token-drive.json")
        result = login(self.client, full_token, full_drive=True, flow_factory=self.factory)
        self.assertEqual(self.calls[0]["scopes"], [FULL_DRIVE_SCOPE])
        self.assertEqual(result["scope"], FULL_DRIVE_SCOPE)
        self.assertFalse(result["migration_started"])
        self.assertEqual(json.loads(full_token.read_text())["scopes"], [FULL_DRIVE_SCOPE])
        self.assertEqual(stat.S_IMODE(full_token.stat().st_mode), 0o600)
        self.assertEqual(self.token.read_text(), "existing metadata fixture")
        count = len(self.calls)
        with self.assertRaisesRegex(SafetyError, "TOKEN_ALREADY_EXISTS"):
            login(self.client, full_token, full_drive=True, flow_factory=self.factory)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.token.read_text(), "existing metadata fixture")

    def test_conflicting_full_drive_and_readonly_never_start_consent(self):
        with self.assertRaises(SafetyError):
            login(self.client, self.token, readonly=True, full_drive=True, flow_factory=self.factory)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.token.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(["--client-json", "unused", "--token", "unused", "--full-drive", "--readonly"])
        self.assertEqual(result.exception.code, 2)

    def test_full_drive_login_rejects_different_or_extra_grants(self):
        for grants in ([METADATA_SCOPE], [FULL_DRIVE_SCOPE, METADATA_SCOPE],
                       [FULL_DRIVE_SCOPE, "https://www.googleapis.com/auth/spreadsheets"]):
            with self.subTest(grants=grants):
                def factory(*args, **kwargs):
                    class Credentials:
                        granted_scopes = grants
                        refresh_token = "fixture-refresh"
                    class Flow:
                        def run_local_server(self, **options):
                            return Credentials()
                    return Flow()
                with self.assertRaisesRegex(SafetyError, "EXACT_REQUESTED_SCOPE"):
                    login(self.client, self.token, full_drive=True, flow_factory=factory)
                self.assertFalse(self.token.exists())

    def test_full_drive_cli_discloses_content_access_and_does_not_migrate(self):
        output = io.StringIO()
        with patch("uqmc_recovery.google_login.login", return_value={"migration_started": False}) as call:
            with contextlib.redirect_stdout(output):
                result = main(["--client-json", "client.json", "--token", "secrets/token-drive.json", "--full-drive"])
        self.assertEqual(result, 0)
        call.assert_called_once_with("client.json", "secrets/token-drive.json", readonly=False, full_drive=True)
        self.assertIn("content", output.getvalue().lower())
        self.assertIn("does not run", output.getvalue())

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

    def test_output_rejects_unignored_git_path_and_public_directory(self):
        (self.root / ".git").mkdir()
        with self.assertRaisesRegex(SafetyError, "GIT_IGNORED_AND_UNTRACKED"):
            login(self.client, self.token, flow_factory=self.factory)
        (self.root / ".git").rmdir()
        self.token.parent.mkdir(mode=0o755)
        self.token.parent.chmod(0o755)
        with self.assertRaisesRegex(SafetyError, "0700"):
            login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(self.calls, [])

    def test_ignored_untracked_token_inside_checkout_is_private(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("/protected/\n")
        login(self.client, self.token, flow_factory=self.factory)
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        self.assertEqual(subprocess.run(["git", "check-ignore", "--quiet", str(self.token)], cwd=self.root).returncode, 0)

    def test_previously_tracked_token_is_refused_even_if_ignore_rule_exists(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("/protected/\n")
        self.token.parent.mkdir(mode=0o700)
        self.token.write_text("synthetic fixture only")
        subprocess.run(["git", "add", "-f", str(self.token)], cwd=self.root, check=True)
        self.token.unlink()
        with self.assertRaisesRegex(SafetyError, "GIT_IGNORED_AND_UNTRACKED"):
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
                with self.assertRaisesRegex(SafetyError, "EXACT_REQUESTED_SCOPE"):
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
        call.assert_called_once_with("client.json", "protected/token.json", readonly=True, full_drive=False)
        self.assertIn("does not run", output.getvalue())


if __name__ == "__main__":
    unittest.main()
