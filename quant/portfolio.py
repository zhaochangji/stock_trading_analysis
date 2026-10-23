"""Helpers for valuing a portfolio from cash and current holding quotes."""

from __future__ import annotations

import math


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def infer_legacy_cash_balance(holdings, prices, legacy_total_assets):
    """Infer an initial cash balance from legacy equity and a complete snapshot."""
    total_assets = _number(legacy_total_assets)
    if total_assets is None or total_assets < 0:
        return None

    market_value = 0.0
    for holding in holdings or []:
        code = str(holding.get("code") or "").strip().zfill(6)
        shares = _number(holding.get("shares"))
        quote = (prices or {}).get(code)
        price = _number(quote.get("price") if isinstance(quote, dict) else quote)
        if shares is None or shares < 0 or (shares > 0 and (price is None or price <= 0)):
            return None
        if shares:
            market_value += price * shares

    cash = total_assets - market_value
    return cash if cash >= 0 else None


def calculate_portfolio(holdings, prices, cash_balance=None, legacy_total_assets=None,
                        cash_estimated=False):
    """Calculate current account equity from cash and market value.

    ``prices`` maps six-digit stock codes to quote prices or quote dictionaries.
    A total is returned only when cash and every holding are known. When migrating
    from an older total-assets value, callers should persist the inferred cash
    baseline once so subsequent quote movements change equity as expected.
    """
    positions = {}
    missing_codes = []
    market_value_partial = 0.0
    unrealized_pnl_partial = 0.0
    pnl_complete = True

    for holding in holdings or []:
        code = str(holding.get("code") or "").strip().zfill(6)
        if code == "000000":
            continue
        shares = _number(holding.get("shares"))
        quote = (prices or {}).get(code)
        price = _number(quote.get("price") if isinstance(quote, dict) else quote)
        cost = _number(holding.get("cost"))

        if shares is None or shares < 0 or (shares > 0 and (price is None or price <= 0)):
            missing_codes.append(code)
            market_value = profit = profit_pct = None
            pnl_complete = False
        else:
            market_value = price * shares if shares else 0.0
            market_value_partial += market_value
            if cost is not None and cost > 0 and (price is not None or shares == 0):
                profit = ((price - cost) * shares) if shares else 0.0
                profit_pct = ((price / cost - 1) * 100) if shares and price is not None else 0.0
                unrealized_pnl_partial += profit
            else:
                profit = profit_pct = None
                if shares:
                    pnl_complete = False

        positions[code] = {
            "price": price,
            "shares": shares,
            "market_value": market_value,
            "profit": profit,
            "profit_pct": profit_pct,
        }

    complete = not missing_codes
    cash = _number(cash_balance)
    cash_source = "configured" if cash is not None and cash >= 0 else None
    if cash_source is None:
        cash = infer_legacy_cash_balance(holdings, prices, legacy_total_assets)
        if cash is not None:
            cash_source = "estimated_from_legacy_total_assets"
    cash_known = cash is not None and cash >= 0
    market_value = market_value_partial if complete else None
    total_assets = cash + market_value if cash_known and complete else None
    unrealized_pnl = unrealized_pnl_partial if complete and pnl_complete else None

    return {
        "positions": positions,
        "cash_balance": cash if cash_known else None,
        "cash_known": cash_known,
        "cash_source": "estimated_from_legacy_total_assets" if cash_estimated else cash_source,
        "market_value": market_value,
        "market_value_partial": market_value_partial,
        "total_assets": total_assets,
        "unrealized_pnl": unrealized_pnl,
        "complete": complete,
        "missing_codes": sorted(set(missing_codes)),
    }
