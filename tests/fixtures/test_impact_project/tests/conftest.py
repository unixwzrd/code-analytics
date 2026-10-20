import pytest


@pytest.fixture(scope="session")
def configuration():
    return {"enabled": True}


@pytest.fixture
def client(configuration):
    return configuration
