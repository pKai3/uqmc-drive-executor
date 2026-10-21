"""Explicit local Google Desktop OAuth login; never starts a migration.

Reference: https://google-auth-oauthlib.readthedocs.io/en/latest/reference/google_auth_oauthlib.flow.html
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
import tempfile

from .direct_transport import METADATA_SCOPE, READONLY_SCOPE
from .model import SafetyError


def _client_config(path):
    try:
        data = json.loads(Path(path).read_text())
        client = data["installed"]
        if "web" in data or not all(isinstance(client.get(k), str) and client[k]
                                    for k in ("client_id", "client_secret")):
            raise ValueError()
        if client.get("auth_uri") not in {
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        } or client.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SafetyError("GOOGLE_DESKTOP_CLIENT_JSON_REQUIRED") from None
    return data


def _token_path(value):
    supplied = Path(value).expanduser().absolute()
    if supplied.exists() or supplied.is_symlink():
        raise SafetyError("TOKEN_ALREADY_EXISTS_CHOOSE_NEW_PATH")
    path = supplied.resolve()
    repository = next((p for p in (path.parent, *path.parent.parents) if (p / ".git").exists()), None)
    if repository:
        relative = str(path.relative_to(repository))
        try:
            ignored = subprocess.run(["git", "check-ignore", "--quiet", "--", relative],
                cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", relative],
                cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 1
        except OSError:
            raise SafetyError("TOKEN_MUST_BE_GIT_IGNORED_AND_UNTRACKED") from None
        if not ignored or tracked:
            raise SafetyError("TOKEN_MUST_BE_GIT_IGNORED_AND_UNTRACKED")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise SafetyError("TOKEN_DIRECTORY_REQUIRES_0700_PERMISSIONS")
    return path


def _save_exclusive(path, content):
    """Publish one complete 0600 token atomically, refusing an existing name."""
    fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise SafetyError("TOKEN_ALREADY_EXISTS_CHOOSE_NEW_PATH") from None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def login(client_json, token, *, readonly=False, flow_factory=None):
    """Called only by an explicit operator command; tests inject a fake flow."""
    config = _client_config(client_json)
    path = _token_path(token)
    scope = READONLY_SCOPE if readonly else METADATA_SCOPE
    if flow_factory is None:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            raise SafetyError("INSTALL_DIRECT_OPTIONAL_DEPENDENCIES") from None
        flow_factory = InstalledAppFlow.from_client_config
    try:
        flow = flow_factory(config, scopes=[scope], autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host="localhost", port=0, open_browser=True, timeout_seconds=300,
            authorization_prompt_message="Select the intended Google account in the browser. No migration will start.",
            success_message="Google login completed. You can close this window. No migration has started.",
            prompt="consent select_account", access_type="offline", include_granted_scopes="false")
        granted = set(credentials.granted_scopes or credentials.scopes or ())
        if granted != {scope} or not credentials.refresh_token:
            raise SafetyError("EXACT_METADATA_SCOPE_AND_REFRESH_TOKEN_REQUIRED")
        serialized = credentials.to_json()
        parsed = json.loads(serialized)
        if set(parsed.get("scopes", [])) != {scope}:
            raise SafetyError("EXACT_METADATA_SCOPE_AND_REFRESH_TOKEN_REQUIRED")
    except SafetyError:
        raise
    except Exception:
        # Provider exceptions can include authorization codes or token payloads.
        raise SafetyError("GOOGLE_LOGIN_FAILED_OR_CANCELLED") from None
    _save_exclusive(path, serialized)
    return {"token_path": str(path), "scope": scope, "migration_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-json", required=True, help="Downloaded OAuth Desktop app client JSON")
    parser.add_argument("--token", required=True, help="New private token file; inside a Git checkout it must be ignored and untracked")
    parser.add_argument("--readonly", action="store_true", help="Request Drive metadata read-only access")
    args = parser.parse_args(argv)
    print("This opens Google consent for the account you select. It does not run the migration.")
    try:
        print(json.dumps(login(args.client_json, args.token, readonly=args.readonly)))
        return 0
    except SafetyError as error:
        print(json.dumps({"error": str(error)}))
        return 2
    except Exception:
        print(json.dumps({"error": "GOOGLE_LOGIN_LOCAL_FILE_ERROR"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
