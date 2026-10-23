"""Bounded guest SMB mount and exact mount-table verification; no stored credentials."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, unquote, quote


def canonical_smb_url(value, *, allow_userinfo=False):
    try:
        parsed = urlsplit('smb:' + value if value.startswith('//') else value)
        if (parsed.scheme != 'smb' or not parsed.hostname or parsed.query or parsed.fragment
                or (not allow_userinfo and (parsed.username is not None or parsed.password is not None))):
            raise ValueError()
        share = unquote(parsed.path).strip('/')
        if not share or '/' in share or any(ord(c) < 32 for c in share): raise ValueError()
        host = parsed.hostname.lower()
        if ':' in host: host = '[' + host + ']'
        if parsed.port is not None: host += ':' + str(parsed.port)
        return 'smb://' + host + '/' + quote(share, safe='')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('SERVER_SYNC_SMB_URL_INVALID') from None


def mount_entries():
    """Never return or log credential-bearing raw mount source strings."""
    try:
        result = subprocess.run(['/sbin/mount', '-t', 'smbfs'], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('SERVER_SYNC_MOUNT_TABLE_UNAVAILABLE') from None
    if result.returncode: raise ValueError('SERVER_SYNC_MOUNT_TABLE_UNAVAILABLE')
    entries = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'(.+) on (.+) \(smbfs(?:,.*)?\)', line)
        if match:
            entries.append({'mount_point': match[2],
                            'smb_url': canonical_smb_url(match[1], allow_userinfo=True)})
    return entries


def mount_guest(url):
    # The OS StandardAdditions dictionary documents omitted user/password as
    # guest access. Pass URL as argv, never interpolate it into AppleScript.
    script = ('on run argv\nwith timeout of 15 seconds\n'
              'mount volume (item 1 of argv)\nend timeout\nend run')
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script, url],
                                capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError('SERVER_SYNC_MOUNT_TIMEOUT') from None
    except OSError:
        raise ValueError('SERVER_SYNC_MOUNT_COMMAND_UNAVAILABLE') from None
    if result.returncode:
        raise ValueError('SERVER_SYNC_GUEST_MOUNT_FAILED_LOGIN_MAY_BE_REQUIRED')


def ensure_mount(config, *, apply=False, reader=None, mounter=None):
    """Mount once only when explicitly configured, then verify exact server/share.

    A local destination outside /Volumes remains supported. Network destinations
    under /Volumes require an explicit identity and cannot fall back to local disk.
    Read-only checks never mount. This function does not create directories.
    """
    destination = Path(config['destination_directory'])
    mount_point = config.get('mount_point')
    if mount_point is None:
        if destination.is_relative_to('/Volumes'):
            raise ValueError('SERVER_SYNC_SMB_IDENTITY_NOT_CONFIGURED')
        return None
    point = Path(mount_point)
    if (not point.is_absolute() or not destination.is_absolute() or destination == point
            or not destination.is_relative_to(point) or point.is_symlink()):
        raise ValueError('SERVER_SYNC_MOUNT_PATH_INVALID')
    endpoint = canonical_smb_url(config.get('smb_url'))
    reader, mounter = reader or mount_entries, mounter or mount_guest

    def observed():
        matches = [e for e in reader() if e['mount_point'] == str(point)]
        if len(matches) > 1: raise ValueError('SERVER_SYNC_MOUNT_IDENTITY_CONFLICT')
        if matches and matches[0]['smb_url'] != endpoint:
            raise ValueError('SERVER_SYNC_MOUNT_IDENTITY_CONFLICT')
        return bool(matches)

    present = observed()
    if not present and apply and config.get('auto_mount') is True:
        # Do not mount over a different filesystem, symlink, or populated local directory.
        if point.is_mount(): raise ValueError('SERVER_SYNC_MOUNT_IDENTITY_CONFLICT')
        if point.exists() and (not point.is_dir() or any(point.iterdir())):
            raise ValueError('SERVER_SYNC_UNMOUNTED_LOCAL_DIRECTORY_NOT_EMPTY')
        failure = None
        try: mounter(endpoint)
        except ValueError as exc: failure = exc
        present = observed()  # A timed-out OS call may still have completed.
        if not present and failure: raise failure
    if not present: raise ValueError('SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED')
    if (not point.is_dir() or point.resolve() != point or not destination.is_dir()
            or destination.resolve() != destination or destination.stat().st_dev != point.stat().st_dev):
        raise ValueError('SERVER_SYNC_DESTINATION_NOT_ON_VERIFIED_MOUNT')
    return {'mount_point': str(point), 'smb_url': endpoint, 'verified': True}
