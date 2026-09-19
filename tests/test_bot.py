import unittest, tempfile, os
from datetime import datetime, date, timedelta
import importlib

def api():
    try: return importlib.import_module('main')
    except ModuleNotFoundError: return None

class Contract(unittest.TestCase):
    def test_uncapped_queue_score_and_zero_volume(self):
        m=api(); self.assertIsNotNone(m, 'main implementation missing')
        self.assertIsNone(m.calculate_score(2,100,0,0,3,-3))
        self.assertEqual(m.calculate_score(2,100,0,1,3,-3),103)
        self.assertEqual(m.calculate_score(2,0,100,1,3,-3),-103)
        self.assertEqual(m.calculate_score(2,0,0,1,3,-3),2)

    def test_schedule_and_threshold_boundaries(self):
        m=api(); self.assertTrue(hasattr(m,'report_slots'))
        slots=m.report_slots(date(2026,9,19))
        self.assertEqual(len(slots),21); self.assertEqual(slots[0].strftime('%H:%M'),'09:05')
        self.assertEqual(slots[-1].strftime('%H:%M'),'12:25')
        self.assertEqual(m.report_slots(date(2026,9,18)),[])
        for kind, values in [('median',[(2,4),(1,3),(-1,3),(-2,2)]),('turnover',[(.25,4),(.1,3),(-.1,3),(-.25,2)]),('queue',[(.5,4),(.2,3),(-.2,3),(-.5,2)])]:
            for value,expected in values: self.assertEqual(m.stars(value,kind),expected)
        self.assertIsNone(m.stars(None,'median'))
        self.assertEqual([m.allocation(x,0)[0] for x in [2,1.5,.5,-.5,-1.5,-2]],[5,4,3,3,2,1])

    def test_universe_queue_ipo_and_industry_pipeline(self):
        m=api(); self.assertTrue(hasattr(m,'summarize'))
        today=date(2026,9,19)
        def row(symbol,score,volume=1,**kw):
            return dict(symbol=symbol,score=score,trade_volume=volume,eligible_market_stock=True,industry='بانک',trade_value_toman=10,buy_queue_value_toman=100,sell_queue_value_toman=5,**kw)
        stocks=[row('وبملت',3),row('الف',1,first_trade_date='2026-09-01'),row('ب',None,0),row('پالایش',10)]
        stocks[-1]['eligible_market_stock']=False
        s=m.summarize(stocks,today,200)
        self.assertEqual(s['median'],2); self.assertEqual(s['leader_median'],6.5)
        self.assertEqual(s['nonleader_median'],1); self.assertEqual(s['buy_toman'],200)
        self.assertEqual(s['sell_toman'],15); self.assertEqual(s['queue_ratio'],.925)
        self.assertEqual(s['industries'][0]['stocks'][0]['symbol'],'وبملت')
        self.assertFalse(m.is_common_stock('نماد2','شرکت بلوکی','N1'))
        self.assertFalse(m.is_common_stock('اهرم','صندوق اهرمی','N1'))
        self.assertTrue(m.is_common_stock('نماد','شرکت بازار پایه','Z1'))
        f=['0']*26
        for i,v in {0:'123',2:'نماد',3:'شرکت',7:'102',9:'1',10:'102',13:'100',18:'57',19:'103',20:'97',25:'Z1'}.items(): f[i]=v
        parsed=m.parse_market_data([','.join(f)],['123,1,1,1,103,0,100,0'])
        self.assertEqual(parsed[0]['score'],103)
        self.assertEqual(parsed[0]['industry'],'بانک')

    def test_durable_history_completion_incident_and_retention(self):
        m=api(); self.assertTrue(hasattr(m,'Store'))
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db.sqlite')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            def rows(values): return [dict(symbol=str(i),score=v,trade_volume=1,eligible_market_stock=True,industry='بانک',trade_value_toman=1) for i,v in enumerate(values)]
            store.save(now,rows([1,2,100]),m.summarize(rows([1,2,100]),now.date()))
            store.save(now+timedelta(minutes=10),rows([10]),m.summarize(rows([10]),now.date()))
            self.assertEqual(store.incident(now+timedelta(minutes=10))['median'],6)
            daily=store.daily_scores(now.date()); self.assertEqual(daily['0']['median'],5.5); self.assertEqual(daily['0']['last'],10)
            self.assertEqual(store.turnover_metrics(now.date()),(None,None,0))
            for i in range(1,16): store.finalize_day(now.date()-timedelta(days=i),100+i,verified=True,source='official final daily')
            store.finalize_day(now.date(),999999,verified=True,source='official final daily')
            x,avg,n=store.turnover_metrics(now.date()); self.assertEqual(n,15); self.assertEqual(avg,108); self.assertAlmostEqual(x,103/108-1)
            self.assertFalse(store.finalize_day(now.date()-timedelta(days=20),9,verified=False,source='legacy partial'))
            store.prune(now+timedelta(days=31)); self.assertEqual(store.incident(now+timedelta(days=31))['count'],0)
            self.assertEqual(len(store.points(now.date())),2)

    def test_five_message_preview_has_real_charts_gaps_and_history(self):
        m=api(); self.assertTrue(hasattr(m,'generate_reports'))
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db.sqlite')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            stocks=[dict(symbol='وبملت',score=3,trade_volume=1,eligible_market_stock=True,industry='بانک',trade_value_toman=1)]
            summary=m.summarize(stocks,now.date()); summary['turnover_x']=None
            store.save(now,stocks,summary)
            messages=m.generate_reports(store,now,summary,d,historical=True)
            self.assertEqual(len(messages),5)
            self.assertIn('تاریخی',messages[0]['text']); self.assertIn('داده کافی نیست',messages[0]['text'])
            for msg in messages:
                if msg['kind']=='photo':
                    with Image.open(msg['path']) as im: self.assertGreater(im.width,1000)
            series=m.chart_series(store,now+timedelta(minutes=20),summary)
            self.assertEqual(len(series['times']),3); self.assertIsNone(series['median'][1])
            self.assertEqual(series['industries']['بانک'][0],3)

    def test_replay_and_legacy_summary_repair(self):
        m=api(); self.assertTrue(hasattr(m,'replay_archive'))
        import sqlite3,json,gzip
        with tempfile.TemporaryDirectory() as d:
            db=os.path.join(d,'db'); store=m.Store(db)
            with store.connect() as c:
                c.executescript('CREATE TABLE stock_snapshots(timestamp TEXT,trading_date TEXT,symbol TEXT,score REAL,valid INTEGER); CREATE TABLE daily_stock_summary(trading_date TEXT,symbol TEXT,median_score REAL,last_score REAL);')
                c.executemany('INSERT INTO stock_snapshots VALUES (?,?,?,?,?)',[(f'2026-09-19T09:{i}0','2026-09-19','a',v,1) for i,v in enumerate([1,100,2])])
                c.execute("INSERT INTO daily_stock_summary VALUES ('2026-09-19','a',34,100)")
            self.assertEqual(m.repair_legacy_summaries(store),1)
            with store.connect() as c: self.assertEqual(c.execute('SELECT median_score,last_score FROM daily_stock_summary').fetchone(),(2,2))
            f=['0']*26
            for i,v in {0:'123',1:'IRO1TEST0001',2:'سهم',3:'شرکت',7:'102',9:'1',10:'102',13:'100',18:'57',19:'103',20:'97',22:'300',25:'N1'}.items(): f[i]=v
            for name,payload in [('s',[','.join(f)]),('d',['123,1,0,0,0,0,0,0'])]:
                with gzip.open(os.path.join(d,name),'wt',encoding='utf-8') as z: json.dump(payload,z)
            manifest=[dict(timestamp='2026-09-19T09:10:00+03:30',stocks='s',depth='d'),dict(timestamp='2026-09-19T12:30:00+03:30',stocks='s',depth='d')]
            path=os.path.join(d,'manifest.json'); m.Path(path).write_text(json.dumps(manifest),encoding='utf-8')
            self.assertEqual(m.replay_archive(store,path),1)
            self.assertEqual(store.points(date(2026,9,19))[0][0].minute,10)
            self.assertEqual(store.incident(datetime(2026,9,19,12,25,tzinfo=m.TEHRAN))['count'],1)
            f[1]='IRR1TEST0101'; f[22]='400'; self.assertFalse(m.parse_market_data([','.join(f)],[])[0]['eligible_market_stock'])
            f[1]='IRO1TEST0002'; f[22]='300'; self.assertFalse(m.parse_market_data([','.join(f)],[])[0]['eligible_market_stock'])

    def test_delivery_receipts_restart_and_ambiguous_timeout(self):
        m=api(); self.assertTrue(hasattr(m,'deliver'))
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); sent=[]
            messages=[dict(key=str(i),kind='text',text='test') for i in range(3)]
            def sender(message):
                sent.append(message['key'])
                if message['key']=='1': raise ConnectionError('ambiguous')
                return {'ok':True,'result':{'message_id':100+int(message['key'])}}
            m.deliver(store,'slot',messages,sender)
            self.assertEqual(sent,['0','1','2'])
            m.deliver(m.Store(store.path),'slot',messages,sender)
            self.assertEqual(sent,['0','1','2'])
            with store.connect() as c:
                self.assertEqual(c.execute("SELECT message_id FROM v2_delivery WHERE key='2'").fetchone()[0],102)
                self.assertEqual(c.execute("SELECT status FROM v2_delivery WHERE key='1'").fetchone()[0],'uncertain')

    def test_freshness_cycle_health_and_no_stale_snapshot(self):
        m=api(); self.assertTrue(hasattr(m,'run_cycle'))
        import json
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            packet=dict(market_date='2026-09-18',asof='09:05:00',stocks=[],depth=[])
            self.assertEqual(m.run_cycle(store,now,lambda:packet,{},d),[])
            health=json.loads(m.Path(d,'runtime_health.json').read_text())
            self.assertEqual(health['status'],'stale'); self.assertFalse(health['session_open']); self.assertEqual(store.points(now.date()),[])
            self.assertEqual(m.run_cycle(store,now.replace(minute=0),lambda:packet,{},d),[])

    def test_per_stock_incident_format_and_historical_cadence(self):
        m=api()
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,10,tzinfo=m.TEHRAN)
            stocks=[dict(symbol='الف',score=1,trade_volume=1,eligible_market_stock=True,industry='بانک',trade_value_toman=1),dict(symbol='ب',score=99,trade_volume=1,eligible_market_stock=True,industry='بانک',trade_value_toman=1)]
            summary=m.summarize(stocks,now.date()); summary['historical']=True
            store.save(now,stocks,summary)
            self.assertIn('by_symbol',store.incident(now))
            self.assertEqual(store.incident(now)['by_symbol']['الف']['median'],1)
            self.assertEqual(m.fmt(1.234),'\u200e1.2\u200e')
            self.assertEqual(m.fmt(.1234,percent=True),'\u200e12.3%\u200e')
            self.assertEqual(len(m.chart_series(store,now,summary)['times']),1)

    def test_metadata_adapter_and_feed_dual_market_dates(self):
        m=api(); self.assertTrue(hasattr(m,'convert_metadata'))
        meta=m.convert_metadata({'123':{'info':{'sector':{'cSecVal':'57','lSecVal':'بانکها'},'flow':4,'cgrValCotTitle':'بازار پایه زرد'},'first_trade_date':'2026-09-01'}})
        self.assertEqual(meta['123']['market'],'base'); self.assertEqual(meta['123']['industry'],'بانک')
        self.assertEqual(meta['123']['first_trade_date'],'2026-09-01')
        class Response:
            text='a@b@s1;s2@d1'
            def raise_for_status(self): pass
            def json(self): return {'marketOverview':{'marketActivityDEven':20260919,'marketActivityHEven':90500}}
        class Session:
            def get(self,*a,**kw): return Response()
        packet=m.fetch_market_data(Session())
        self.assertEqual(packet['market_date'],'2026-09-19'); self.assertEqual(packet['asof'],'09:05:00')
        self.assertEqual(packet['stocks'],['s1','s2'])

    def test_legacy_reports_remain_separate_and_uncapped_new_data(self):
        m=api(); self.assertTrue(hasattr(m,'legacy_reports'))
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            rows=[dict(symbol='فولاد',score=200,trade_volume=1,queue_value=0,queue_side=None,eligible_market_stock=True)]
            messages=m.legacy_reports(store,rows,now,d)
            self.assertIn('#لیدر',messages[1]['text']); self.assertEqual(rows[0]['score'],200)
            self.assertEqual(messages[0]['key'],'legacy_leveraged_text')

    @unittest.skipUnless(os.path.exists('real_sample/manifest.json') and os.path.exists('metadata_verified.json'),'external real-data integration fixtures unavailable')
    def test_cli_preview_real_archive_no_send(self):
        m=api(); self.assertTrue(hasattr(m,'cli'))
        with tempfile.TemporaryDirectory() as d:
            result=m.cli(['--preview','real_sample/manifest.json','--metadata','metadata_verified.json','--db',os.path.join(d,'db'),'--output',d])
            self.assertEqual(result,0)
            import json
            messages=json.loads(m.Path(d,'messages.json').read_text(encoding='utf-8'))
            self.assertEqual(len(messages),5); self.assertIn('12:20',messages[0]['text'])
            self.assertIn('تاریخی',messages[0]['text'])
            self.assertNotIn('Incident سی',messages[0]['text'])
            points=m.Store(os.path.join(d,'db')).points(date(2026,9,19))
            self.assertEqual(points[-1][1]['count'],747)
            self.assertAlmostEqual(points[-1][1]['median'],-3.0674345,places=6)

    def test_tick_finalizes_only_after_close_and_heartbeat(self):
        m=api(); self.assertTrue(hasattr(m,'tick'))
        import json
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,13,0,tzinfo=m.TEHRAN)
            f=['0']*26
            for i,v in {0:'123',1:'IRO1TEST0001',2:'سهم',3:'شرکت',6:'101',7:'102',9:'1',10:'1000',13:'100',15:'4',18:'57',19:'103',20:'97',22:'309',25:'P1'}.items(): f[i]=v
            packet=dict(market_date='2026-09-19',asof='12:30:00',stocks=[','.join(f)],depth=[],closed=True)
            m.tick(store,now,lambda:packet,{},d)
            self.assertEqual(store.turnover_metrics(date(2026,9,20)),(None,None,1)); self.assertEqual(store.points(now.date()),[])
            with store.connect() as c: self.assertEqual(c.execute('SELECT turnover FROM v2_days').fetchone()[0],100)
            health=json.loads(m.Path(d,'runtime_health.json').read_text()); self.assertEqual(health['status'],'closed')
            m.tick(store,now.replace(hour=9,minute=6),lambda:packet,{},d)
            health=json.loads(m.Path(d,'runtime_health.json').read_text()); self.assertEqual(health['checked_at'],'2026-09-19T09:06:00+03:30')
            parsed=m.parse_market_data(packet['stocks'],[])[0]; self.assertEqual(parsed['market'],'base'); self.assertEqual(parsed['close_price'],101)

    def test_legacy_detail_retention_deletes_expired_raw_only(self):
        m=api()
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,13,0,tzinfo=m.TEHRAN)
            old=m.Path(d,'raw_tse','20260101T090500_stocks.json.gz'); old.parent.mkdir(); old.write_bytes(b'old')
            new=old.with_name('20260919T090500_stocks.json.gz'); new.write_bytes(b'new')
            with store.connect() as c:
                c.executescript("CREATE TABLE history(symbol TEXT,score REAL,timestamp TEXT); INSERT INTO history VALUES ('a',1,'2026-01-01T09:05:00');")
            store.prune(now)
            self.assertFalse(old.exists()); self.assertTrue(new.exists())
            with store.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM history').fetchone()[0],0)

    def test_explicit_live_cli_one_tick_and_telegram_adapter(self):
        m=api(); self.assertTrue(hasattr(m,'serve')); self.assertTrue(hasattr(m,'TelegramSender'))
        class Session:
            def __init__(self): self.calls=[]
            def post(self,url,**kw):
                self.calls.append((url,kw))
                class Response:
                    def json(self): return {'ok':True,'result':{'message_id':77}}
                    def raise_for_status(self): pass
                return Response()
        session=Session(); sender=m.TelegramSender('fake-test-token','test-chat',session=session)
        self.assertEqual(sender(dict(kind='text',text='unit test'))['result']['message_id'],77)
        self.assertEqual(session.calls[0][1]['json']['chat_id'],'test-chat')
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,6,tzinfo=m.TEHRAN)
            packet=dict(market_date='2026-09-18',asof='12:30:00',stocks=[],depth=[],closed=True)
            m.serve(store,{},d,fetcher=lambda:packet,clock=lambda:now,sender=None,once=True)
            self.assertTrue(m.Path(d,'runtime_health.json').exists())

    @unittest.skipUnless(os.path.exists('real_sample/manifest.json') and os.path.exists('metadata_verified.json'),'external real-data integration fixtures unavailable')
    def test_polished_text_and_real_industry_membership(self):
        m=api()
        import gzip,json
        root=m.Path('real_sample'); entry=json.loads((root/'manifest.json').read_text())[-1]
        metadata=m.convert_metadata(json.loads(m.Path('metadata_verified.json').read_text(encoding='utf-8')))
        with gzip.open(root/entry['stocks'],'rt',encoding='utf-8') as f: raw=json.load(f)
        with gzip.open(root/entry['depth'],'rt',encoding='utf-8') as f: depth=json.load(f)
        stocks=m.parse_market_data(raw,depth,metadata); now=datetime.fromisoformat(entry['timestamp'])
        summary=m.summarize(stocks,now.date()); summary['historical']=True
        self.assertEqual(summary['industries'],[])
        self.assertAlmostEqual(summary['buy_toman'],3893981950054.7,places=1)
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); store.save(now,stocks,summary)
            messages=m.generate_reports(store,now,summary,d,True)
            self.assertIn('4\u200e همت',messages[0]['text']); self.assertIn('میانه نمره بازار',messages[0]['text'])
            self.assertIn('میانه غیرمنفی',messages[2]['text'])

    def test_metadata_refresh_is_bounded_durable_and_backed_off(self):
        m=api(); self.assertTrue(hasattr(m,'refresh_metadata'))
        import json
        class Session:
            def get(self,url,**kw):
                class Response:
                    text='20260901@1@1@1@1@1@1@1@1@1;20260919@1@1@1@1@1@1@1@1@1'
                    def raise_for_status(self): pass
                    def json(self): return {'instrumentInfo':{'flow':4,'sector':{'cSecVal':'57'},'flowTitle':'بازار پایه'}}
                return Response()
        with tempfile.TemporaryDirectory() as d:
            path=m.Path(d,'metadata.json'); now=datetime(2026,9,19,13,0,tzinfo=m.TEHRAN)
            candidates=[{'instrument':str(i),'symbol':'سهم','eligible_market_stock':True} for i in range(10)]
            result=m.refresh_metadata(path,candidates,now,limit=2,session=Session())
            self.assertEqual(len(result),2); self.assertEqual(result['0']['first_trade_date'],'2026-09-01')
            result=m.refresh_metadata(path,candidates[:2],now,limit=2,session=Session())
            self.assertEqual(len(result),2); self.assertTrue(result['0']['history_complete'])
            self.assertEqual(m.convert_metadata(result)['0']['market'],'base')

    def test_live_cli_has_explicit_send_gate(self):
        m=api()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d, patch.object(m,'serve') as serve:
            result=m.cli(['--run','--once','--db',os.path.join(d,'db'),'--output',d])
            self.assertEqual(result,0); self.assertIsNone(serve.call_args.kwargs['sender'])

    def test_exact_price_boundary_nonfinite_final_and_corrupt_book(self):
        m=api()
        f=['0']*26
        for i,v in {0:'123',1:'IRO1TEST0001',2:'سهم',3:'شرکت',7:'102',9:'1',10:'1000',13:'100',15:'1',18:'57',19:'103',20:'97',22:'300',25:'N1'}.items(): f[i]=v
        stock=m.parse_market_data([','.join(f)],['123,1,0,0,0,0,0,0'])[0]
        self.assertEqual(m.stars(stock['score'],'median'),4)
        stock=m.parse_market_data([','.join(f)],['123,1,1,1,broken,0,100,0'])[0]
        self.assertIsNone(stock['score']); self.assertIsNone(stock['buy_queue_value_toman'])
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db'))
            self.assertFalse(store.finalize_day(date(2026,9,19),float('inf'),True,'official'))
            store.finalize_day(date(2026,9,19),10,True,'official'); store.finalize_day(date(2026,9,19),20,True,'official corrected')
            with store.connect() as c: self.assertEqual(c.execute('SELECT turnover FROM v2_days').fetchone()[0],20)

    def test_idle_runtime_does_not_fetch_and_market_runtime_refreshes(self):
        m=api()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); calls=[]
            def fetch():
                calls.append(1); return dict(market_date='2026-09-19',asof='09:06:00',stocks=[],depth=[],closed=False)
            with patch.object(m,'refresh_metadata',return_value={}) as refresh:
                m.serve(store,{},d,fetcher=fetch,clock=lambda:datetime(2026,9,19,22,0,tzinfo=m.TEHRAN),once=True)
                self.assertEqual(calls,[])
                m.serve(store,{},d,fetcher=fetch,clock=lambda:datetime(2026,9,19,9,6,tzinfo=m.TEHRAN),once=True,metadata_path=os.path.join(d,'metadata.json'))
                self.assertEqual(len(calls),1); self.assertTrue(refresh.called)

    def test_legacy_generation_is_idempotent_and_industry_has_incident(self):
        m=api()
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            rows=[dict(symbol='فولاد',score=2,trade_volume=1,queue_value=0,queue_side=None,eligible_market_stock=True,industry='فلزات اساسی',trade_value_toman=1)]
            first=m.legacy_reports(store,rows,now,d); second=m.legacy_reports(store,rows,now,d)
            with store.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM history').fetchone()[0],1)
            summary=m.summarize(rows,now.date()); store.save(now,rows,summary)
            messages=m.generate_reports(store,now,summary,d)
            self.assertIn('Incident',messages[2]['text']); self.assertIn('ناقص',messages[2]['text'])

    def test_rejected_delivery_retries_next_minute_with_original_payload(self):
        m=api(); self.assertTrue(hasattr(m,'retry_pending'))
        with tempfile.TemporaryDirectory() as d:
            store=m.Store(os.path.join(d,'db')); messages=[dict(key='a',kind='text',text='original')]
            m.deliver(store,'original-slot',messages,lambda msg:{'ok':False,'description':'429'})
            calls=[]
            def sender(msg): calls.append(msg['text']); return {'ok':True,'result':{'message_id':7}}
            m.retry_pending(store,sender)
            self.assertEqual(calls,['original']); m.retry_pending(store,sender); self.assertEqual(calls,['original'])

    def test_empty_industry_chart_has_no_fake_axes(self):
        m=api()
        import matplotlib.pyplot as plt
        from unittest.mock import patch
        original=plt.subplots; axes=[]
        def tracked(*a,**kw):
            fig,ax=original(*a,**kw); axes.append(ax); return fig,ax
        with tempfile.TemporaryDirectory() as d, patch.object(plt,'subplots',side_effect=tracked):
            store=m.Store(os.path.join(d,'db')); now=datetime(2026,9,19,9,5,tzinfo=m.TEHRAN)
            summary=m.summarize([],now.date()); store.save(now,[],summary)
            m.generate_reports(store,now,summary,d)
            self.assertFalse(axes[1].axison)
            self.assertTrue(axes[2][0].get_ylabel())

    def test_unknown_metadata_is_prioritized_before_cached_names(self):
        m=api()
        from unittest.mock import patch
        import json
        class Response:
            text='20260901@1@1@1@1@1@1@1@1@1'
            def raise_for_status(self): pass
            def json(self): return {'instrumentInfo':{'flow':1}}
        class Session:
            def __init__(self): self.urls=[]
            def get(self,url,**kw): self.urls.append(url); return Response()
        with tempfile.TemporaryDirectory() as d:
            path=m.Path(d,'meta.json'); path.write_text(json.dumps({'known':{'first_trade_date':'2000-01-01','info':{'flow':1},'checked_at':'2026-09-19T08:00:00+03:30'}}))
            session=Session(); rows=[dict(instrument=k,symbol=k,eligible_market_stock=True) for k in ['known','new']]
            result=m.refresh_metadata(path,rows,datetime(2026,9,19,9,0,tzinfo=m.TEHRAN),limit=1,session=session)
            self.assertIn('new',result); self.assertIn('/new',session.urls[0])

    def test_missing_market_values_remain_unavailable(self):
        m=api(); s=m.summarize([],date(2026,9,19),100)
        self.assertIsNone(s['buy_toman']); self.assertIsNone(s['sell_toman']); self.assertIsNone(s['turnover_toman'])
        row=dict(symbol='الف',score=None,trade_volume=0,eligible_market_stock=True,book_valid=False,trade_value_toman=1)
        s=m.summarize([row],date(2026,9,19),100)
        self.assertIsNone(s['queue_ratio'])

if __name__=='__main__': unittest.main()
