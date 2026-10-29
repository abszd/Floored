import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from floored import config, db, http

    s = config.Settings(data_dir=tmp_path / "data", converter="legacy", crawl_delay_s=0.0, workers=1)
    config.override_settings(s)
    db.reset_engine()
    db.init_db()
    yield s
    http.set_transport(None)
    db.reset_engine()
