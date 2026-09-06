"""The publishing ZIP must work through the real downloader and update helpers."""
import importlib.util
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location('update_pipeline_smoke', Path(__file__).resolve().parents[1] / 'tools/test_update.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


@pytest.mark.parametrize('mode', ('direct', 'bad-checksum', 'rollback', 'native'))
def test_produced_delta_through_client_pipeline(mode):
    if mode == 'native' and sys.platform != 'win32':
        pytest.skip('Native update helper requires Windows')
    pipeline.run_mode(mode)
