"""Path-specific exit overlay for the multipath simulation strategy."""

if "FACTOR_PATH_WAVE3" not in globals():
    from factor_contracts import (
        FACTOR_PATH_LIMITDOWN,
        FACTOR_PATH_MOMENTUM,
        FACTOR_PATH_WAVE3,
        factor_number,
    )


STRATEGY_EXIT_RUNTIME_VERSION = "2026-08-10.1"


def evaluate_factor_exit(
    factor_path,
    entry_price,
    initial_stop_price,
    current_price,
    highest_price,
    atr14,
    holding_trade_days,
    market_state="NORMAL",
):
    """Return None for momentum or a deterministic path-specific overlay."""
    path = str(factor_path or FACTOR_PATH_MOMENTUM)
    if path == FACTOR_PATH_MOMENTUM:
        return None
    entry = factor_number(entry_price)
    stop = factor_number(initial_stop_price)
    current = factor_number(current_price)
    highest = max(factor_number(highest_price), current)
    atr = factor_number(atr14)
    days = max(0, int(factor_number(holding_trade_days)))
    risk = max(entry - stop, 0.0)
    r_multiple = (current - entry) / risk if risk > 0 else 0.0
    result = {
        "version": STRATEGY_EXIT_RUNTIME_VERSION,
        "factor_path": path,
        "action": "hold",
        "target_qty": None,
        "new_stop_price": None,
        "r_multiple": round(r_multiple, 4),
        "reason": "继续持有",
    }
    if min(entry, stop, current) <= 0 or stop >= entry:
        result.update({"action": "exit_all", "target_qty": 0, "reason": "因子持仓价格计划无效"})
        return result
    if current <= stop:
        result.update({"action": "exit_all", "target_qty": 0, "reason": "触及因子冻结初始止损"})
        return result
    if str(market_state).upper() == "RISK_OFF":
        result.update({"action": "exit_all", "target_qty": 0, "reason": "市场进入风险释放状态"})
        return result
    if path == FACTOR_PATH_WAVE3:
        if r_multiple >= 2.0:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "第三浪路径达到2R目标"})
        elif days >= 10:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "第三浪路径达到10个交易日上限"})
        elif days >= 5 and r_multiple < 0.5:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "第三浪路径5日内未达到0.5R"})
        return result
    if path == FACTOR_PATH_LIMITDOWN:
        trailing = highest - atr if atr > 0 and r_multiple >= 1.0 else 0.0
        protected = max(entry, trailing) if r_multiple >= 1.0 else 0.0
        if r_multiple >= 1.5:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "跌停衰竭路径达到1.5R目标"})
        elif protected > 0 and current <= protected:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "跌停衰竭路径触及1ATR盈利保护"})
        elif days >= 3:
            result.update({"action": "exit_all", "target_qty": 0, "reason": "跌停衰竭路径达到3个交易日上限"})
        elif protected > stop:
            result.update({"action": "raise_stop", "new_stop_price": round(protected, 2), "reason": "达到1R，上调至成本线与1ATR保护中较高者"})
        return result
    result.update({"action": "exit_all", "target_qty": 0, "reason": "未知因子路径，失败关闭"})
    return result
