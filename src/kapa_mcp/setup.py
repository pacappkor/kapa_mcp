# SPDX-License-Identifier: GPL-3.0-only
"""Explicit installer for dedicated upstream snapshots; never run by the server."""

import argparse
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

from .config import REPOSITORIES


class SetupError(ValueError):
    """Fail closed without deleting or resetting a user's files."""


def _plain_path(path):
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise SetupError('Links and reparse points are not supported')


def _git(root, *args):
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT'}}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH='1')
    result = subprocess.run(
        ['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
         '-c', 'core.fsmonitor=false', '-c', 'core.autocrlf=false',
         '-C', str(root), *args], env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=300, check=False)
    if result.returncode:
        raise SetupError('Git operation failed; check permissions, git installation and remote availability')
    return result.stdout.strip()


def load_pins():
    pins = json.loads(files('kapa_mcp').joinpath('upstream_pins.json').read_text('utf-8'))['repositories']
    if set(pins) != set(REPOSITORIES):
        raise SetupError('Invalid repository manifest')
    for name, pin in pins.items():
        if pin['url'] != f'https://github.com/pacappkor/{name}' or not re.fullmatch(r'[0-9a-f]{40}', pin['commit']):
            raise SetupError('Invalid upstream pin')
    return pins


def _check_existing(target, pin, update):
    _plain_path(target / '.git')
    if not (target / '.git').is_dir():
        raise SetupError(f'{target.name}: non-empty target is not a standalone checkout')
    if Path(_git(target, 'rev-parse', '--show-toplevel')).resolve() != target.resolve():
        raise SetupError('Unexpected checkout root')
    if _git(target, 'remote', 'get-url', 'origin') != pin['url']:
        raise SetupError(f'{target.name}: origin does not match')
    # Include ignored files and reject index flags that could hide local changes.
    if _git(target, 'status', '--porcelain', '--untracked-files=all', '--ignored'):
        raise SetupError(f'{target.name}: local changes or ignored files; refusing to overwrite')
    if any(not line.startswith('H ') for line in _git(target, 'ls-files', '-v').splitlines()):
        raise SetupError(f'{target.name}: hidden index entries are unsupported')
    head = _git(target, 'rev-parse', 'HEAD')
    if head == pin['commit']:
        return False
    if not update:
        raise SetupError(f'{target.name}: HEAD differs; use --update only for managed snapshots')
    marker = target / '.git' / 'kapa-setup.json'
    _plain_path(marker)
    try:
        previous = json.loads(marker.read_text('utf-8'))
    except (OSError, ValueError):
        raise SetupError(f'{target.name}: not a managed snapshot; use a new workspace') from None
    if previous != {'url': pin['url'], 'commit': head} or _git(target, 'branch', '--show-current'):
        raise SetupError(f'{target.name}: modified HEAD or branch; use a new workspace')
    return True


def setup_workspace(workspace, pins, *, update=False):
    """Pins injected by tests may refer to local bare remotes, never the network."""
    workspace = Path(os.path.abspath(workspace))
    _plain_path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    pending = []
    for name, pin in pins.items():
        if name not in REPOSITORIES or not re.fullmatch(r'[0-9a-f]{40}', pin['commit']):
            raise SetupError('Invalid upstream pin')
        target = workspace / name
        _plain_path(target)
        exists = target.exists() and (not target.is_dir() or any(target.iterdir()))
        move = _check_existing(target, pin, update) if exists else True
        pending.append((target, pin, exists, move))
    for target, pin, exists, move in pending:
        if not move:
            continue
        if exists:
            # Recheck immediately before modification. Operators must stop servers
            # and other writers; this is not a hostile concurrent-writer sandbox.
            _check_existing(target, pin, update)
            _git(target, 'fetch', '--no-tags', '--no-recurse-submodules', 'origin', pin['commit'])
        else:
            _git(workspace, 'clone', '--no-checkout', '--no-recurse-submodules', '--', pin['url'], str(target))
        _git(target, 'checkout', '--detach', pin['commit'])
        if _git(target, 'rev-parse', 'HEAD') != pin['commit']:
            raise SetupError('Checkout did not match pin')
        (target / '.git' / 'kapa-setup.json').write_text(
            json.dumps(pin) + '\n', encoding='utf-8', newline='\n')
    return workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--update', action='store_true', help='Update clean, installer-managed detached snapshots to bundled pins')
    args = parser.parse_args()
    try:
        workspace = setup_workspace(args.workspace, load_pins(), update=args.update)
    except (SetupError, OSError, subprocess.SubprocessError) as error:
        print(f'kapa-mcp-setup: {error}', file=sys.stderr)
        return 2
    print('# PowerShell')
    for name, path in [('KAPA_WORKSPACE', workspace), ('KAPA_JOBS_DIR', workspace / '_kapa_jobs')]:
        print(f"$env:{name} = '" + str(path).replace("'", "''") + "'")
    print('# macOS/Linux')
    for name, path in [('KAPA_WORKSPACE', workspace), ('KAPA_JOBS_DIR', workspace / '_kapa_jobs')]:
        print(f'export {name}={shlex.quote(str(path))}')
    print('# Snapshots installed. Preserve dependency warnings; pins do not establish legal accuracy.')
    return 0
