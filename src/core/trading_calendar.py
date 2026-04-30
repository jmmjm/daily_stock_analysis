# -*- coding: utf-8 -*-
"""
===================================
交易日历模块 (Issue #373)
===================================

职责：
1. 按市场（A股/港股/美股）判断当日是否为交易日
2. 按市场时区取“今日”日期，避免服务器 UTC 导致日期错误
3. 支持 per-stock 过滤：只分析当日开市市场的股票

依赖：exchange-calendars（可选，不可用时 fail-open）
"""

import logging
from datetime import date, datetime
from typing import Optional, Set, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Exchange-calendars availability
_XCALS_AVAILABLE = False
try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:
    logger.warning(
        "exchange-calendars not installed; trading day check disabled. "
        "Run: pip install exchange-calendars"
    )

# Market -> exchange code (exchange-calendars)
MARKET_EXCHANGE = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}

# Market -> IANA timezone for "today"
MARKET_TIMEZONE = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
}


def get_market_for_stock(code: str) -> Optional[str]:
    """
    Infer market region for a stock code.

    Returns:
        "cn", "hk", "us", or None if unparseable
    """
    if not code:
        return None
    c = code.upper()
    if c.startswith("SH") or c.startswith("SZ") or c.startswith("BJ"):
        return "cn"
    if c.startswith("HK"):
        return "hk"
    # Longbridge format: .US / .HK
    if c.endswith(".US"):
        return "us"
    if c.endswith(".HK"):
        return "hk"
    if c.endswith(".TO") or c.endswith(".V"):
        # Canada map to US timezone/exchange
        return "us"
    
    # 纯数字，如果是5位数，常见为港股
    if c.isdigit():
        if len(c) == 5:
            return "hk"
        # 6位数字：如果首位是 6、0、3、4、8 通常是 A 股
        if len(c) == 6 and c[0] in ("6", "0", "3", "4", "8"):
            return "cn"

    # US tickers: APPL, MSFT, TSLA, SPY, QQQ (letters, no explicit market prefix)
    if c.isalpha() or "^" in c:
        return "us"
        
    return None


def is_market_open(market: str, check_date: date) -> bool:
    """
    Check if the given market is open on the given date.

    Fail-open: returns True if exchange-calendars unavailable or date out of range.

    Args:
        market: 'cn' | 'hk' | 'us'
        check_date: Date to check

    Returns:
        True if trading day (or fail-open), False otherwise
    """
    if not _XCALS_AVAILABLE:
        return True
    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        return True
    try:
        cal = xcals.get_calendar(ex)
        session = datetime(check_date.year, check_date.month, check_date.day)
        return cal.is_session(session)
    except Exception as e:
        logger.warning("trading_calendar.is_market_open fail-open: %s", e)
        return True


def get_market_now(
    market: Optional[str], current_time: Optional[datetime] = None
) -> datetime:
    """
    Return current time in the market's local timezone.

    If current_time is naive, treat it as already expressed in the market timezone.
    Unknown markets fall back to the given datetime (or local system time).
    """
    tz_name = MARKET_TIMEZONE.get(market or "")

    if current_time is None:
        if tz_name:
            return datetime.now(ZoneInfo(tz_name))
        return datetime.now()

    if not tz_name:
        return current_time

    tz = ZoneInfo(tz_name)
    if current_time.tzinfo is None:
        return current_time.replace(tzinfo=tz)
    return current_time.astimezone(tz)


def get_effective_trading_date(
    market: Optional[str], current_time: Optional[datetime] = None
) -> date:
    """
    Resolve the latest reusable daily-bar date for checkpoint/resume logic.

    Rules:
    - Non-trading day / holiday: previous trading session
    - Trading day before market close: previous completed trading session
    - Trading day after market close: current trading session
    - Calendar lookup failure: fail-open to market-local natural date
    """
    market_now = get_market_now(market, current_time=current_time)
    fallback_date = market_now.date()

    if not _XCALS_AVAILABLE:
        return fallback_date

    ex = MARKET_EXCHANGE.get(market or "")
    tz_name = MARKET_TIMEZONE.get(market or "")
    if not ex or tz_name is None:
        return fallback_date

    try:
        cal = xcals.get_calendar(ex)
        local_date = market_now.date()

        if not cal.is_session(local_date):
            return cal.date_to_session(local_date, direction="previous").date()

        session = cal.date_to_session(local_date, direction="previous")
        session_close = cal.session_close(session)
        if hasattr(session_close, "tz_convert"):
            close_local = session_close.tz_convert(tz_name).to_pydatetime()
        elif session_close.tzinfo is not None:
            close_local = session_close.astimezone(ZoneInfo(tz_name))
        else:
            close_local = session_close.replace(tzinfo=ZoneInfo(tz_name))

        if market_now >= close_local:
            return session.date()

        return cal.previous_session(session).date()
    except Exception as e:
        logger.warning("trading_calendar.get_effective_trading_date fail-open: %s", e)
        return fallback_date


def get_open_markets_today() -> Set[str]:
    """
    Get markets that are open today (by each market's local timezone).

    Returns:
        Set of market keys ('cn', 'hk', 'us') that are trading today
    """
    if not _XCALS_AVAILABLE:
        return {"cn", "hk", "us"}
    result: Set[str] = set()
    for mkt, tz_name in MARKET_TIMEZONE.items():
        try:
            tz = ZoneInfo(tz_name)
            today = datetime.now(tz).date()
            if is_market_open(mkt, today):
                result.add(mkt)
        except Exception as e:
            logger.warning("get_open_markets_today fail-open for %s: %s", mkt, e)
            result.add(mkt)
    return result


def _is_market_review_enabled_for_market(config_region: str, market: str) -> bool:
    """
    Check if the user config enables market review for a specific market.
    config_region: cn | hk | us | both | auto
    """
    if config_region == "both":
        return True
    if config_region == "auto":
        # auto delegates to later logic, we return True here to mean "potentially enabled"
        return True
    return config_region == market


def compute_effective_region(
    config_region: str, open_markets: Set[str], stock_codes: Optional[List[str]] = None
) -> Optional[str]:
    """
    Compute effective market review region given config, open markets, and analyzed stocks.

    Args:
        config_region: From MARKET_REVIEW_REGION ('auto' | 'cn' | 'hk' | 'us' | 'both')
        open_markets: Markets open today
        stock_codes: Analyzed stock codes to infer region if config is 'auto'

    Returns:
        None: caller uses config default (check disabled)
        '': all relevant markets closed, skip market review
        'cn' | 'hk' | 'us' | 'cn,us' etc: effective subset for today
    """
    if config_region not in ("auto", "cn", "hk", "us", "both"):
        config_region = "auto"
        
    if config_region == "auto":
        if stock_codes:
            stock_markets = set()
            for code in stock_codes:
                mkt = get_market_for_stock(code)
                if mkt == "cn":
                    stock_markets.add("cn")
                elif mkt == "hk":
                    stock_markets.add("hk")
                elif mkt == "us":
                    stock_markets.add("us")
                    
            parts = []
            if "cn" in stock_markets:
                parts.append("cn")
            if "hk" in stock_markets:
                parts.append("hk")
            if "us" in stock_markets:
                parts.append("us")
                
            if len(parts) > 1:
                config_region = "both"
            elif len(parts) == 1:
                config_region = parts[0]
            else:
                config_region = "cn"
        else:
            config_region = "cn"

    if config_region in ("cn", "hk", "us"):
        return config_region if config_region in open_markets else ""
    
    # both: return only the markets that are actually open today
    parts = [m for m in ("cn", "hk", "us") if m in open_markets]

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ",".join(parts)

