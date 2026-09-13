import os
import sqlite3

import main
from main import calculate_score, queue_value_billion_toman, format_report_line, market_summary, market_allocation_signal, status_stars



def test_status_stars_map_five_levels_and_missing_data():
    assert status_stars("عالی") == "⭐⭐⭐⭐⭐"
    assert status_stars("خوب") == "⭐⭐⭐⭐"
    assert status_stars("معمولی") == "⭐⭐⭐"
    assert status_stars("بد") == "⭐⭐"
    assert status_stars("افتضاح") == "⭐"
    assert status_stars("داده کافی نیست") == "—"


def test_market_summary_puts_three_status_scores_at_top():
    stocks = [
        {"symbol": "الف", "score": 3.0, "trade_volume": 100, "eligible_market_stock": True,
         "buy_queue_value_toman": 6_000_000_000_000, "sell_queue_value_toman": 0},
    ]
    report = market_summary(stocks, leader_stats=(0.0, 0.0), turnover=(10.0, 1.3, "خوب"))
    top = report.splitlines()[:5]
    assert top[1] == "⭐ میانه بازار: ⭐⭐⭐⭐⭐"
    assert top[2] == "⭐ اختلاف عرضه و تقاضا: ⭐⭐⭐⭐⭐"
    assert top[3] == "⭐ نسبت ارزش معاملات ۳ به ۱۰ روزه: ⭐⭐⭐⭐"


def test_market_allocation_signal_classifies_median_gap():
    assert market_allocation_signal(2.1, 0.9) == "تمایل شدید به سهام هم‌وزن"
    assert market_allocation_signal(1.5, 0.9) == "تمایل به سهام هم‌وزن"
    assert market_allocation_signal(1.0, 0.6) == "تمایل خاصی وجود ندارد"
    assert market_allocation_signal(0.0, 0.8) == "تمایل به سهام لیدرها"
    assert market_allocation_signal(-1.0, 0.2) == "تمایل به سهام لیدرها"
    assert market_allocation_signal(-1.1, 0.0) == "تمایل شدید به لیدرها"
    assert market_allocation_signal(0.0, 1.1) == "تمایل شدید به لیدرها"


def test_market_summary_reports_allocation_gap_and_signal():
    stocks = [
        {"symbol": "الف", "score": 1.0, "trade_volume": 100, "eligible_market_stock": True},
        {"symbol": "ب", "score": 5.0, "trade_volume": 100, "eligible_market_stock": True},
    ]
    report = market_summary(stocks, leader_stats=(0.0, 1.0))
    assert "اختلاف میانه کل بازار و لیدرها: ‎+2.0‎" in report
    assert "تمایل پول: تمایل شدید به سهام هم‌وزن" in report


def test_market_summary_reports_market_and_leader_medians_and_signal():
    stocks = [
        {"symbol": "لیدر", "score": 1.0, "trade_volume": 100, "eligible_market_stock": True},
        {"symbol": "هم‌وزن", "score": 5.0, "trade_volume": 100, "eligible_market_stock": True},
    ]
    report = market_summary(stocks, leader_stats=(0.0, 1.0))
    assert "میانه کل بازار: 3.0" in report
    assert "میانه لیدرها: 1.0" in report
    assert "اختلاف میانه: ‎+2.0‎" in report
    assert "تمایل شدید به سهام هم‌وزن" in report




def test_market_summary_reports_count_average_and_median_for_traded_stocks():
    stocks = [
        {"symbol": "الف", "score": 1.0, "trade_volume": 100, "eligible_market_stock": True},
        {"symbol": "ب", "score": 2.0, "trade_volume": 200, "eligible_market_stock": True},
        {"symbol": "ج", "score": 5.0, "trade_volume": 300, "eligible_market_stock": True},
        {"symbol": "د", "score": 9.0, "trade_volume": 0, "eligible_market_stock": True},
    ]
    assert market_summary(stocks) == "تعداد کل سهام معامله شده امروز: 3\nمیانگین نمره: 2.7\nمیانه نمره: 2.0\nارزش سفارشات خرید در سقف: 0.00 همت\nارزش سفارشات فروش در کف: 0.00 همت"


def test_market_summary_is_empty_when_no_stock_traded():
    assert market_summary([{ "symbol": "الف", "score": 1.0, "trade_volume": 0, "eligible_market_stock": True }]) == "تعداد کل سهام معامله شده امروز: 0\nمیانگین نمره: 0.0\nمیانه نمره: 0.0\nارزش سفارشات خرید در سقف: 0.00 همت\nارزش سفارشات فروش در کف: 0.00 همت"


def test_buy_queue_score_uses_last_percent_plus_queue_ratio():
    assert calculate_score(3.0, 20_000_000, 0, 10_000_000) == 5.0


def test_sell_queue_score_is_subtracted():
    assert calculate_score(-3.0, 0, 20_000_000, 10_000_000) == -5.0


def test_queue_value_is_integer_billion_toman():
    assert queue_value_billion_toman(5_000, 20_000_000) == 10


def test_line_omits_queue_when_there_is_no_queue():
    assert format_report_line(1, {"symbol": "فولاد", "score": 2.35, "queue_value": 0, "queue_side": None}) == "1. فولاد | ‎2.4‎ | قبل ‎0.0‎"


def test_line_shows_latin_unit_and_left_to_right_signs():
    assert format_report_line(1, {"symbol": "فولاد", "score": 5.0, "queue_value": 125, "queue_side": "buy"}) == "1. فولاد | ‎5.0‎ | ‎125 B‎ | قبل ‎0.0‎"
    assert format_report_line(2, {"symbol": "خودرو", "score": -4.2, "queue_value": 87, "queue_side": "sell"}) == "2. خودرو | ‎-4.2‎ | ‎-87 B‎ | قبل ‎0.0‎"


def test_market_chart_uses_four_requested_series_and_explicit_legend(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    now = main.datetime(2026, 9, 13, 10, 0, tzinfo=main.TEHRAN)
    main.init_db()
    main.save_market_snapshot((100, 1.9, 2.3, 3.0, 0.0), now, leader_average=3.9, leader_median=3.0)
    main.save_market_snapshot((100, 2.1, 2.5, 3.2, 0.0), now.replace(minute=10), leader_average=4.1, leader_median=3.2)

    chart_paths = main.create_market_charts(now.replace(minute=10), str(tmp_path / "charts"))

    assert len(chart_paths) == 1
    assert os.path.basename(chart_paths[0]) == "market_scores.png"
    assert os.path.getsize(chart_paths[0]) > 0


def test_should_send_market_chart_only_on_ten_minute_boundaries():
    assert main.should_send_market_chart(main.datetime(2026, 9, 13, 10, 0, tzinfo=main.TEHRAN))
    assert main.should_send_market_chart(main.datetime(2026, 9, 13, 10, 10, tzinfo=main.TEHRAN))
    assert not main.should_send_market_chart(main.datetime(2026, 9, 13, 10, 7, tzinfo=main.TEHRAN))


def test_industry_report_ranks_only_configured_industries_by_median_and_previous_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "history.db"))
    now = main.datetime(2026, 9, 13, 10, 10, tzinfo=main.TEHRAN)
    main.init_db()
    main.save_industry_snapshot({"فلزات اساسی": 1.0, "خودرو": 4.0}, now.replace(minute=0))
    stocks = [
        {"symbol": "فولاد", "score": 5.0, "trade_volume": 10, "eligible_market_stock": True, "industry": "فلزات اساسی"},
        {"symbol": "فملی", "score": 3.0, "trade_volume": 10, "eligible_market_stock": True, "industry": "فلزات اساسی"},
        {"symbol": "خودرو", "score": 2.0, "trade_volume": 10, "eligible_market_stock": True, "industry": "خودرو"},
        {"symbol": "شیمی", "score": 99.0, "trade_volume": 10, "eligible_market_stock": True, "industry": "شیمیایی"},
        {"symbol": "بسته", "score": 100.0, "trade_volume": 0, "eligible_market_stock": True, "industry": "بانک"},
    ]
    report = main.build_industry_message(stocks, now, limit=5)
    assert report.splitlines() == [
        "#صنایع",
        "1. خودرو | ‎2.0‎ | قبل ‎-2.0‎",
        "2. فلزات اساسی | ‎4.0‎ | قبل ‎+3.0‎",
        "3. شیمیایی | ‎99.0‎ | قبل ‎+99.0‎",
    ]


def test_industry_report_excludes_unconfigured_industries():
    stocks = [{"symbol": "الف", "score": 10, "trade_volume": 1, "eligible_market_stock": True, "industry": "رایانه"}]
    assert main.industry_stats(stocks) == []
