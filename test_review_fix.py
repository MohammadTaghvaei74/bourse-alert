"""Offline regressions for the two release-review blockers."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import main as m
import watchdog as w

NOW = datetime(2026, 9, 19, 10, 5, tzinfo=m.TEHRAN)


class ClosureReviewTests(unittest.TestCase):
    def test_tick_requires_current_recent_source_for_closed(self):
        for day, asof, status in [('2026-09-18', '12:30:00', 'stale'),
                                  ('2026-09-19', '09:00:00', 'stale'),
                                  ('2026-09-19', '10:10:00', 'stale'),
                                  ('2026-09-19', '10:04:00', 'closed')]:
            with self.subTest(day=day, asof=asof), tempfile.TemporaryDirectory() as d:
                store = m.Store(Path(d)/'db')
                packet = dict(market_date=day, asof=asof, closed=True, stocks=[], depth=[])
                self.assertEqual(m.tick(store, NOW, lambda: packet, {}, d, include_legacy=False), [])
                health = json.loads((Path(d)/'runtime_health.json').read_text())
                self.assertEqual(health['status'], status)
                self.assertEqual(health['source_asof'], day+'T'+asof+'+03:30')
                self.assertFalse(health['session_open'])
                self.assertEqual(store.points(NOW.date()), [])

    def test_watchdog_requires_source_evidence_not_just_recent_check(self):
        cases = [('2026-09-18', '2026-09-18T12:30:00+03:30', False),
                 ('2026-09-19', '2026-09-19T09:00:00+03:30', False),
                 ('2026-09-19', None, False),
                 ('2026-09-19', '2026-09-19T10:10:00+03:30', False),
                 ('2026-09-19', '2026-09-19T10:04:00+03:30', True)]
        for day, asof, expected in cases:
            with self.subTest(day=day, asof=asof), tempfile.TemporaryDirectory() as d:
                health = dict(checked_at=NOW.isoformat(), market_date=day, source_asof=asof,
                              status='closed', session_open=False)
                (Path(d)/'runtime_health.json').write_text(json.dumps(health))
                with patch.object(w, 'PROJECT', Path(d)), patch.object(w, 'command', return_value=type('R', (), {'stdout':'active'})()), patch.object(w, 'latest_success_age', return_value=0), patch.object(w, 'latest_snapshot_age', return_value=1440), patch.object(w, 'chart_age', return_value=1440):
                    self.assertEqual(w.confirmed_closed(NOW), expected)
                    self.assertEqual(bool(w.inspect(NOW)), not expected)

    def test_stale_evidence_warns_even_with_recent_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            health = dict(checked_at=NOW.isoformat(), market_date='2026-09-18',
                          source_asof='2026-09-18T12:30:00+03:30', status='stale', session_open=False)
            (Path(d)/'runtime_health.json').write_text(json.dumps(health))
            with patch.object(w, 'PROJECT', Path(d)), patch.object(w, 'command', return_value=type('R', (), {'stdout':'active'})()), patch.object(w, 'latest_success_age', return_value=0), patch.object(w, 'latest_snapshot_age', return_value=2), patch.object(w, 'chart_age', return_value=2):
                self.assertTrue(w.inspect(NOW))

    def test_healthy_fetch_with_missing_data_warns_without_restart(self):
        with patch.object(w, 'datetime') as clock, patch.object(w, 'load_state', return_value={}), patch.object(w, 'inspect', return_value=['upstream stale']), patch.object(w, 'latest_success_age', return_value=0), patch.object(w, 'command', return_value=type('R', (), {'stdout':'active'})()) as command, patch.object(w, 'send_alert') as alert, patch.object(w, 'save_state') as save, patch.object(w.time, 'sleep'), patch.object(w.sys, 'argv', ['watchdog.py']):
            clock.now.return_value = NOW
            w.main()
            self.assertFalse(any('restart' in call.args for call in command.call_args_list))
            alert.assert_called_once()
            self.assertNotIn('انجام شد', alert.call_args.args[0])
            save.assert_called_once_with('problem', ['upstream stale'])


def stock_row():
    fields = ['0'] * 26
    for i, value in {0:'123', 1:'IRO1TEST0001', 2:'سهم', 3:'شرکت', 6:'101', 7:'102', 9:'10', 10:'1000', 13:'100', 15:'1', 18:'57', 19:'103', 20:'97', 22:'300'}.items():
        fields[i] = value
    return ','.join(fields)


class DepthReviewTests(unittest.TestCase):
    def test_global_unavailable_depth_preserves_turnover_not_scores(self):
        for depth in ([], None, [''], ['broken'], ['123,1'], ['123,1,1,1,NaN,0,0,0'], ['123,1,1,1,103,0,-1,0'], [None], {'error':'unavailable'}):
            with self.subTest(depth=depth):
                stocks = m.parse_market_data([stock_row()], depth)
                self.assertEqual(stocks[0]['trade_value_toman'], 100)
                for key in ('score', 'buy_queue_volume', 'sell_queue_volume', 'buy_queue_value_toman', 'sell_queue_value_toman', 'queue_value'):
                    self.assertIsNone(stocks[0][key], key)
                summary = m.summarize(stocks, NOW.date(), 100)
                for key in ('median', 'buy_toman', 'sell_toman', 'queue_ratio'):
                    self.assertIsNone(summary[key], key)
                self.assertIsNone(m.stars(summary['queue_ratio'], 'queue'))
                self.assertEqual(summary['turnover_toman'], 100)

    def test_live_missing_depth_persists_unavailable_not_zero(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                store = m.Store(Path(d)/'db')
                packet = dict(market_date=str(NOW.date()), asof='10:05:00', closed=False, stocks=[stock_row()])
                if not missing:
                    packet['depth'] = []
                with patch.object(m, 'generate_reports', return_value=[]) as render:
                    m.tick(store, NOW, lambda:packet, {}, d, include_legacy=False)
                    summary = render.call_args.args[2]
                    self.assertIsNone(summary['buy_toman'])
                    self.assertIsNone(summary['queue_ratio'])
                    self.assertIsNone(summary['median'])
                    self.assertIsNone(store.points(NOW.date())[0][1]['buy_toman'])

    def test_explicit_valid_zero_book_is_zero_and_turnover_finalizes_without_depth(self):
        stocks = m.parse_market_data([stock_row()], ['123,1,0,0,0,0,0,0'])
        self.assertEqual(stocks[0]['score'], 2)
        self.assertEqual(m.summarize(stocks, NOW.date(), 100)['queue_ratio'], 0)
        with tempfile.TemporaryDirectory() as d:
            store = m.Store(Path(d)/'db')
            packet = dict(market_date=str(NOW.date()), asof='12:30:00', closed=True, stocks=[stock_row()], depth=[])
            m.tick(store, NOW.replace(hour=13, minute=0), lambda:packet, {}, d)
            with store.connect() as c:
                self.assertEqual(c.execute('SELECT turnover,complete FROM v2_days').fetchone(), (100, 1))


class ClockReviewTests(unittest.TestCase):
    def test_fetch_crossing_report_boundary_uses_completion_clock(self):
        before = NOW.replace(hour=9, minute=4, second=59)
        after = NOW.replace(hour=9, minute=5, second=1)
        packet = dict(market_date=str(NOW.date()), asof='09:05:00', closed=False,
                      stocks=[stock_row()], depth=['123,1,0,0,0,0,0,0'])
        current = [before]
        def fetch():
            current[0] = after
            return packet
        with tempfile.TemporaryDirectory() as d, patch.object(m, 'generate_reports', return_value=[]), patch.object(m, 'legacy_reports', return_value=[]):
            store = m.Store(Path(d)/'db')
            m.serve(store, {}, d, fetcher=fetch, clock=lambda:current[0], once=True)
            points = store.points(NOW.date())
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0][0], after.replace(second=0))
            health = json.loads((Path(d)/'runtime_health.json').read_text())
            self.assertEqual(health['checked_at'], after.isoformat())
            self.assertEqual(health['source_asof'], '2026-09-19T09:05:00+03:30')
            with store.connect() as c:
                self.assertEqual(json.loads(c.execute('SELECT payload FROM v2_raw').fetchone()[0])['asof'], '09:05:00')


if __name__ == '__main__':
    unittest.main()
