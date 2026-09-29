"""Short-code generation and validation.

The interesting properties are statistical (is the generator actually drawing
from the whole alphabet?) and adversarial (can a custom alias smuggle in a
path separator, a reserved route, or a case-collision?).
"""

from __future__ import annotations

import math
import re
from collections import Counter

import pytest

from app.core.errors import InvalidShortCodeError, ShortCodeNotFoundError
from app.services.short_code import (
    ALPHABET,
    RESERVED_CODES,
    collision_probability,
    generate_short_code,
    keyspace_size,
    validate_short_code,
)
from tests.conftest import make_settings


class TestGeneration:
    @pytest.mark.parametrize("length", [4, 7, 10, 16])
    def test_generates_requested_length_from_alphabet(self, length: int) -> None:
        for _ in range(200):
            code = generate_short_code(length)
            assert len(code) == length
            assert set(code) <= set(ALPHABET)

    def test_codes_are_not_sequential_or_repeating(self) -> None:
        """Guards against a future refactor to a counter/UUID-prefix scheme.

        Sequential codes would let anyone enumerate every link in the system,
        so this is a security property, not just an aesthetic one.
        """
        codes = [generate_short_code(7) for _ in range(1000)]
        assert len(set(codes)) == 1000, "generator produced a duplicate in 1k draws"

    def test_draws_from_the_whole_alphabet(self) -> None:
        """A truncated alphabet silently shrinks the keyspace.

        With 62 symbols and 62*200 draws, every symbol should appear; the
        probability of any given symbol being absent is (1-1/62)^12400, which
        is far below any plausible flake threshold.
        """
        drawn = Counter("".join(generate_short_code(62) for _ in range(200)))
        assert set(drawn) == set(ALPHABET)

    def test_distribution_is_roughly_uniform(self) -> None:
        """A biased generator concentrates collisions in part of the keyspace."""
        sample = "".join(generate_short_code(62) for _ in range(500))
        counts = Counter(sample)
        expected = len(sample) / len(ALPHABET)
        # Chi-square would be more rigorous, but a generous per-symbol band
        # catches the realistic failure (modulo bias) without being flaky.
        for symbol, count in counts.items():
            assert 0.7 * expected < count < 1.3 * expected, f"symbol {symbol!r} skewed"


class TestKeyspaceMath:
    def test_keyspace_size_is_62_to_the_length(self) -> None:
        assert keyspace_size(1) == 62
        assert keyspace_size(7) == 62**7
        assert isinstance(keyspace_size(7), int)

    def test_collision_probability_grows_with_stored_rows(self) -> None:
        at_1m = collision_probability(1_000_000, 7)
        at_10m = collision_probability(10_000_000, 7)
        assert 0.0 < at_1m < at_10m < 1.0

    def test_collision_probability_is_zero_for_empty_table(self) -> None:
        assert collision_probability(0, 7) == 0.0

    def test_collision_probability_is_occupancy_of_the_keyspace(self) -> None:
        """The metric is P(next insert collides) = n/N, not the birthday bound.

        The birthday bound answers "will *any* pair among n collide", which is
        the wrong question here: every insert is guarded by a UNIQUE index and
        retried, so what matters operationally is how often a single insert
        needs a retry.
        """
        assert collision_probability(1, 7) == pytest.approx(1 / 62**7)
        assert collision_probability(62**7, 7) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (1_000_000, 2.8e-7),
            (10_000_000, 2.8e-6),
            (100_000_000, 2.8e-5),
        ],
    )
    def test_documented_collision_table_stays_accurate(self, stored: int, expected: float) -> None:
        """Pins the table published in app/services/short_code.py and the docs.

        If SHORT_CODE_LENGTH or the alphabet ever changes, these numbers become
        wrong and the documentation silently starts lying. This fails first.
        """
        assert collision_probability(stored, 7) == pytest.approx(expected, rel=0.02)

    def test_five_consecutive_collisions_is_negligible(self) -> None:
        """Justifies SHORT_CODE_MAX_ATTEMPTS=5 as an adequate retry budget."""
        per_insert = collision_probability(100_000_000, 7)
        assert math.pow(per_insert, 5) < 1e-20


class TestValidation:
    def test_accepts_a_generated_code(self) -> None:
        settings = make_settings()
        code = generate_short_code(settings.short_code_length)
        assert validate_short_code(code, settings) == code

    @pytest.mark.parametrize(
        "code",
        [
            "",  # empty
            "a" * 40,  # longer than short_code_max_length
            "abc/def",  # path separator -- would break routing
            "abc def",  # whitespace
            "abc.def",  # dot could collide with a file extension route
            "abc%2F",  # encoded separator
            "../etc",  # traversal attempt
            "abc\n",  # newline (header/log injection shape)
            "abc\x00",  # NUL
            "café12",  # non-ASCII
            "abc+def",  # plus
            "abc?x=1",  # query string
        ],
    )
    def test_malformed_codes_are_a_miss_on_the_lookup_path(self, code: str) -> None:
        """404: a path that cannot name a link is not found, not "invalid"."""
        with pytest.raises(ShortCodeNotFoundError):
            validate_short_code(code, make_settings())

    @pytest.mark.parametrize(
        "code",
        [
            "",
            "a" * 40,
            "abc/def",
            "abc def",
            "abc.def",
            "café12",
            "abc?x=1",
        ],
    )
    def test_malformed_aliases_are_a_validation_error(self, code: str) -> None:
        """422: the alias is a form field the user can correct."""
        with pytest.raises(InvalidShortCodeError):
            validate_short_code(code, make_settings(), is_custom_alias=True)

    @pytest.mark.parametrize("reserved", sorted(RESERVED_CODES)[:8])
    def test_rejects_reserved_codes_as_custom_alias(self, reserved: str) -> None:
        """A custom alias of `api` or `health` would shadow a real route."""
        with pytest.raises(InvalidShortCodeError):
            validate_short_code(reserved, make_settings(), is_custom_alias=True)

    @pytest.mark.parametrize("reserved", sorted(RESERVED_CODES)[:8])
    def test_reserved_codes_are_a_miss_on_the_lookup_path(self, reserved: str) -> None:
        """404, not 422.

        `GET /docs` on a deployment with docs disabled falls through to the
        catch-all redirect route. Answering "malformed short code" there would
        be both wrong and a disclosure of which words are reserved.
        """
        with pytest.raises(ShortCodeNotFoundError):
            validate_short_code(reserved, make_settings())

    def test_reserved_set_covers_every_real_route_prefix(self) -> None:
        """Keeps RESERVED_CODES honest as routes are added."""
        for required in ("api", "health", "ready", "metrics", "docs"):
            assert required in RESERVED_CODES

    def test_custom_alias_allows_hyphen_and_underscore(self) -> None:
        settings = make_settings()
        for alias in ("my-link", "my_link", "My-Link_2"):
            assert validate_short_code(alias, settings, is_custom_alias=True) == alias

    def test_generated_alphabet_excludes_hyphen_and_underscore(self) -> None:
        """Hyphen/underscore are allowed for humans but never generated.

        Keeping them out of ALPHABET means a generated code can never be
        confused for a user-chosen alias in logs or support tickets.
        """
        assert "-" not in ALPHABET
        assert "_" not in ALPHABET

    def test_short_codes_resolve_on_the_lookup_path_but_are_rejected_as_aliases(
        self,
    ) -> None:
        """Asymmetry is deliberate, not an oversight.

        Lookup imposes no minimum length: if SHORT_CODE_LENGTH is ever reduced
        or was smaller in the past, previously issued links must keep
        resolving. Minting a *new* 2-character alias is a different matter --
        the short end of the keyspace is guessable, so aliases require 4+.
        """
        settings = make_settings()
        assert validate_short_code("ab", settings) == "ab"
        with pytest.raises(InvalidShortCodeError):
            validate_short_code("ab", settings, is_custom_alias=True)

    def test_error_message_is_actionable_for_an_alias(self) -> None:
        """The alias path must say what to fix; the lookup path says nothing."""
        with pytest.raises(InvalidShortCodeError) as excinfo:
            validate_short_code("bad/alias", make_settings(), is_custom_alias=True)
        assert "letters, digits" in str(excinfo.value)

    def test_validation_is_case_sensitive(self) -> None:
        """Base62 is case-sensitive, so `aB3` and `Ab3` are different links.

        Folding case would quietly quarter the keyspace and let one user's
        link resolve to another's.
        """
        settings = make_settings()
        assert validate_short_code("aBcDeFg", settings) == "aBcDeFg"
        assert validate_short_code("AbCdEfG", settings) == "AbCdEfG"

    def test_error_carries_no_user_input(self) -> None:
        """The message is surfaced to clients and written to logs.

        Echoing the raw code back would make the API a reflection point for
        injected content, so the message must stay static.
        """
        settings = make_settings()
        with pytest.raises(InvalidShortCodeError) as excinfo:
            validate_short_code("<script>alert(1)</script>", settings, is_custom_alias=True)
        assert "script" not in str(excinfo.value).lower()
        assert not re.search(r"[<>]", str(excinfo.value))
