# SPDX-License-Identifier: GPL-3.0-only
"""Installer tests use only local bare repositories, with no network calls."""
import json
import subprocess

import pytest

from kapa_mcp.setup import SetupError, load_pins, setup_workspace


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', f'safe.directory={root.as_posix()}', '-C', str(root), *args],
        text=True).strip()


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'no-config'))
    for key in ('GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME'):
        monkeypatch.setenv(key, 'Synthetic fixture')
    for key in ('GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.setenv(key, 'fixture@example.invalid')
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'document.md').write_text('first\n', encoding='utf-8')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'fixture one')
    first = git(source, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'clone', '--bare', str(source), str(remote))
    return source, remote, {'kapa_edu_1': {'url': str(remote), 'commit': first}}


def test_manifest():
    assert len(load_pins()) == 4


def test_clone_idempotent_update(tmp_path, upstream):
    source, remote, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    assert git(target, 'rev-parse', 'HEAD') == pins['kapa_edu_1']['commit']
    setup_workspace(workspace, pins)
    (source / 'document.md').write_text('second\n', encoding='utf-8')
    git(source, 'commit', '-am', 'fixture two')
    git(source, 'push', str(remote), 'HEAD:refs/heads/main')
    pins['kapa_edu_1']['commit'] = git(source, 'rev-parse', 'HEAD')
    with pytest.raises(SetupError, match='HEAD differs'):
        setup_workspace(workspace, pins)
    setup_workspace(workspace, pins, update=True)
    assert git(target, 'rev-parse', 'HEAD') == pins['kapa_edu_1']['commit']
    assert (target / 'document.md').read_text() == 'second\n'


@pytest.mark.parametrize('update', [False, True])
def test_matching_pin_valid_rerun_is_read_only(tmp_path, upstream, monkeypatch, update):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    marker = target / '.git/kapa-setup.json'
    before = marker.read_bytes(), marker.stat().st_mtime_ns
    assert json.loads(before[0]) == pins['kapa_edu_1']
    assert git(target, 'branch', '--show-current') == ''
    from kapa_mcp import setup
    original_git = setup._git

    def read_only_git(root, *args):
        assert args[0] not in {'clone', 'fetch', 'checkout'}
        return original_git(root, *args)

    monkeypatch.setattr(setup, '_git', read_only_git)
    assert setup_workspace(workspace, pins, update=update) == workspace
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before


@pytest.mark.parametrize('update', [False, True])
@pytest.mark.parametrize('kind', ['branch', 'missing', 'corrupt', 'wrong-marker'])
def test_matching_pin_requires_managed_detached_state(tmp_path, upstream, update, kind):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    marker = target / '.git/kapa-setup.json'
    if kind == 'branch':
        git(target, 'switch', '-c', 'local-work')
    elif kind == 'missing':
        marker.unlink()
    elif kind == 'corrupt':
        marker.write_text('{broken', encoding='utf-8')
    else:
        marker.write_text(json.dumps({'url': pins['kapa_edu_1']['url'], 'commit': '0' * 40}),
                          encoding='utf-8')
    before = marker.read_bytes() if marker.exists() else None
    message = 'modified HEAD or branch' if kind in {'branch', 'wrong-marker'} else 'missing or invalid installer marker'
    with pytest.raises(SetupError, match=message):
        setup_workspace(workspace, pins, update=update)
    assert git(target, 'rev-parse', 'HEAD') == pins['kapa_edu_1']['commit']
    assert (marker.read_bytes() if marker.exists() else None) == before
    assert git(target, 'branch', '--show-current') == ('local-work' if kind == 'branch' else '')


@pytest.mark.parametrize('kind', ['tracked', 'untracked', 'ignored', 'hidden', 'skip-worktree'])
def test_refuse_changes(tmp_path, upstream, kind):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    if kind in ('hidden', 'skip-worktree'):
        flag = '--assume-unchanged' if kind == 'hidden' else '--skip-worktree'
        git(target, 'update-index', flag, 'document.md')
    if kind in ('tracked', 'hidden', 'skip-worktree'):
        (target / 'document.md').write_text('local change', encoding='utf-8')
    else:
        if kind == 'ignored':
            (target / '.git/info/exclude').write_text('local.md\n', encoding='utf-8')
        (target / 'local.md').write_text('keep me', encoding='utf-8')
    with pytest.raises(SetupError):
        setup_workspace(workspace, pins, update=True)
    changed = target / ('document.md' if kind in ('tracked', 'hidden', 'skip-worktree') else 'local.md')
    assert changed.read_text() == ('local change' if kind in ('tracked', 'hidden', 'skip-worktree') else 'keep me')


def test_refuse_nonempty_and_wrong_origin(tmp_path, upstream):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    target = workspace / 'kapa_edu_1'
    target.mkdir(parents=True)
    (target / 'keep.md').write_text('keep', encoding='utf-8')
    with pytest.raises(SetupError, match='non-empty'):
        setup_workspace(workspace, pins)
    (target / 'keep.md').unlink()
    setup_workspace(workspace, pins)
    git(target, 'remote', 'set-url', 'origin', str(tmp_path / 'other.git'))
    with pytest.raises(SetupError, match='origin'):
        setup_workspace(workspace, pins, update=True)


def test_unmanaged_head_refused(tmp_path, upstream):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    git(target, 'commit', '--allow-empty', '-m', 'local commit')
    with pytest.raises(SetupError, match='modified HEAD'):
        setup_workspace(workspace, pins, update=True)


def test_refuse_symlink_workspace(tmp_path, upstream):
    _, _, pins = upstream
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(SetupError, match='Links'):
        setup_workspace(link, pins)
    assert not list(real.iterdir())


@pytest.mark.parametrize('location', ['marker', 'metadata'])
def test_matching_pin_refuses_links(tmp_path, upstream, location):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    marker = target / '.git/kapa-setup.json'
    real = tmp_path / 'preserved-marker.json'
    real.write_bytes(marker.read_bytes())
    link = marker if location == 'marker' else target / '.git/linked-metadata'
    if location == 'marker':
        marker.unlink()
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(SetupError, match='Links'):
        setup_workspace(workspace, pins)
    assert real.read_bytes() == (json.dumps(pins['kapa_edu_1']) + '\n').encode()


def test_failed_marker_write_is_not_adopted_on_retry(tmp_path, upstream, monkeypatch):
    from pathlib import Path

    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    original_write = Path.write_text

    def fail_marker_write(path, *args, **kwargs):
        if path.name == 'kapa-setup.json':
            raise OSError('simulated marker write failure')
        return original_write(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'write_text', fail_marker_write)
        with pytest.raises(OSError, match='simulated'):
            setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    assert git(target, 'rev-parse', 'HEAD') == pins['kapa_edu_1']['commit']
    assert git(target, 'branch', '--show-current') == ''
    with pytest.raises(SetupError, match='missing or invalid installer marker'):
        setup_workspace(workspace, pins)
    assert not (target / '.git/kapa-setup.json').exists()


def test_preflight_rejects_before_cloning(tmp_path, upstream):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    target = workspace / 'kapa_edu_2'
    target.mkdir(parents=True)
    (target / 'keep.md').write_text('keep', encoding='utf-8')
    pins['kapa_edu_2'] = dict(pins['kapa_edu_1'])
    with pytest.raises(SetupError):
        setup_workspace(workspace, pins)
    assert not (workspace / 'kapa_edu_1').exists()


def test_invalid_pin_and_failed_clone_preserve_files(tmp_path, upstream):
    _, _, pins = upstream
    pins['kapa_edu_1']['commit'] = '0' * 40
    with pytest.raises(SetupError):
        setup_workspace(tmp_path / 'workspace', pins)
    assert (tmp_path / 'workspace/kapa_edu_1/.git').is_dir()
    pins['kapa_edu_1']['commit'] = '--bad'
    with pytest.raises(SetupError, match='Invalid'):
        setup_workspace(tmp_path / 'other', pins)
