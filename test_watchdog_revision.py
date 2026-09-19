import importlib.util
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pathlib import Path

path=Path(__file__).with_name('watchdog.py')
if not path.exists(): path=path.with_name('deployed_watchdog.py')
spec=importlib.util.spec_from_file_location('watchdog_under_test',path)
w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
TZ=ZoneInfo('Asia/Tehran')
class WatchdogTests(unittest.TestCase):
 def test_ten_minute_snapshot_not_five_minute_failure(self):
  now=datetime(2026,9,19,10,12,tzinfo=TZ)
  with patch.object(w,'command',return_value=type('R',(),{'stdout':'active'})()),patch.object(w,'latest_success_age',return_value=0),patch.object(w,'latest_snapshot_age',return_value=7),patch.object(w,'chart_age',return_value=7):
   self.assertEqual(w.inspect(now),[])
 def test_no_monitoring_after_last_report(self):
  self.assertFalse(w.market_window(datetime(2026,9,19,12,30,tzinfo=TZ)))
 def test_recent_closed_session_skips_snapshot_and_chart_checks(self):
  with patch.object(w,'command',return_value=type('R',(),{'stdout':'active'})()),patch.object(w,'latest_success_age',return_value=0),patch.object(w,'latest_snapshot_age',return_value=1000),patch.object(w,'chart_age',return_value=1000),patch.object(w,'confirmed_closed',return_value=True,create=True):
   self.assertEqual(w.inspect(datetime(2026,9,19,10,7,tzinfo=TZ)),[])
 def test_v2_snapshot_is_authoritative(self):
  import tempfile,sqlite3
  now=datetime(2026,9,19,10,7,tzinfo=TZ)
  with tempfile.TemporaryDirectory() as d:
   db=Path(d)/'test.db'
   with sqlite3.connect(db) as c:
    c.execute('CREATE TABLE v2_slots(ts TEXT,payload TEXT)');c.execute('INSERT INTO v2_slots VALUES (?,?)',('2026-09-19T10:05:00+03:30','{}'))
   c.close()
   with patch.object(w,'DB_PATH',db): self.assertEqual(w.latest_snapshot_age(now),2)
 def test_core_chart_files(self):
  import tempfile,os
  now=datetime(2026,9,19,10,7,tzinfo=TZ)
  with tempfile.TemporaryDirectory() as d:
   for name in ('market_panels.png','industries.png','allocation.png'):
    p=Path(d)/name;p.write_bytes(b'test');os.utime(p,(now.timestamp()-120,now.timestamp()-120))
   with patch.object(w,'CHART_DIR',Path(d)):self.assertEqual(w.chart_age(now),2)
 def test_missing_fetch_is_not_healthy(self):
  with patch.object(w,'command',return_value=type('R',(),{'stdout':'active'})()),patch.object(w,'latest_success_age',return_value=None),patch.object(w,'latest_snapshot_age',return_value=2),patch.object(w,'chart_age',return_value=2):
   self.assertTrue(w.inspect(datetime(2026,9,19,10,7,tzinfo=TZ)))
if __name__=='__main__':unittest.main()
