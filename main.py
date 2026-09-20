"""Revised Bourse reporting. Importing this module performs no I/O."""
import math

def tick(store,now,fetcher,metadata,output_dir,sender=None,include_legacy=True):
    """One bounded poll. Non-slot polls are health/final-turnover only."""
    now=now.astimezone(TEHRAN); health_path=Path(store.path).parent/'runtime_health.json'
    try:
        packet=fetcher()
        source_date=packet['market_date']
        observed=datetime.fromisoformat(source_date+'T'+packet['asof']).replace(tzinfo=TEHRAN)
        in_window=now.weekday() not in (3,4) and (9,5)<=(now.hour,now.minute)<=(12,25)
        fresh=source_date==str(now.date()) and -60<=(now-observed).total_seconds()<=300
        confirmed_closed=fresh and packet.get('closed') is True
        status='fresh' if in_window and fresh and not confirmed_closed else 'closed' if confirmed_closed or not in_window else 'stale'
        health=dict(checked_at=now.isoformat(),market_date=source_date,source_asof=observed.isoformat(),session_open=status=='fresh',status=status)
        temp=health_path.with_suffix('.tmp'); temp.write_text(json.dumps(health),encoding='utf-8'); temp.replace(health_path)
        logging.info('TSETMC market data fetched; market_date=%s status=%s',source_date,status)
        slot=now.replace(second=0,microsecond=0)
        messages=[]
        if status=='fresh' and slot in report_slots(now.date()):
            messages=run_cycle(store,now,lambda:packet,metadata,output_dir,sender)
            if include_legacy:
                try:
                    stocks=parse_market_data(packet['stocks'],packet.get('depth'),metadata)
                    legacy=legacy_reports(store,stocks,slot,output_dir)
                    if sender: deliver(store,slot.isoformat(),legacy,sender)
                except Exception: logging.exception('Legacy reports failed independently')
        # Never derive complete daily turnover from the 12:25 snapshot. Only
        # independently fetched post-close cumulative ordinary-share turnover.
        if now.hour>=13 and source_date==str(now.date()) and packet['asof']>='12:30:00' and now.weekday() not in (3,4):
            stocks=parse_market_data(packet['stocks'],[],metadata)
            ordinary=[s for s in stocks if s['eligible_market_stock']]
            if ordinary:
                store.finalize_day(now.date(),sum(s['trade_value_toman'] for s in ordinary),verified=True,source='post-close MarketWatch ordinary shares; overview date verified; '+now.isoformat())
        store.prune(now)
        return messages
    except Exception:
        health_path.write_text(json.dumps(dict(checked_at=now.isoformat(),market_date=None,session_open=False,status='error')),encoding='utf-8')
        raise


def refresh_metadata(path,candidates,now,limit=4,session=None):
    """Bounded concurrent cache work, invoked AFTER reporting, never per symbol inline."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote
    import requests
    path=Path(path); cache=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    due=[]
    for row in sorted(candidates,key=lambda r:bool(cache.get(r['instrument'],{}).get('first_trade_date'))):
        if not row.get('eligible_market_stock'): continue
        entry=cache.get(row['instrument'],{})
        retry=entry.get('retry_after')
        if not retry and entry.get('first_trade_date') and entry.get('info') and entry.get('checked_at'):
            retry=(datetime.fromisoformat(entry['checked_at'])+timedelta(days=7)).isoformat()
        if retry and datetime.fromisoformat(retry)>now: continue
        due.append(row)
        if len(due)>=limit: break
    def worker(row):
        instrument=row['instrument']; entry=dict(cache.get(instrument,{})); client=session or requests.Session()
        try:
            response=client.get('https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/'+instrument,timeout=10); response.raise_for_status()
            entry['info']=response.json()['instrumentInfo']
        except Exception:
            try:
                response=client.get('https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/'+quote(row['symbol']),timeout=10); response.raise_for_status()
                entry['info']=next(r for r in response.json()['instrumentSearch'] if str(r['insCode'])==instrument)
            except Exception: pass
        if not entry.get('first_trade_date'):
            try:
                url=f'https://old.tsetmc.com/tsev2/data/InstTradeHistory.aspx?i={instrument}&Top=9999&A=0'
                response=client.get(url,timeout=15); response.raise_for_status()
                rows=[r.split('@') for r in response.text.split(';') if r.strip()]
                dates=[]
                for r in rows:
                    if len(r)!=10: raise ValueError('history schema changed')
                    day=datetime.strptime(r[0],'%Y%m%d').date()
                    if float(r[8])>0 and float(r[9])>0: dates.append(day)
                if dates and len(rows)<9999:
                    entry.update(first_trade_date=str(min(dates)),history_complete=True,history_source=url)
            except Exception: entry['history_complete']=False
        good=bool(entry.get('info')) and bool(entry.get('first_trade_date'))
        entry.update(checked_at=now.isoformat(),retry_after=(now+timedelta(days=7 if good else 1)).isoformat())
        return instrument,entry
    with ThreadPoolExecutor(max_workers=max(1,min(limit,4))) as pool:
        for instrument,entry in pool.map(worker,due): cache[instrument]=entry
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8'); temp.replace(path)
    return cache

def convert_metadata(cache):
    result={}
    for instrument,entry in cache.items():
        if not isinstance(entry,dict): continue
        info=entry.get('info',{}); sector=info.get('sector') or {}
        title=normalize(info.get('flowTitle','')+' '+info.get('cgrValCotTitle',''))
        code=str(sector.get('cSecVal','')).strip().zfill(2)
        result[instrument]=dict(entry,industry=INDUSTRIES.get(code,entry.get('industry')),market='base' if 'پایه' in title else 'main' if info.get('flow') in (1,2) else entry.get('market'),first_trade_date=entry.get('first_trade_date'))
    return result

def fetch_market_data(session=None):
    import requests
    session=session or requests.Session()
    overview=[]
    for market in (1,2):
        response=session.get(f'https://cdn.tsetmc.com/api/MarketData/GetMarketOverview/{market}',timeout=15); response.raise_for_status()
        overview.append(response.json()['marketOverview'])
    dates=[str(o['marketActivityDEven']) for o in overview]
    if len(set(dates))!=1: raise ValueError('market dates disagree')
    market_date=datetime.strptime(dates[0],'%Y%m%d').date().isoformat()
    asof=min(str(o['marketActivityHEven']).zfill(6) for o in overview)
    response=session.get('https://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx?h=0&r=0',headers={'User-Agent':'Mozilla/5.0'},timeout=20); response.raise_for_status()
    parts=response.text.split('@')
    if len(parts)<4 or not parts[2]: raise ValueError('unexpected MarketWatch structure')
    return dict(market_date=market_date,asof=f'{asof[:2]}:{asof[2:4]}:{asof[4:]}',stocks=parts[2].split(';'),depth=parts[3].split(';'),closed=all(o.get('marketState')=='F' for o in overview))

def decorate_report(text):
    """Add stable visual markers without changing report data or RTL marks."""
    prefixes = {
        '#وضعیت_بازار': '📊', '#صنایع_برتر': '🏭',
        '#روند_بازار': '📈', '#روند_صنایع': '🏭', '#تمایل_بازار': '⚖️',
        '#اهرمی': '🚀', '#لیدر': '👑',
        'میانه نمره بازار:': '🎯', 'روند ارزش معاملات': '💹',
        'خالص صف /': '⚖️', 'صف خرید:': '🟢',
        'تمایل بازار به': '🧭', 'سهام معامله‌شده:': '🔎',
        'میانگین ': '📊', 'صنعتی با میانه غیرمنفی': 'ℹ️',
    }
    lines = []
    for line in text.split('\n'):
        for prefix, icon in prefixes.items():
            if line.startswith(prefix) or line.startswith('<b>' + prefix):
                line = icon + ' ' + line
                break
        else:
            if len(line) >= 10 and line[:4].isdigit() and line[4] == '-':
                line = '🕒 ' + line
            elif line.startswith('نمونه تاریخی — '):
                line = '🕒 ' + line
            elif ' | میانه ' in line:
                line = '🏭 ' + line
        line = line.replace('| صف فروش:', '| 🔴 صف فروش:')
        line = line.replace('| روز کامل پیشین:', '| 🗓 روز کامل پیشین:')
        lines.append(line)
    return '\n'.join(lines)


def legacy_reports(store,stocks,now,output_dir):
    """Keep deployed step3 rendering isolated; never apply its filters to v2."""
    with store.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS v2_legacy(slot TEXT PRIMARY KEY,payload TEXT)')
        cached=c.execute('SELECT payload FROM v2_legacy WHERE slot=?',(now.isoformat(),)).fetchone()
    if cached: return json.loads(cached[0])
    import importlib.util, gc
    spec=importlib.util.spec_from_file_location('_legacy_bourse',Path(__file__).with_name('deployed_main.py'))
    legacy=importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
    legacy.DB_PATH=store.path
    legacy.init_db()
    valid=[dict(s) for s in stocks if s.get('score') is not None]
    funds=sorted([s for s in valid if s['symbol'] in LEVERAGED_FUNDS],key=lambda s:-s['score'])
    leaders=sorted([s for s in valid if s['symbol'] in LEADERS],key=lambda s:-s['score'])
    messages=[]
    for key,title,rows,limit in [('leveraged','#اهرمی',funds,None),('leaders','#لیدر',leaders,10)]:
        messages.append(dict(key='legacy_'+key+'_text',kind='text',text=legacy.build_group_message(title,rows,now,limit),parse_mode='HTML'))
    legacy.save_scores(valid,now)
    for key,title,rows in [('leveraged','#اهرمی',legacy.top_leveraged_for_chart(funds)),('leaders','#لیدر',legacy.top_leaders_for_chart(leaders))]:
        chart=legacy.create_score_chart(title,rows,now,str(Path(output_dir)/(key+'.png')))
        if chart: messages.append(dict(key='legacy_'+key+'_photo',kind='photo',path=chart,caption=title))
    for message in messages:
        field = 'text' if message['kind'] == 'text' else 'caption'
        message[field] = decorate_report(message.get(field, ''))
    with store.connect() as c: c.execute('INSERT OR REPLACE INTO v2_legacy VALUES (?,?)',(now.isoformat(),json.dumps(messages,ensure_ascii=False)))
    gc.collect()
    return messages

import logging

class RejectedDelivery(Exception):
    """Provider explicitly rejected; safe to retry, unlike transport ambiguity."""

def deliver(store,slot,messages,sender):
    import hashlib,shutil
    for message in messages:
        key=message['key']
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT status FROM v2_delivery WHERE slot=? AND key=?',(slot,key)).fetchone()
            if row and row[0] in ('sent','sending','uncertain'): continue
            saved=c.execute('SELECT payload FROM v2_outbox WHERE slot=? AND key=?',(slot,key)).fetchone()
            if saved: message=json.loads(saved[0])
            else:
                message=dict(message)
                if message['kind']=='photo':
                    assets=Path(store.path).parent/'delivery_assets'; assets.mkdir(exist_ok=True)
                    target=assets/(hashlib.sha256((slot+key).encode()).hexdigest()+'.png')
                    shutil.copyfile(message['path'],target); message['path']=str(target)
                c.execute('INSERT INTO v2_outbox VALUES (?,?,?)',(slot,key,json.dumps(message,ensure_ascii=False)))
            c.execute('INSERT OR REPLACE INTO v2_delivery VALUES (?,?,?,NULL,NULL)',(slot,key,'sending'))
        try:
            response=sender(message)
            if not response.get('ok'): raise RejectedDelivery(str(response.get('description','rejected')))
            message_id=response['result']['message_id']
            with store.connect() as c: c.execute('UPDATE v2_delivery SET status=?,message_id=?,error=NULL WHERE slot=? AND key=?',('sent',message_id,slot,key))
        except Exception as error:
            status='retry' if isinstance(error,RejectedDelivery) else 'uncertain'
            with store.connect() as c: c.execute('UPDATE v2_delivery SET status=?,error=? WHERE slot=? AND key=?',(status,type(error).__name__,slot,key))
            logging.warning('Delivery %s %s: %s',key,status,type(error).__name__)
    with store.connect() as c: return c.execute('SELECT key,status,message_id FROM v2_delivery WHERE slot=?',(slot,)).fetchall()

def retry_pending(store,sender):
    with store.connect() as c:
        rows=c.execute("SELECT o.slot,o.payload FROM v2_outbox o JOIN v2_delivery d ON o.slot=d.slot AND o.key=d.key WHERE d.status='retry' ORDER BY o.slot").fetchall()
    for slot,payload in rows: deliver(store,slot,[json.loads(payload)],sender)

def run_cycle(store,now,fetcher,metadata,output_dir,sender=None):
    now=now.astimezone(TEHRAN); slot=now.replace(second=0,microsecond=0)
    if slot not in report_slots(now.date()): return []
    health_path=Path(store.path).parent/'runtime_health.json'
    try:
        packet=fetcher()
        source_date=packet['market_date']; source_time=datetime.fromisoformat(source_date+'T'+packet['asof']).replace(tzinfo=TEHRAN)
        fresh=source_date==str(now.date()) and -60<=(now-source_time).total_seconds()<=300
        health=dict(checked_at=now.isoformat(),market_date=source_date,source_asof=source_time.isoformat(),session_open=fresh,status='fresh' if fresh else 'stale')
        temp=health_path.with_suffix('.tmp'); temp.write_text(json.dumps(health),encoding='utf-8'); temp.replace(health_path)
        logging.info('Successfully fetched market data; market_date=%s status=%s',source_date,health['status'])
        if not fresh: return []
        stocks=parse_market_data(packet['stocks'],packet.get('depth'),metadata)
        if not stocks: raise ValueError('empty parsed feed')
        x,avg,n=store.turnover_metrics(now.date()); summary=summarize(stocks,now.date(),avg)
        summary.update(turnover_x=x,completed_days=n)
        # Once captured, a slot is immutable across send retries and restarts.
        previous=dict(store.points(now.date())).get(slot)
        if previous is None: store.save(slot,stocks,summary,packet)
        else: summary=previous
        store.prune(now)
        messages=generate_reports(store,slot,summary,output_dir)
        if sender: deliver(store,slot.isoformat(),messages,sender)
        return messages
    except Exception:
        health_path.write_text(json.dumps(dict(checked_at=now.isoformat(),market_date=None,session_open=False,status='error')),encoding='utf-8')
        raise


def repair_legacy_summaries(store):
    with store.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'stock_snapshots','daily_stock_summary'}<=tables: return 0
        groups=defaultdict(list)
        for day,symbol,score in c.execute('SELECT trading_date,symbol,score FROM stock_snapshots WHERE valid=1 AND score IS NOT NULL ORDER BY timestamp'):
            groups[(day,symbol)].append(score)
        changed=0
        for (day,symbol),values in groups.items():
            changed+=c.execute('UPDATE daily_stock_summary SET median_score=?,last_score=? WHERE trading_date=? AND symbol=?',(statistics.median(values),values[-1],day,symbol)).rowcount
    return changed

def replay_archive(store,manifest_path,metadata=None):
    import gzip
    manifest_path=Path(manifest_path); manifest=json.loads(manifest_path.read_text(encoding='utf-8')); count=0
    for entry in sorted(manifest,key=lambda r:r['timestamp']):
        now=datetime.fromisoformat(entry['timestamp']).astimezone(TEHRAN)
        if now.hour<9 or (now.hour,now.minute)>(12,25): continue
        with gzip.open(manifest_path.parent/entry['stocks'],'rt',encoding='utf-8') as f: raw=json.load(f)
        with gzip.open(manifest_path.parent/entry['depth'],'rt',encoding='utf-8') as f: depth=json.load(f)
        stocks=parse_market_data(raw,depth,metadata)
        x,avg,n=store.turnover_metrics(now.date()); summary=summarize(stocks,now.date(),avg)
        summary.update(turnover_x=x,completed_days=n,historical=True)
        store.save(now,stocks,summary,dict(stocks=raw,depth=depth)); count+=1
    return count


def fmt(value, decimals=1, percent=False):
    return 'داده کافی نیست' if value is None else f"\u200e{value*(100 if percent else 1):.{decimals}f}{'%' if percent else ''}\u200e"

def chart_series(store,now,summary):
    points=dict(store.points(now.date()))
    times=sorted(set(t for t in report_slots(now.date()) if t<=now)|set(t for t in points if t<=now))
    if summary.get('historical'):
        times=sorted(t for t in points if t<=now)
    out={'times':times}
    for key in ('median','leader_median','nonleader_median','queue_ratio'):
        out[key]=[points.get(t,{}).get(key) for t in times]
    out['turnover_x']=[summary.get('turnover_x') if t in points else None for t in times]
    out['industries']={g['industry']:[next((h['median'] for h in points.get(t,{}).get('all_industries',[]) if h['industry']==g['industry']),None) for t in times] for g in summary['industries']}
    return out

def generate_reports(store,now,summary,output_dir,historical=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import arabic_reshaper
    from bidi.algorithm import get_display
    def rtl(s): return get_display(arabic_reshaper.reshape(str(s)))
    output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    stamp=('نمونه تاریخی — ' if historical else '')+now.strftime('%Y-%m-%d %H:%M')+' تهران'
    series=chart_series(store,now,summary); times=series['times']
    incident=store.incident(now); position,label=allocation(summary['leader_median'],summary['nonleader_median'])
    def rating(key,kind):
        n=stars(summary.get(key),kind)
        return '—' if n is None else '⭐'*n+'☆'*(5-n)
    text='\n'.join(['#وضعیت_بازار',stamp,f"میانه نمره بازار: {fmt(summary['median'])} {rating('median','median')}",f"روند ارزش معاملات MA5/MA15−1: {fmt(summary.get('turnover_x'),percent=True)} {rating('turnover_x','turnover')}",f"خالص صف / میانگین ۱۵ روز: {fmt(summary['queue_ratio'],percent=True)} {rating('queue_ratio','queue')}",f"صف خرید: {fmt(summary['buy_toman']/1e12 if summary['buy_toman'] is not None else None,0)} همت | صف فروش: {fmt(summary['sell_toman']/1e12 if summary['sell_toman'] is not None else None,0)} همت",f"تمایل بازار به لیدرها / هم‌وزن: {label}",f"سهام معامله‌شده: {summary['count']} | روز کامل پیشین: {summary.get('completed_days',0)}/15"])
    colors=['#1565c0','#c62828','#2e7d32','#6a1b9a','#ef6c00','#00838f','#4527a0','#ad1457','#558b2f','#283593','#d84315','#00695c','#8e24aa','#5d4037','#0277bd','#9e9d24']
    def plot(ax,values,label,color):
        ax.set_title(rtl(label))
        if not any(v is not None for v in values):
            ax.set_yticks([]); ax.set_xticks([])
            ax.text(.5,.5,rtl('داده کافی نیست — نیاز به ۱۵ روز کامل'),transform=ax.transAxes,ha='center',fontsize=16)
            return
        ax.plot(times,[float('nan') if v is None else v for v in values],label=rtl(label),color=color,marker='o',markersize=4,linewidth=2)
        if times:
            ticks=sorted(set([times[0],times[-1]]+[t for t in times if t.minute in (0,30)]))
            ax.set_xticks(ticks)

        ax.grid(alpha=.25); ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=TEHRAN))
        ax.set_xlim(now.replace(hour=9,minute=0),now.replace(hour=12,minute=25))
        ax.legend(loc='best'); ax.set_xlabel(rtl('زمان تهران'))
        if not any(v is not None for v in values): ax.text(.5,.5,rtl('داده کافی نیست'),transform=ax.transAxes,ha='center')
    plt.rcParams['font.family']='DejaVu Sans'
    fig,axes=plt.subplots(3,1,figsize=(15,12),layout='constrained')
    for ax,key,title in zip(axes,['turnover_x','median','queue_ratio'],['روند ارزش معاملات — ثابت روزانه','میانه نمره بازار','نسبت خالص صف به میانگین ارزش معاملات ۱۵ روز']):
        values=series[key] if key=='median' else [None if v is None else v*100 for v in series[key]]
        plot(ax,values,title,colors[0])
        ax.set_ylabel(rtl('نمره' if key=='median' else 'درصد'))

    fig.suptitle(rtl(stamp)); p1=output_dir/'market_panels.png'; fig.savefig(p1,dpi=150); plt.close(fig)
    fig,ax=plt.subplots(figsize=(15,7),layout='constrained')
    for industry,values in series['industries'].items(): plot(ax,values,industry,colors[list(INDUSTRIES.values()).index(industry)])
    if not series['industries']:
        ax.set_axis_off()
        ax.text(.5,.5,rtl('صنعتی با میانه غیرمنفی وجود ندارد'),ha='center',transform=ax.transAxes,fontsize=18)
    ax.set_title(rtl('پنج صنعت برتر فعلی — تاریخچه کامل روز | '+stamp)); p2=output_dir/'industries.png'; fig.savefig(p2,dpi=150); plt.close(fig)
    fig,(ax,gauge)=plt.subplots(2,1,figsize=(15,8),gridspec_kw={'height_ratios':[5,1]},layout='constrained')
    plot(ax,series['leader_median'],'میانه لیدرها',colors[0]); plot(ax,series['nonleader_median'],'میانه غیرلیدرها',colors[1]); ax.set_title(rtl(stamp))
    ax.set_ylabel(rtl('نمره'))
    gauge.set_xlim(.5,5.5); gauge.set_ylim(0,1); gauge.set_yticks([])
    gauge.set_xticks(range(1,6),[rtl(x) for x in ['هم‌وزن شدید','هم‌وزن نسبی','خنثی','لیدر نسبی','لیدر شدید']]); gauge.scatter(range(1,6),[.5]*5,c='#dddddd',s=250)
    if position: gauge.scatter([position],[.5],c='#1565c0',s=300)
    gauge.set_title(rtl('تمایل بازار به لیدرها / هم‌وزن — '+label)); p3=output_dir/'allocation.png'; fig.savefig(p3,dpi=150); plt.close(fig)
    industry_lines=['#صنایع_برتر',stamp]
    for g in summary['industries']:
        industry_lines.append(f"{g['industry']} | میانه {fmt(g['median'])} | {g['count']} سهم")
        for i,s in enumerate(g['stocks'],1):
            inc=incident['by_symbol'].get(s['symbol'],{})
            market={'base':'پایه','main':'اصلی'}.get(s.get('market'),'نامشخص')
            industry_lines.append(f"{i}. {s['symbol']} | {fmt(s['score'])} | {market} | Incident {fmt(inc.get('median'))} ({inc.get('count',0)} نمره؛ {'ناقص' if inc.get('partial',True) else 'کامل'})")
    if not summary['industries']: industry_lines.append('صنعتی با میانه غیرمنفی وجود ندارد')
    messages=[dict(key='market_text',kind='text',text=text),dict(key='market_panels',kind='photo',path=str(p1),caption='#روند_بازار '+stamp),dict(key='industry_text',kind='text',text='\n'.join(industry_lines)),dict(key='industries',kind='photo',path=str(p2),caption='#روند_صنایع '+stamp),dict(key='allocation',kind='photo',path=str(p3),caption='#تمایل_بازار '+stamp)]
    for message in messages:
        field = 'text' if message['kind'] == 'text' else 'caption'
        message[field] = decorate_report(message.get(field, ''))
    (output_dir/'messages.json').write_text(json.dumps(messages,ensure_ascii=False,indent=2),encoding='utf-8')
    return messages

import sqlite3
import json
from pathlib import Path

class Store:
    """Versioned tables never reinterpret unverified legacy day totals as complete."""
    def __init__(self,path):
        self.path=str(path); Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS v2_slots(ts TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS v2_detail(ts TEXT,symbol TEXT,payload TEXT,PRIMARY KEY(ts,symbol));
            CREATE TABLE IF NOT EXISTS v2_raw(ts TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS v2_outbox(slot TEXT,key TEXT,payload TEXT,PRIMARY KEY(slot,key));
            CREATE TABLE IF NOT EXISTS v2_days(day TEXT PRIMARY KEY,turnover REAL,source TEXT,complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS v2_daily_scores(day TEXT,symbol TEXT,payload TEXT,PRIMARY KEY(day,symbol));
            CREATE TABLE IF NOT EXISTS v2_delivery(slot TEXT,key TEXT,status TEXT,message_id INTEGER,error TEXT,PRIMARY KEY(slot,key));''')
    from contextlib import contextmanager
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30)
        try:
            with c: yield c
        finally: c.close()
    def save(self,now,stocks,summary,raw=None):
        ts=now.isoformat()
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO v2_slots VALUES (?,?)',(ts,json.dumps(summary,ensure_ascii=False)))
            c.executemany('INSERT OR REPLACE INTO v2_detail VALUES (?,?,?)',[(ts,s['symbol'],json.dumps(s,ensure_ascii=False)) for s in stocks])
            if raw is not None: c.execute('INSERT OR REPLACE INTO v2_raw VALUES (?,?)',(ts,json.dumps(raw,ensure_ascii=False)))
        daily=self.daily_scores(now.date())
        with self.connect() as c:
            c.executemany('INSERT OR REPLACE INTO v2_daily_scores VALUES (?,?,?)',[(str(now.date()),symbol,json.dumps(values)) for symbol,values in daily.items()])
    def points(self,day):
        with self.connect() as c: rows=c.execute('SELECT ts,payload FROM v2_slots WHERE substr(ts,1,10)=? ORDER BY ts',(str(day),)).fetchall()
        return [(datetime.fromisoformat(ts),json.loads(p)) for ts,p in rows]
    def daily_scores(self,day):
        with self.connect() as c: rows=c.execute('SELECT payload FROM v2_detail WHERE substr(ts,1,10)=? ORDER BY ts',(str(day),)).fetchall()
        groups=defaultdict(list)
        for (p,) in rows:
            s=json.loads(p)
            if s.get('score') is not None: groups[s['symbol']].append(s['score'])
        return {k:dict(count=len(v),mean=statistics.mean(v),median=statistics.median(v),min=min(v),max=max(v),last=v[-1]) for k,v in groups.items()}
    def incident(self,now):
        start=datetime.combine(now.date()-timedelta(days=29),datetime.min.time(),TEHRAN)
        with self.connect() as c: rows=c.execute('SELECT ts,payload FROM v2_detail WHERE ts>=? AND ts<=?',(start.isoformat(),now.isoformat())).fetchall()
        scores=[]; slots=set(); by_symbol=defaultdict(list)
        for ts,p in rows:
            s=json.loads(p); dt=datetime.fromisoformat(ts)
            if (dt.hour,dt.minute)<(9,5) or (dt.hour,dt.minute)>(12,25): continue
            if s.get('eligible_market_stock') and s.get('trade_volume',0)>0 and s.get('score') is not None:
                scores.append(s['score']); slots.add(ts); by_symbol[s['symbol']].append(s['score'])
        expected=sum(len(report_slots(start.date()+timedelta(days=i))) for i in range(30))
        return dict(median=median(scores),count=len(scores),slots=len(slots),partial=len(slots)<expected,by_symbol={k:dict(median=median(v),count=len(v),partial=len(v)<expected) for k,v in by_symbol.items()})
    def finalize_day(self,day,turnover,verified=False,source=''):
        if not verified or not source or turnover is None or not math.isfinite(turnover) or turnover<0: return False
        with self.connect() as c:
            c.execute('INSERT INTO v2_days VALUES (?,?,?,1) ON CONFLICT(day) DO UPDATE SET turnover=excluded.turnover,source=excluded.source,complete=1',(str(day),turnover,source))
        return True
    def turnover_metrics(self,day):
        with self.connect() as c: rows=c.execute('SELECT turnover FROM v2_days WHERE day<? AND complete=1 ORDER BY day DESC LIMIT 15',(str(day),)).fetchall()
        values=[r[0] for r in rows]
        if len(values)<15: return None,None,len(values)
        avg=statistics.mean(values)
        return (statistics.mean(values[:5])/avg-1 if avg>0 else None),avg,len(values)
    def prune(self,now):
        cutoff=datetime.combine(now.date()-timedelta(days=29),datetime.min.time(),TEHRAN).isoformat()
        with self.connect() as c:
            for table in ('v2_detail','v2_raw'): c.execute(f'DELETE FROM {table} WHERE ts<?',(cutoff,))
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('history','stock_snapshots','raw_tse_snapshots','group_snapshots','industry_detail_snapshots'):
                if table in tables: c.execute(f'DELETE FROM {table} WHERE timestamp<?',(cutoff,))
        for path in (Path(self.path).parent/'raw_tse').glob('*.json.gz'):
            try: day=datetime.strptime(path.name[:15],'%Y%m%dT%H%M%S').date()
            except ValueError: continue
            if str(day)<cutoff[:10]: path.unlink()


import statistics
from collections import defaultdict
LEADERS = {'ذوب','فملی','فولاد','تاپیکو','شستا','شبریز','شتران','وغدیر','شپنا','شبندر','پالایش','خگستر','فارس','خودرو','وبصادر','وبملت','خساپا','دارا یکم','پارسان','وتجارت'}
LEVERAGED_FUNDS = {'اهرم','شتاب','موج','جهش','توان','نارنج','بیدار'}
INDUSTRIES = {'34':'خودرو','57':'بانک','44':'محصولات شیمیایی','13':'استخراج کانه‌های فلزی','39':'شرکت‌های چندرشته‌ای صنعتی','27':'فلزات اساسی','23':'فرآورده‌های نفتی','70':'انبوه‌سازی','56':'سرمایه‌گذاری','28':'محصولات فلزی','66':'بیمه','43':'محصولات دارویی','53':'سیمان','42':'محصولات غذایی','01':'زراعت','49':'کاشی'}

def normalize(text):
    return str(text or '').replace('ي','ی').replace('ك','ک').strip()

def is_common_stock(symbol, description, instrument_type=''):
    symbol,description=normalize(symbol),normalize(description)
    excluded=('صندوق','ص.س.','حق تقدم','اوراق','اختیار','آتی','گواهی سپرده','تسهیلات مسکن','اسناد خزانه','بلوک','عمده')
    return bool(symbol) and not symbol.endswith('ح') and not any(c.isdigit() for c in symbol) and not any(t in description for t in excluded)

def parse_market_data(stocks_raw, depth_raw, metadata=None):
    metadata=metadata or {}; depth=defaultdict(list); bad_books=set()
    for raw in depth_raw if isinstance(depth_raw,(list,tuple)) else []:
        if not isinstance(raw,str) or not raw.strip(): continue
        f=raw.split(',')
        try:
            if len(f)<8 or not f[0]: raise ValueError('malformed depth row')
            values=tuple(float(x) for x in f[4:8])
            if not all(math.isfinite(x) and x>=0 for x in values): raise ValueError('invalid depth value')
            depth[f[0]].append(values)
        except ValueError: bad_books.add(f[0])
    book_available=any(rows for instrument,rows in depth.items() if instrument not in bad_books)
    result=[]
    for raw in stocks_raw:
        f=raw.split(',')
        try:
            if len(f)<23: continue
            yesterday,last,volume,hi,lo=map(float,[f[13],f[7],f[9],f[19],f[20]])
            if yesterday<=0 or volume<0: continue
            symbol=normalize(f[2]); meta=metadata.get(f[0],{})
            rows=depth[f[0]]
            buy=sum(bv for bp,sp,bv,sv in rows if bp==hi and hi>0 and bv>0)
            sell=sum(sv for bp,sp,bv,sv in rows if sp==lo and lo>0 and sv>0)
            # A limit order is not a queue if immediately executable opposing orders exist.
            if any(sp>0 and sp<=hi and sv>0 for bp,sp,bv,sv in rows): buy=0
            if any(bp>=lo and bv>0 for bp,sp,bv,sv in rows): sell=0
            pct=(last-yesterday)*100/yesterday
            item=dict(instrument=f[0],symbol=symbol,description=normalize(f[3]),instrument_type=f[25] if len(f)>25 else '',yesterday_price=yesterday,last_price=last,trade_volume=volume,trade_value_toman=float(f[10])/10,last_pct=pct,buy_queue_volume=buy,sell_queue_volume=sell,buy_queue_value_toman=buy*hi/10,sell_queue_value_toman=sell*lo/10,score=calculate_score(pct,buy,sell,volume,(hi-yesterday)*100/yesterday,(lo-yesterday)*100/yesterday),industry=meta.get('industry') or INDUSTRIES.get(f[18].zfill(2)),market=meta.get('market') or ('base' if f[15]=='4' else 'main' if f[15] in ('1','2') else None),first_trade_date=meta.get('first_trade_date'))
            item['eligible_market_stock']=f[22] in {'300','303','309','313'} and f[1].startswith('IRO') and f[1].endswith('0001')
            item.update(yval=f[22],instrument_id=f[1],close_price=float(f[6]))
            item['queue_side']='buy' if buy else 'sell' if sell else None
            item['queue_value']=int((item['buy_queue_value_toman'] or item['sell_queue_value_toman'])/1e9)
            if not book_available or f[0] in bad_books:
                item.update(score=None,buy_queue_volume=None,sell_queue_volume=None,
                            buy_queue_value_toman=None,sell_queue_value_toman=None,
                            queue_side=None,queue_value=None,book_valid=False)
            result.append(item)
        except (ValueError,IndexError): continue
    return result

def median(values):
    values=[v for v in values if v is not None and math.isfinite(v)]
    return statistics.median(values) if values else None

def summarize(stocks, day, average15=None):
    ordinary=[s for s in stocks if s.get('eligible_market_stock')]
    traded=[s for s in ordinary if s.get('trade_volume',0)>0 and s.get('score') is not None]
    leader=[s for s in stocks if s['symbol'] in LEADERS and s.get('trade_volume',0)>0]
    buy=sell=0
    for s in ordinary:
        first=s.get('first_trade_date')
        young=first and 0<=(day-date.fromisoformat(first)).days<30
        if not young: buy+=s.get('buy_queue_value_toman') or 0
        sell+=s.get('sell_queue_value_toman') or 0
    industries=[]
    for industry in INDUSTRIES.values():
        members=sorted([s for s in traded if s.get('industry')==industry],key=lambda s:(-s['score'],s['symbol']))
        med=median([s['score'] for s in members])
        if med is not None: industries.append(dict(industry=industry,median=med,count=len(members),stocks=members[:5]))
    industries.sort(key=lambda g:(-g['median'],g['industry']))
    queues_valid=bool(ordinary) and all(s.get('book_valid',True) for s in ordinary)
    return dict(count=len(traded),median=median([s['score'] for s in traded]),leader_median=median([s.get('score') for s in leader]),nonleader_median=median([s['score'] for s in traded if s['symbol'] not in LEADERS]),buy_toman=buy if queues_valid else None,sell_toman=sell if queues_valid else None,queue_ratio=(buy-sell)/average15 if average15 and queues_valid else None,turnover_toman=sum(s.get('trade_value_toman') or 0 for s in ordinary) if ordinary else None,industries=[g for g in industries if g['median']>=0][:5],all_industries=industries)

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
TEHRAN = ZoneInfo('Asia/Tehran')

def report_slots(day):
    if day.weekday() in (3,4): return []
    first=datetime.combine(day,datetime.min.time(),TEHRAN).replace(hour=9,minute=5)
    return [first+timedelta(minutes=10*i) for i in range(21)]

def stars(value, kind):
    if value is None: return None
    a,b,c,d={'median':(2,1,-1,-2),'turnover':(.25,.10,-.10,-.25),'queue':(.50,.20,-.20,-.50)}[kind]
    return 5 if value>a else 4 if value>b else 3 if value>=c else 2 if value>=d else 1

def allocation(leader, nonleader):
    if leader is None or nonleader is None: return None, 'داده کافی نیست'
    d=leader-nonleader
    n=5 if d>1.5 else 4 if d>.5 else 3 if d>=-.5 else 2 if d>=-1.5 else 1
    return n, {5:'تمایل شدید به لیدرها',4:'تمایل نسبی به لیدرها',3:'خنثی',2:'تمایل نسبی به هم‌وزن',1:'تمایل شدید به هم‌وزن'}[n]

def calculate_score(last_pct, buy_queue_volume, sell_queue_volume, trade_volume,
                    allowed_max_pct=None, allowed_min_pct=None):
    if trade_volume is None or trade_volume <= 0:
        return None
    if buy_queue_volume and sell_queue_volume:
        return None  # contradictory order book
    base = allowed_max_pct if buy_queue_volume else allowed_min_pct if sell_queue_volume else last_pct
    if base is None:
        return None
    result = base + (buy_queue_volume - sell_queue_volume) / trade_volume
    return result if math.isfinite(result) else None

class TelegramSender:
    def __init__(self,token,chat_id,proxy='https://api.telegram.org',session=None):
        import requests
        if not token or not chat_id: raise ValueError('Telegram configuration required')
        self.base=proxy.rstrip('/')+'/bot'+token; self.chat_id=chat_id; self.session=session or requests.Session()
    def __call__(self,message):
        if message['kind']=='text':
            payload=dict(chat_id=self.chat_id,text=message['text'],disable_web_page_preview=True)
            if message.get('parse_mode'): payload['parse_mode']=message['parse_mode']
            response=self.session.post(self.base+'/sendMessage',json=payload,timeout=30)
        else:
            with open(message['path'],'rb') as image:
                response=self.session.post(self.base+'/sendPhoto',data=dict(chat_id=self.chat_id,caption=message.get('caption','')),files={'photo':image},timeout=30)
        data=response.json()
        if not data.get('ok'): raise RejectedDelivery(data.get('description','Telegram rejected request'))
        response.raise_for_status()
        return data

def serve(store,metadata,output_dir,fetcher=None,clock=None,sender=None,once=False,metadata_path=None):
    import time
    fetcher=fetcher or fetch_market_data; clock=clock or (lambda:datetime.now(TEHRAN))
    while True:
        now=clock()
        try:
            with store.connect() as c: finalized=c.execute('SELECT 1 FROM v2_days WHERE day=? AND complete=1',(str(now.date()),)).fetchone()
            poll=now.weekday() not in (3,4) and ((8,50)<=(now.hour,now.minute)<=(12,25) or (13<=now.hour<18 and not finalized))
            if poll:
                packet=fetcher()
                now=clock()
                tick(store,now,lambda:packet,metadata,output_dir,sender)
                if sender: retry_pending(store,sender)
                next_slots=[s for s in report_slots(now.date()) if s>clock()]
                headroom=(next_slots[0]-clock()).total_seconds() if next_slots else 3600
                if metadata_path and headroom>45:
                    metadata=convert_metadata(refresh_metadata(metadata_path,parse_market_data(packet['stocks'],packet.get('depth'),metadata),now,limit=2))

        except Exception: logging.exception('Pipeline failed')
        if once: return
        time.sleep(max(1,60-clock().second))

def cli(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',metavar='MANIFEST')
    parser.add_argument('--metadata')
    parser.add_argument('--db',default='scores_history.db')
    parser.add_argument('--output',default='charts')
    parser.add_argument('--repair-legacy',action='store_true')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--send',action='store_true',help='Explicitly enable Telegram delivery (never used for preview)')
    args=parser.parse_args(argv)
    store=Store(args.db)
    if args.repair_legacy: print('repaired',repair_legacy_summaries(store))
    metadata=convert_metadata(json.loads(Path(args.metadata).read_text(encoding='utf-8'))) if args.metadata else {}
    if args.preview:
        count=replay_archive(store,args.preview,metadata)
        manifest=json.loads(Path(args.preview).read_text(encoding='utf-8'))
        day=max(datetime.fromisoformat(e['timestamp']).date() for e in manifest)
        points=store.points(day)
        if not points: raise ValueError('no usable archive snapshots')
        now,summary=points[-1]; generate_reports(store,now,summary,args.output,historical=True)
        Path(args.output,'incident.json').write_text(json.dumps(store.incident(now)['by_symbol'],ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(replayed=count,timestamp=now.isoformat(),count=summary['count'],median=summary['median'],messages=5)))
        return 0
    if args.repair_legacy: return 0
    if args.run:
        import os
        logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(levelname)s] %(message)s')
        sender=TelegramSender(os.environ.get('TELEGRAM_BOT_TOKEN'),os.environ.get('TELEGRAM_CHAT_ID'),os.environ.get('TELEGRAM_PROXY','https://api.telegram.org')) if args.send else None
        serve(store,metadata,args.output,sender=sender,once=args.once,metadata_path=args.metadata or str(Path(args.db).parent/'metadata.json'))
        return 0
    parser.error('specify --preview or --repair-legacy; live mode requires explicit --run')

if __name__=='__main__':
    raise SystemExit(cli())
