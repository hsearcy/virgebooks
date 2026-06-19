"""Tests for reading-level (difficulty) configuration of generated stories.

These cover the pure prompt-building logic, so they run without any API key or
network access. Run with:  python -m unittest discover -s tests
"""

import os
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
        self.assertIn("four-letter", prompt)
        # Still anchored on CVC words as the staple.
        self.assertIn("three-letter CVC", prompt)

    def test_unknown_level_falls_back_to_default_rules(self):
        unknown = app.build_story_prompt(10, "", "wizard")
        default = app.build_story_prompt(10, "", app.DEFAULT_LEVEL)
        self.assertEqual(unknown, default)

    def test_instructions_are_woven_in(self):
        prompt = app.build_story_prompt(5, "a pig and a red hat", "cvc")
        self.assertIn("a pig and a red hat", prompt)

    def test_blank_instructions_add_nothing(self):
        with_blank = app.build_story_prompt(5, "   ", "cvc")
        without = app.build_story_prompt(5, "", "cvc")
        self.assertEqual(with_blank, without)


if __name__ == "__main__":
    unittest.main()
