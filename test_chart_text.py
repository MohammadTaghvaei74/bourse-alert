"""Offline regressions for Matplotlib Persian text shaping."""
import unittest
from unittest.mock import patch

import matplotlib
import main as m


SAMPLES = ('میانه نمره بازار', 'هم‌وزن شدید',
           'نمونه تاریخی — 2026-09-20 10:05 تهران',
           'خالص صف -12.5% | MA5/MA15−1 | ۱۵ روز', '', -12.5)


class ChartTextTests(unittest.TestCase):
    def test_native_matplotlib_preserves_logical_unicode(self):
        self.assertTrue(callable(getattr(m, 'chart_text', None)),
                        'module-level chart_text helper is missing')
        for version in ('3.11.0', '3.11.2', '3.11.0rc1', '3.12.0', '4.0.0'):
            with patch.object(matplotlib, '__version__', version):
                for text in SAMPLES:
                    with self.subTest(version=version, text=text):
                        self.assertEqual(m.chart_text(text), str(text))

    def test_older_matplotlib_uses_manual_shaping(self):
        import arabic_reshaper
        from bidi.algorithm import get_display
        for version in ('3.10.0', '3.10.8', '3.9.4'):
            with patch.object(matplotlib, '__version__', version):
                for text in SAMPLES:
                    with self.subTest(version=version, text=text):
                        self.assertEqual(m.chart_text(text),
                                         get_display(arabic_reshaper.reshape(str(text))))

    def test_explicit_shaping_override(self):
        import arabic_reshaper
        from bidi.algorithm import get_display
        for version in ('3.10.8', '3.11.2'):
            with patch.object(matplotlib, '__version__', version):
                for text in SAMPLES:
                    self.assertEqual(m.chart_text(text, native_shaping=True), str(text))
                    self.assertEqual(m.chart_text(text, native_shaping=False),
                                     get_display(arabic_reshaper.reshape(str(text))))

    def test_reports_route_labels_through_chart_text(self):
        import tempfile
        from datetime import datetime
        from pathlib import Path
        now = datetime(2026, 9, 19, 9, 5, tzinfo=m.TEHRAN)
        with tempfile.TemporaryDirectory() as d:
            store = m.Store(Path(d) / 'db.sqlite')
            stocks = [dict(symbol='وبملت', score=3, trade_volume=1,
                           eligible_market_stock=True, industry='بانک', trade_value_toman=1)]
            summary = m.summarize(stocks, now.date())
            store.save(now, stocks, summary)
            with patch.object(m, 'chart_text', wraps=m.chart_text) as helper:
                m.generate_reports(store, now, summary, d, historical=True)
            labels = {call.args[0] for call in helper.call_args_list}
            self.assertTrue({'میانه نمره بازار', 'بانک', 'هم‌وزن شدید',
                             'زمان تهران', 'میانه لیدرها'}.issubset(labels))


if __name__ == '__main__':
    unittest.main()
