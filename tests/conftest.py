import pytest

from butler.config import settings


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    """Tests must not call the real Gemini API (now that .env has a key)."""
    monkeypatch.setattr(settings(), "gemini_api_key", "")
