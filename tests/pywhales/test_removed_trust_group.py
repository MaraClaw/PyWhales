import importlib

import pytest

from pywhales import DockerClient


def test_client_has_no_trust_group() -> None:
    assert not hasattr(DockerClient(), "trust")


def test_trust_component_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pywhales.components.trust")
