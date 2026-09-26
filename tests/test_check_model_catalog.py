"""Tests for the Claude model-catalog drift checker.

All offline: the network fetch is monkeypatched, so this suite exercises the
parsing and diffing logic that actually decides pass/fail. The live docs run
belongs to the weekly workflow, not to CI's main suite.
"""

import importlib.util
import io
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_model_catalog.py"


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("check_model_catalog_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Mirrors the shapes that actually appear on the docs page: a clean alias row, a
# Bedrock row with a provider prefix and a footnote digit glued on, a Google
# Cloud row using @, and a heading anchor that concatenates two names.
_PAGE = """
# Models overview

See [Claude Fable 5](#claude-fable-5-and-claude-mythos-5) for launch details.

| **Claude API ID**    | claude-opus-5 | claude-sonnet-5 | claude-haiku-4-5-20251001 |
| **Claude API alias** | claude-opus-5 | claude-sonnet-5 | claude-haiku-4-5 |
| **AWS Bedrock ID**   | anthropic.claude-opus-53 | anthropic.claude-fable-53 | x |
| **Google Cloud ID**  | claude-opus-5 | claude-sonnet-5 | claude-haiku-4-5\\@20251001 |

Legacy: claude-opus-4-8, claude-opus-4-7, claude-sonnet-4-6, claude-mythos-preview.
"""


def _patch_fetch(checker, monkeypatch, body: str):
    monkeypatch.setattr(
        checker.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(body.encode("utf-8")),
    )


@pytest.mark.unit
class TestParsing:
    def test_extracts_first_party_ids(self, checker, monkeypatch):
        _patch_fetch(checker, monkeypatch, _PAGE)
        assert checker.fetch_live_model_ids() == {
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-mythos-preview",
        }

    def test_bedrock_prefixed_and_anchor_forms_are_rejected(self, checker, monkeypatch):
        """`anthropic.claude-fable-53` and `#claude-fable-5-and-...` are not IDs."""
        _patch_fetch(checker, monkeypatch, _PAGE)
        live = checker.fetch_live_model_ids()
        assert not any("fable" in model for model in live)
        assert not any(model.endswith("3") and "opus-5" in model for model in live)

    def test_empty_parse_raises_rather_than_reporting_everything_stale(
        self, checker, monkeypatch
    ):
        """A layout change must fail loudly, not flag the whole catalog."""
        _patch_fetch(checker, monkeypatch, "# Models overview\n\nNothing here.\n")
        with pytest.raises(RuntimeError, match="layout probably changed"):
            checker.fetch_live_model_ids()


@pytest.mark.unit
class TestCatalogDiff:
    def test_catalog_ids_cover_both_claude_providers(self, checker):
        by_provider = checker.catalog_model_ids()
        # The switchboard menus carry only CLI family aliases (no claude-* IDs),
        # so only the direct Anthropic provider has IDs to check for drift.
        assert set(by_provider) == {"anthropic"}
        assert all(
            model.startswith("claude-")
            for models in by_provider.values()
            for model in models
        )

    def test_non_claude_switchboard_entries_are_ignored(self, checker):
        """`llama3` and `custom` ride the same menu but aren't Anthropic models."""
        assert "llama3" not in checker.catalog_model_ids().get("switchboard", set())
        assert "custom" not in checker.catalog_model_ids().get("switchboard", set())

    @pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku", "fable"])
    def test_cli_family_aliases_are_not_checked_as_ids(self, checker, alias):
        """They resolve at call time and never appear in Anthropic's ID list,
        so treating them as IDs would report permanent false drift."""
        assert alias not in checker.catalog_model_ids().get("switchboard", set())

    def test_clean_when_docs_list_everything_we_offer(self, checker, monkeypatch):
        ours = {m for models in checker.catalog_model_ids().values() for m in models}
        monkeypatch.setattr(checker, "fetch_live_model_ids", lambda *a, **k: ours)
        monkeypatch.setattr("sys.argv", ["check_model_catalog.py"])
        assert checker.main() == 0

    def test_retired_model_in_catalog_fails(self, checker, monkeypatch):
        ours = {m for models in checker.catalog_model_ids().values() for m in models}
        monkeypatch.setattr(
            checker, "fetch_live_model_ids", lambda *a, **k: ours - {"claude-opus-5"}
        )
        monkeypatch.setattr("sys.argv", ["check_model_catalog.py"])
        assert checker.main() == 1

    def test_new_model_is_informational_unless_strict(self, checker, monkeypatch):
        ours = {m for models in checker.catalog_model_ids().values() for m in models}
        monkeypatch.setattr(
            checker, "fetch_live_model_ids", lambda *a, **k: ours | {"claude-opus-6"}
        )

        monkeypatch.setattr("sys.argv", ["check_model_catalog.py"])
        assert checker.main() == 0

        monkeypatch.setattr("sys.argv", ["check_model_catalog.py", "--strict"])
        assert checker.main() == 1

    def test_deliberately_excluded_models_stay_quiet(self, checker, monkeypatch):
        """Fable/Mythos are released but declined on purpose — no weekly noise."""
        ours = {m for models in checker.catalog_model_ids().values() for m in models}
        monkeypatch.setattr(
            checker, "fetch_live_model_ids", lambda *a, **k: ours | {"claude-fable-5"}
        )
        monkeypatch.setattr("sys.argv", ["check_model_catalog.py", "--strict"])
        assert checker.main() == 0

    def test_fetch_failure_exits_two_not_one(self, checker, monkeypatch):
        """Exit 2 keeps a network blip distinguishable from real drift."""
        def boom(*a, **k):
            raise checker.urllib.error.URLError("no route to host")

        monkeypatch.setattr(checker, "fetch_live_model_ids", boom)
        monkeypatch.setattr("sys.argv", ["check_model_catalog.py"])
        assert checker.main() == 2
