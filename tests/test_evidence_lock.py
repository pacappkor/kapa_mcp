# SPDX-License-Identifier: GPL-3.0-only
"""Force first-use lock interleavings without relying on scheduler luck."""
import os
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

from kapa_mcp.config import Settings
from kapa_mcp.readers import Catalog
from kapa_mcp.workflow import WorkflowManager


def test_empty_lock_initialization_is_serialized(tmp_path):
    settings = Settings(workspace=tmp_path, jobs_dir=tmp_path / 'jobs')
    owner, contender = [WorkflowManager(settings, Catalog(settings)) for _ in range(2)]
    observed_empty = threading.Event()
    owner_locked = threading.Event()
    contender_waiting = threading.Event()
    entered = threading.Event()
    real_fstat = os.fstat
    real_sleep = time.sleep

    def delayed_fstat(descriptor):
        info = real_fstat(descriptor)
        if threading.current_thread().name.startswith('contender') and not observed_empty.is_set():
            assert info.st_size == 0
            observed_empty.set()
            assert owner_locked.wait(10)
        return info

    def observe_retry(seconds):
        contender_waiting.set()
        real_sleep(seconds)

    def acquire():
        with contender._storage_lock():
            entered.set()

    with patch('kapa_mcp.workflow.os.fstat', side_effect=delayed_fstat), \
            patch('kapa_mcp.workflow.time.sleep', side_effect=observe_retry):
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='contender') as pool:
            future = pool.submit(acquire)
            try:
                assert observed_empty.wait(10)
                with owner._storage_lock():
                    owner_locked.set()
                    # Wait for an actual OS-lock contention, not a guessed delay.
                    assert contender_waiting.wait(10)
                    assert not entered.is_set()
            finally:
                owner_locked.set()
            future.result(timeout=10)
    assert entered.is_set()
    assert (owner.root / '.lock').read_bytes() == b'0'
