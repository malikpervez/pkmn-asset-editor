import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthrom  # noqa: E402


@pytest.fixture(scope="session")
def synth():
    return synthrom.build()


@pytest.fixture()
def rom_file(tmp_path, synth):
    data, _ = synth
    p = tmp_path / "test.gba"
    p.write_bytes(data)
    return p
