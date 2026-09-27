import pytest

from web import credentials, db

_KEYS = {s["key"] for s in credentials.SETTINGS_REGISTRY}
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        "SCHEDULE_RESEARCH_TIME" not in _KEYS,
        reason="setting lives in a TIER:3 block — stripped below tier 3",
    ),
]


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    monkeypatch.delenv("SCHEDULE_RESEARCH_TIME", raising=False)
    db.init_db()


def test_registry_entry_properties():
    matches = [s for s in credentials.SETTINGS_REGISTRY if s["key"] == "SCHEDULE_RESEARCH_TIME"]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["secret"] is False
    assert entry["group"] == "Automation Schedule"
    assert entry["type"] == "text"


def test_mask_setting_non_secret_is_verbatim():
    assert credentials.mask_setting("SCHEDULE_RESEARCH_TIME", "01:15") == "01:15"


def test_list_settings_meta_shows_db_value_verbatim():
    db.set_app_setting("SCHEDULE_RESEARCH_TIME", "01:15")
    entry = next(
        s for s in credentials.list_settings_meta()["registry"] if s["key"] == "SCHEDULE_RESEARCH_TIME"
    )
    assert entry["has_value"] is True
    assert entry["masked"] == "01:15"


def test_entry_sits_inside_tier3_block_after_tier2_block():
    src = (credentials.__file__).replace(".pyc", ".py")
    text = open(src).read()
    key_pos = text.index('"SCHEDULE_RESEARCH_TIME"')
    begin = text.rindex("# TIER:3 BEGIN", 0, key_pos)
    end = text.index("# TIER:3 END", key_pos)
    tier2_end = text.index("# TIER:2 END")
    assert tier2_end < begin < key_pos < end
    # Not nested: no other marker sits between this block's BEGIN and END.
    assert "# TIER:" not in text[begin + len("# TIER:3 BEGIN"):end]


@pytest.mark.parametrize("key,value,ok", [
    ("SCHEDULE_RESEARCH_TIME", "00:00", True),
    ("SCHEDULE_RESEARCH_TIME", "05:29", True),
    ("SCHEDULE_RESEARCH_TIME", "05:30", False),
    ("SCHEDULE_RESEARCH_TIME", "22:00", False),
    ("SCHEDULE_RESEARCH_TIME", "midnight", False),
    ("SCHEDULE_NIGHTLY_SCAN_TIME", "22:00", True),
    ("SCHEDULE_NIGHTLY_SCAN_TIME", "25:00", False),
    ("OLLAMA_BASE_URL", "anything goes", True),
])
def test_schedule_settings_are_validated(key, value, ok):
    from web import credentials as creds
    assert (creds.validate_setting_value(key, value) is None) is ok
