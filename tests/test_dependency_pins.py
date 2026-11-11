# SPDX-License-Identifier: GPL-3.0-only
"""Offline pin contract; fixture copied from knowledge a023555fd288718a84e592bf463d407b4dd52cfa."""
import json
from pathlib import Path

import pytest

from kapa_mcp.setup import SetupError, load_pins, validate_dependency_pins


def test_packaged_pins_match_knowledge_dependency():
    text = Path(__file__).with_name('fixtures').joinpath('knowledge-deps.lock').read_text('utf-8')
    validate_dependency_pins(load_pins(), text)


@pytest.mark.parametrize('kind', ['skills', 'vendor', 'worktree', 'missing', 'malformed'])
def test_inconsistent_dependency_pins_rejected(kind):
    pins = load_pins()
    commit = pins['kapa_skills']['commit']
    lock = {'kapa_skills': {'commit': commit, 'vendor': {'commit': commit}}}
    if kind == 'skills':
        pins['kapa_skills']['commit'] = '0' * 40
    elif kind == 'vendor':
        lock['kapa_skills']['vendor']['commit'] = '0' * 40
    elif kind == 'worktree':
        lock['kapa_skills']['commit'] = 'WORKTREE'
    elif kind == 'missing':
        lock = {}
    text = 'not JSON' if kind == 'malformed' else json.dumps(lock)
    with pytest.raises(SetupError, match='deps.lock must match'):
        validate_dependency_pins(pins, text)
