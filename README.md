# Bourse Alert

Persian Telegram monitoring for ordinary Tehran exchange shares. The approved step1/step2 contract is in [REVIEWED_SPEC.md](REVIEWED_SPEC.md). Stock-picking/step3 remains pending product review; `deployed_main.py` isolates the pre-existing leader/leveraged report renderers.

## Run

Python3.11+; production tested with Python3.12. Install `requirements.txt` in a virtual environment. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optionally `TELEGRAM_PROXY` in a protected environment file, never in Git.

```bash
python main.py --run --send --db /root/bourse-alert/scores_history.db --metadata /root/bourse-alert/metadata.json --output /root/bourse-alert/charts
```

Sending requires explicit `--send`. A no-send single iteration uses `--run --once` without `--send`; outside the operating/finalization windows it intentionally performs no fetch.

- Tehran timezone; Saturday–Wednesday.
- Reports and score snapshots09:05,09:15,…,12:25 (21 slots); no12:30 score/queue snapshot.
- Separate post-close cumulative turnover finalization after13:00 supplies future completed-day MAs; never uses12:25 turnover as a complete day.
- MA5/MA15 and queue denominator require15 verified prior complete days. Old unverified daily totals are NOT silently imported as complete.
- Metadata/first-trade-date cache bounded refresh; confirmed first dates are retained.
- Receipt/outbox tables avoid duplicate sends. Transport-ambiguous `uncertain`/`sending` requires reconciliation, not blind resend.

## Tests

```bash
python -m unittest discover -s tests -v
python -m unittest test_watchdog_revision -v
```

Real-archive integration cases additionally require a local `real_sample/manifest.json` with actual gzipped raw snapshots plus `metadata_verified.json`. They explicitly skip when those external fixtures are absent; no synthetic market output is substituted. Production verification ran with real fixtures available.

## Historical preview / migration

```bash
python main.py --preview archive_manifest.json --metadata metadata.json --db staging.db --output sample_charts
python main.py --repair-legacy --db staging.db
```

Manifest: JSON array of `{timestamp, stocks, depth}`; stocks/depth identify gzipped JSON arrays of original MarketWatch raw rows. Original timestamps are preserved. Historical preview never sends. Old snapshots after12:25 are excluded; replay is idempotent. `incident.json` exports per-stock rolling30-calendar-day medians and sample coverage. Repair updates legacy `median_score` and `last_score` from available chronological source rows.

Back up source, DB via SQLite backup API, and systemd configuration before migration. Test a separate staging DB before atomic source deployment. `main.py` requires `deployed_main.py` alongside it for legacy reports. The watchdog uses v2 snapshot cadence, minute-fetch heartbeat, and core chart filenames.

## Evidence limits

Source freshness checks both official market-overview dates/times plus the newly fetched MarketWatch payload. MarketWatch supplies intraday times but no instrument-level calendar date, so individual order-book age cannot independently be proven from that feed. Stale/invalid data is not represented as fresh/zero. Recent IPO buy queues only are removed from market queue totals; all other eligible contributions remain.
