import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Keep tests away from the machine's real settings and Keychain."""
    from reviewr.config import load_config

    monkeypatch.setenv("REVIEWR_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("REVIEWR_SECRETS_FILE", str(tmp_path / "secrets.json"))
    load_config.cache_clear()
    yield
    load_config.cache_clear()
