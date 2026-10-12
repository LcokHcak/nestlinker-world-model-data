import unittest
from scripts.collect_context_sources import parse_bok_rates


class ContextParserTests(unittest.TestCase):
    def test_policy_rate_table_ignores_navigation_and_supports_rowspan_year(self):
        text = '''<table><tr><td>2026</td><td>01월 01일</td><td>9.9</td></tr></table>
        <table><tr><th>변경일자</th><th>기준금리</th></tr>
        <tr><td rowspan="2">2026</td><td>08월 27일</td><td>3.00</td></tr>
        <tr><td>07월 16일</td><td>2.75</td></tr></table>'''
        rows = parse_bok_rates(text)
        self.assertEqual(rows, [{"effective_date": "2026-07-16", "base_rate_pct": 2.75},
                                {"effective_date": "2026-08-27", "base_rate_pct": 3.0}])

    def test_missing_rate_does_not_become_zero(self):
        with self.assertRaises(ValueError):
            parse_bok_rates('<table><tr><th>변경일자</th><th>기준금리</th></tr>'
                            '<tr><td>2026</td><td>08월 27일</td><td></td></tr></table>')

    def test_duplicate_effective_dates_are_rejected(self):
        row = '<tr><td>2026</td><td>08월 27일</td><td>3.00</td></tr>'
        with self.assertRaises(ValueError):
            parse_bok_rates('<table><tr><th>변경일자</th><th>기준금리</th></tr>' + row + row + '</table>')
