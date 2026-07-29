"""Tests for spar/models.py: display labels for model identifiers."""

from spar.models import ALIAS_GENERATIONS, display_model, split_model_id


class TestBareAliases:
    def test_claude_aliases_gain_their_generation(self):
        assert display_model("sonnet") == "sonnet 5"
        assert display_model("opus") == "opus 5"
        assert display_model("haiku") == "haiku 4.5"

    def test_alias_table_matches_the_ids_the_cli_actually_resolved(self):
        # Verified against a real run's transcripts, which recorded
        # "claude-sonnet-5" and "claude-opus-5".
        assert display_model("claude-sonnet-5") == f"sonnet {ALIAS_GENERATIONS['sonnet']}"
        assert display_model("claude-opus-5") == f"opus {ALIAS_GENERATIONS['opus']}"

    def test_unknown_alias_is_left_alone(self):
        assert display_model("brandnew") == "brandnew"


class TestNamesThatAlreadyStateTheirGeneration:
    def test_codex_ids_are_verbatim(self):
        # Rewriting gpt-5.5 -> "gpt 5.5" while gpt-5.6-sol stayed hyphenated
        # would read as an inconsistency, not a convention.
        assert display_model("gpt-5.5") == "gpt-5.5"
        assert display_model("gpt-5.6-sol") == "gpt-5.6-sol"
        assert display_model("gpt-5.4") == "gpt-5.4"

    def test_versioned_claude_alias_is_verbatim(self):
        assert display_model("sonnet-4.6") == "sonnet-4.6"


class TestEdges:
    def test_empty_and_none(self):
        assert display_model(None) == ""
        assert display_model("") == ""
        assert display_model("   ") == ""

    def test_case_insensitive_alias_lookup(self):
        assert display_model("Sonnet") == "Sonnet 5"


class TestSplitModelId:
    def test_splits_a_full_anthropic_id(self):
        assert split_model_id("claude-opus-5") == ("opus", "5")

    def test_bare_alias_has_no_generation(self):
        assert split_model_id("sonnet") == ("sonnet", "")

    def test_unsplittable_id_comes_back_whole(self):
        assert split_model_id("gpt-5.6-sol") == ("gpt-5.6-sol", "")
