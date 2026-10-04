import pytest

from tests.helpers import make_cfg


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)
