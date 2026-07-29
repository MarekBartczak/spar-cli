"""Display names for model identifiers.

A side's config names models the way its CLI accepts them, and claude's CLI
takes bare family aliases -- ``opus``, ``sonnet``, ``haiku``. Those aliases say
nothing about WHICH generation actually ran, so a task board full of
"sonnet → gpt-5.6-sol" hid the fact that one column carries a version and the
other does not. This module resolves an alias to the generation it currently
means, for DISPLAY only: nothing here is ever passed back to a CLI.

The alias table is the one thing that rots when a new generation ships. It is
verified against reality rather than assumed: the transcripts of a run record
the id the CLI resolved the alias to (``claude-sonnet-5``, ``claude-opus-5``),
so :func:`display_model` on ``"sonnet"`` must agree with what those show.

Anything that already carries a digit (``gpt-5.6-sol``, ``sonnet-4.6``) is left
alone -- it already states its generation.
"""

from __future__ import annotations

import re

__all__ = ["ALIAS_GENERATIONS", "display_model", "split_model_id"]

# Alias -> the generation that alias resolves to today. Keep in sync with what
# the CLIs actually run; see the module docstring for how to check.
ALIAS_GENERATIONS: dict[str, str] = {
    "opus": "5",
    "sonnet": "5",
    "fable": "5",
    "haiku": "4.5",
}

_HAS_DIGIT_RE = re.compile(r"\d")
# A full id ("claude-opus-5", "claude-3-5-haiku-20241022") -- the vendor prefix
# is noise in a label whose column already implies the vendor.
_VENDOR_PREFIX_RE = re.compile(r"^(?:claude|anthropic)-")


def split_model_id(name: str) -> tuple[str, str]:
    """Split a model id into ``(family, generation)``; generation may be ``""``.

    ``"claude-opus-5"`` -> ``("opus", "5")``, ``"sonnet"`` -> ``("sonnet", "")``,
    ``"gpt-5.6-sol"`` -> ``("gpt-5.6-sol", "")`` (nothing to split usefully).
    """
    stripped = _VENDOR_PREFIX_RE.sub("", name.strip())
    match = re.fullmatch(r"([a-z]+)-([\d.]+)", stripped)
    if match:
        return match.group(1), match.group(2)
    return stripped, ""


def display_model(name: "str | None") -> str:
    """Label for ``name``: a bare claude alias gains its generation.

    ``"sonnet"`` -> ``"sonnet 5"``, ``"claude-opus-5"`` -> ``"opus 5"``,
    unknown alias -> unchanged.

    A name that already carries a digit is returned VERBATIM: codex ids are
    written the way its CLI takes them and their shape varies
    (``gpt-5.6-sol``), so rewriting some of them (``gpt-5.5`` -> ``gpt 5.5``)
    and not others would look like an inconsistency rather than a convention.
    """
    if not name:
        return ""
    raw = name.strip()
    if not raw:
        return ""
    if _VENDOR_PREFIX_RE.match(raw):
        # A full anthropic id: the vendor prefix is noise, the generation is not.
        family, generation = split_model_id(raw)
        return f"{family} {generation}" if generation else family
    if _HAS_DIGIT_RE.search(raw):
        return raw
    known = ALIAS_GENERATIONS.get(raw.lower())
    return f"{raw} {known}" if known else raw
