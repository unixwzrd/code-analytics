from unittest.mock import patch

import pytest

import sample.core as core
from sample.core import calculate


@pytest.mark.parametrize("value,expected", [(1, 4)])
def test_calculate(client, monkeypatch, value, expected):
    monkeypatch.setattr(core, "helper", lambda item: item + 1)
    with patch("sample.core.helper"):
        assert calculate(value) == expected


def test_qualification_path(client):
    assert client["enabled"]


def test_dynamic_patch(target_name):
    with patch(target_name):
        pass
