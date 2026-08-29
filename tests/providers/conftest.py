import pytest


@pytest.fixture(autouse=True)
def _clean_tables():
    """Provider unit tests do not require the root PostgreSQL cleanup fixture."""
    yield
