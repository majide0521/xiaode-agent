import unittest

from xiaode.retrieval import classify_source


class SourceClassificationTests(unittest.TestCase):
    def test_official_subdomain_is_tier_a(self) -> None:
        self.assertEqual(classify_source("https://blog.example.com/post", "example.com"), "Tier A")

    def test_lookalike_official_domain_is_not_tier_a(self) -> None:
        self.assertEqual(classify_source("https://example.com.evil.test/post", "example.com"), "Tier C")

    def test_media_name_substring_is_not_tier_b(self) -> None:
        self.assertEqual(classify_source("https://fakereuters.com/story", "example.com"), "Tier C")


if __name__ == "__main__":
    unittest.main()
