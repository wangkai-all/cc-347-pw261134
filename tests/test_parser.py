import unittest

from annarchive.parser import parse_body


class ParserTests(unittest.TestCase):
    def test_parses_links_and_checksums(self):
        body = (
            "# t\n"
            "see [A](files/a.pdf) and [B](http://x/y.csv)\n"
            f"<!-- checksums\nfiles/a.pdf = {'a' * 64} sha256\n-->\n")
        refs = parse_body(body, "http://host/pub/ann")
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].source_url, "http://host/pub/files/a.pdf")
        self.assertEqual(refs[0].declared_sha256, "a" * 64)
        self.assertEqual(refs[1].source_url, "http://x/y.csv")
        self.assertIsNone(refs[1].declared_sha256)

    def test_relative_resolution_and_duplicate_dedup(self):
        body = ("[A](a.pdf) [A again](a.pdf) [C](http://h/c.bin)\n"
                f"<!-- checksums\na.pdf = {'b' * 64} sha256\n-->")
        refs = parse_body(body, "http://host/dir/ann")
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].source_url, "http://host/dir/a.pdf")
        self.assertEqual(refs[0].label, "A")

    def test_pure_deterministic(self):
        body = f"[A](http://h/a)\n<!-- checksums\na = {'c' * 64} sha256\n-->"
        r1 = [r.to_dict() for r in parse_body(body, "http://h/ann")]
        r2 = [r.to_dict() for r in parse_body(body, "http://h/ann")]
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
