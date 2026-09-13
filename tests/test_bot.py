from main import calculate_score, queue_value_billion_toman, format_report_line, market_summary


def test_market_summary_reports_count_average_and_median_for_traded_stocks():
    stocks = [
        {"symbol": "الف", "score": 1.0, "trade_volume": 100, "eligible_market_stock": True},
        {"symbol": "ب", "score": 2.0, "trade_volume": 200, "eligible_market_stock": True},
        {"symbol": "ج", "score": 5.0, "trade_volume": 300, "eligible_market_stock": True},
        {"symbol": "د", "score": 9.0, "trade_volume": 0, "eligible_market_stock": True},
    ]
    assert market_summary(stocks) == "تعداد کل سهام معامله شده امروز: 3\\nمیانگین نمره: 2.7\\nمیانه نمره: 2.0\\nارزش سفارشات خرید در سقف: 0.00 همت\\nارزش سفارشات فروش در کف: 0.00 همت"


def test_market_summary_is_empty_when_no_stock_traded():
    assert market_summary([{ "symbol": "الف", "score": 1.0, "trade_volume": 0, "eligible_market_stock": True }]) == "تعداد کل سهام معامله شده امروز: 0\\nمیانگین نمره: 0.0\\nمیانه نمره: 0.0\\nارزش سفارشات خرید در سقف: 0.00 همت\\nارزش سفارشات فروش در کف: 0.00 همت"


def test_buy_queue_score_uses_last_percent_plus_queue_ratio():
    assert calculate_score(3.0, 20_000_000, 0, 10_000_000) == 5.0


def test_sell_queue_score_is_subtracted():
    assert calculate_score(-3.0, 0, 20_000_000, 10_000_000) == -5.0


def test_queue_value_is_integer_billion_toman():
    assert queue_value_billion_toman(5_000, 20_000_000) == 10


def test_line_omits_queue_when_there_is_no_queue():
    assert format_report_line(1, {"symbol": "فولاد", "score": 2.35, "queue_value": 0, "queue_side": None}) == "1. فولاد | ‎2.4‎"


def test_line_shows_latin_unit_and_left_to_right_signs():
    assert format_report_line(1, {"symbol": "فولاد", "score": 5.0, "queue_value": 125, "queue_side": "buy"}) == "1. فولاد | ‎5.0‎ | ‎125 B‎"
    assert format_report_line(2, {"symbol": "خودرو", "score": -4.2, "queue_value": 87, "queue_side": "sell"}) == "2. خودرو | ‎-4.2‎ | ‎-87 B‎"
