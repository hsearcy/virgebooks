"""Tests for reading-level (difficulty) configuration of generated stories.

These cover the pure prompt-building logic, so they run without any API key or
network access. Run with:  python -m unittest discover -s tests
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class NormalizeLevelTests(unittest.TestCase):
    def test_default_when_missing_or_empty(self):
        self.assertEqual(app.normalize_level(None), app.DEFAULT_LEVEL)
        self.assertEqual(app.normalize_level(""), app.DEFAULT_LEVEL)
        self.assertEqual(app.normalize_level("   "), app.DEFAULT_LEVEL)

    def test_default_when_unknown(self):
        self.assertEqual(app.normalize_level("phd"), app.DEFAULT_LEVEL)

    def test_known_levels_pass_through(self):
        for key in app.LEVELS:
            self.assertEqual(app.normalize_level(key), key)

    def test_case_and_separator_insensitive(self):
        self.assertEqual(app.normalize_level("CVC-PLUS"), "cvc-plus")
        self.assertEqual(app.normalize_level("cvc_plus"), "cvc-plus")
        self.assertEqual(app.normalize_level("  Cvc  "), "cvc")

    def test_default_level_is_a_known_level(self):
        self.assertIn(app.DEFAULT_LEVEL, app.LEVELS)


class BuildStoryPromptTests(unittest.TestCase):
    def test_includes_page_count(self):
        prompt = app.build_story_prompt(7, "", "cvc")
        self.assertIn("exactly 7 pages", prompt)

    def test_cvc_level_forbids_four_letter_words(self):
        prompt = app.build_story_prompt(10, "", "cvc")
        self.assertIn("three-letter CVC", prompt)
        self.assertNotIn("four-letter", prompt)

    def test_cvc_plus_level_allows_some_four_letter_words(self):
        prompt = app.build_story_prompt(10, "", "cvc-plus")
        self.assertIn("four-letter words, but sparingly", prompt)
        # Still anchored on CVC words as the staple.
        self.assertIn("three-letter CVC", prompt)

    def test_four_plus_level_requires_four_letter_words(self):
        prompt = app.build_story_prompt(10, "", "four-plus")
        self.assertIn("MUST contain at least one four-letter word", prompt)
        self.assertNotIn("sparingly: at most one per", prompt)

    def test_unknown_level_falls_back_to_default_rules(self):
        unknown = app.build_story_prompt(10, "", "wizard", rng=random.Random(7))
        default = app.build_story_prompt(
            10, "", app.DEFAULT_LEVEL, rng=random.Random(7)
        )
        self.assertEqual(unknown, default)

    def test_instructions_are_woven_in(self):
        prompt = app.build_story_prompt(5, "a pig and a red hat", "cvc")
        self.assertIn("a pig and a red hat", prompt)

    def test_blank_instructions_add_nothing(self):
        with_blank = app.build_story_prompt(5, "   ", "cvc", rng=random.Random(7))
        without = app.build_story_prompt(5, "", "cvc", rng=random.Random(7))
        self.assertEqual(with_blank, without)

    def test_example_words_vary_between_generations(self):
        a = app.build_story_prompt(10, "", "cvc", rng=random.Random(1))
        b = app.build_story_prompt(10, "", "cvc", rng=random.Random(2))
        self.assertNotEqual(a, b)

    def test_examples_are_declared_optional(self):
        for level in app.LEVELS:
            prompt = app.build_story_prompt(10, "", level)
            self.assertIn("NOT a required list", prompt)


def _pages(*sentences):
    return [{"sentence": s} for s in sentences]


class LevelSatisfiedTests(unittest.TestCase):
    def test_counts_pages_with_a_four_letter_word(self):
        pages = _pages("Max can run.", "Max ran fast.", "The cat did nap.")
        self.assertEqual(app.pages_with_four_letter_word(pages), 1)  # "fast"

    def test_cvc_is_always_satisfied(self):
        self.assertTrue(app.level_satisfied("cvc", _pages("Max can run.")))

    def test_cvc_plus_needs_at_least_one_four_letter_word(self):
        self.assertFalse(app.level_satisfied("cvc-plus", _pages("Max can run.")))
        self.assertTrue(
            app.level_satisfied("cvc-plus", _pages("Max can run.", "Max ran fast."))
        )

    def test_four_plus_needs_four_letter_words_on_half_the_pages(self):
        # The real story that slipped through: 10 pages, only one 4-letter word.
        bad = _pages(
            "Max can run.", "Max ran fast.", "Max saw a cat.", "The cat did nap.",
            "Max can dig.", "Max dug a pit.", "Max saw a bug.", "The bug is red.",
            "Max did a hop.", "Max had fun.",
        )
        self.assertFalse(app.level_satisfied("four-plus", bad))
        good = _pages(
            "Max can jump.", "Max ran fast.", "Max dug in sand.", "A frog can hop.",
        )
        self.assertTrue(app.level_satisfied("four-plus", good))


if __name__ == "__main__":
    unittest.main()
