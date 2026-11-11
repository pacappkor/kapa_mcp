# SPDX-License-Identifier: GPL-3.0-only
"""Installer tests use only local bare repositories, with no network calls."""
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


@pytest.mark.parametrize('kind', ['tracked', 'untracked', 'ignored', 'hidden'])
def test_refuse_changes(tmp_path, upstream, kind):
    _, _, pins = upstream
    workspace = tmp_path / 'workspace'
    setup_workspace(workspace, pins)
    target = workspace / 'kapa_edu_1'
    if kind == 'hidden':
        git(target, 'update-index', '--assume-unchanged', 'document.md')
    if kind in ('tracked', 'hidden'):
        (target / 'document.md').write_text('local change', encoding='utf-8')
    else:
        if kind == 'ignored':
            (target / '.git/info/exclude').write_text('local.md\n', encoding='utf-8')
        (target / 'local.md').write_text('keep me', encoding='utf-8')
    with pytest.raises(SetupError):
        setup_workspace(workspace, pins, update=True)
    changed = target / ('document.md' if kind in ('tracked', 'hidden') else 'local.md')
    assert changed.read_text() == ('local change' if kind in ('tracked', 'hidden') else 'keep me')


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
