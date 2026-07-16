from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, abort, make_response, redirect, render_template_string, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config as app_config
from exit_policy import PositionExitState, resolve_effective_stop
from trading_store import TradingStore


APP = Flask(__name__)
POSITIONS_FILE = app_config.POSITIONS_FILE
EVENTS_FILE = app_config.PORTFOLIO_EVENTS_FILE
TRADING_DB_FILE = app_config.TRADING_DB_FILE
SESSION_COOKIE = "portfolio_session"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    value = safe_float(value)
    return int(value) if value is not None else default


def money(value: Any) -> str:
    value = safe_float(value)
    return "-" if value is None or value <= 0 else f"{value:.2f}"


def ensure_dirs() -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def validate_position_numbers(
    qty: int | None, cost_price: float | None, current_price: float | None,
    stop_pct: float | None, take_pct: float | None,
    stop_price: float | None, take_price: float | None,
) -> None:
    if qty is not None and qty < 0:
        raise ValueError("持仓数量不能为负数")
    for label, value in (("成本价", cost_price), ("现价", current_price), ("止损价", stop_price), ("止盈价", take_price)):
        if value is not None and value <= 0:
            raise ValueError(f"{label}必须大于 0")
    if stop_pct is not None and not 0 < stop_pct <= 50:
        raise ValueError("止损比例必须在 0 到 50 之间")
    if take_pct is not None and not 0 < take_pct <= 200:
        raise ValueError("止盈比例必须在 0 到 200 之间")
    if cost_price is not None and stop_price is not None and stop_price >= cost_price:
        raise ValueError("止损价必须低于成本价")
    if cost_price is not None and take_price is not None and take_price <= cost_price:
        raise ValueError("止盈价必须高于成本价")


class PositionStore:
    """Compatibility reader for the bounded JoinQuant position snapshot."""

    def __init__(self, positions_file: Path, events_file: Path):
        self.positions_file = positions_file
        self.events_file = events_file
        ensure_dirs()
        self.positions = self._load_positions()

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.positions_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = raw.get("positions", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        return {
            code: {**item, "code": code}
            for item in items if isinstance(item, dict)
            if (code := normalize_code(item.get("code")))
        }

    def save(self) -> None:
        payload = {"updated_at": now_iso(), "positions": sorted(self.positions.values(), key=lambda x: x["code"])}
        tmp = self.positions_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.positions_file)

    def append_event(self, action: str, payload: dict[str, Any]) -> None:
        record = {"ts": now_iso(), "action": action, **payload}
        with self.events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def upsert(self, entry: dict[str, Any], source: str) -> dict[str, Any]:
        code = normalize_code(entry.get("code"))
        if not code:
            raise ValueError("股票代码不能为空")
        current = self.positions.get(code, {})
        values = {
            "qty": safe_int(entry.get("qty"), safe_int(current.get("qty"))),
            "cost_price": safe_float(entry.get("cost_price"), safe_float(current.get("cost_price"))),
            "current_price": safe_float(entry.get("current_price"), safe_float(current.get("current_price"))),
            "stop_pct": safe_float(entry.get("stop_pct"), safe_float(current.get("stop_pct"))),
            "take_pct": safe_float(entry.get("take_pct"), safe_float(current.get("take_pct"))),
            "stop_price": safe_float(entry.get("stop_price"), safe_float(current.get("stop_price"))),
            "take_price": safe_float(entry.get("take_price"), safe_float(current.get("take_price"))),
        }
        validate_position_numbers(**values)
        saved = {
            **current, **entry, **values, "code": code, "source": source,
            "status": str(entry.get("status") or current.get("status") or "holding"),
            "updated_at": now_iso(),
        }
        self.positions[code] = saved
        self.save()
        self.append_event("upsert", {"code": code, "source": source})
        return saved


def _web_token() -> str:
    return str(getattr(app_config, "PORTFOLIO_WEB_TOKEN", "") or app_config.JOINQUANT_SYNC_TOKEN)


def _serializer() -> URLSafeTimedSerializer:
    token = _web_token()
    if not token:
        raise RuntimeError("portfolio web authentication is not configured")
    return URLSafeTimedSerializer(token, salt="portfolio-web-v1")


def _session() -> dict[str, str] | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=12 * 60 * 60)
        return data if isinstance(data, dict) else None
    except (BadSignature, SignatureExpired):
        return None


def _csrf(session: dict[str, str]) -> str:
    return _serializer().dumps({"nonce": session["nonce"]}, salt="csrf-v1")


def _check_csrf() -> None:
    session = _session()
    try:
        payload = _serializer().loads(request.form.get("csrf_token", ""), salt="csrf-v1", max_age=12 * 60 * 60)
    except (BadSignature, SignatureExpired):
        abort(403)
    if not session or payload.get("nonce") != session.get("nonce"):
        abort(403)


def authenticated(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _web_token():
            abort(503, description="交易运行面板尚未配置认证凭据")
        if not _session():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


LOGIN_TEMPLATE = """
<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>交易运行面板登录</title><style>body{font-family:sans-serif;background:#f4f6fa;margin:0}.box{max-width:360px;margin:12vh auto;background:white;padding:24px;border-radius:14px}input,button{width:100%;padding:12px;margin-top:12px;box-sizing:border-box}button{background:#2457f5;color:white;border:0;border-radius:8px}.bad{color:#b42318}</style>
<div class="box"><h2>交易运行面板</h2><p>请输入服务器面板凭据。凭据不会写入 URL。</p>{% if error %}<p class="bad">认证失败</p>{% endif %}<form method="post"><input type="password" name="token" autocomplete="current-password" required><button>登录</button></form></div></html>
"""


DASHBOARD_TEMPLATE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>交易运行面板</title>
<style>:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8f1;--muted:#667085;--bad:#b42318;--good:#087443;--warn:#b54708}*{box-sizing:border-box}body{margin:0;background:var(--bg);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#172033}.wrap{max-width:1180px;margin:auto;padding:12px}h1{font-size:22px;margin:4px 0}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px;margin:10px 0}.stat b{display:block;font-size:17px;margin-top:5px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}table{width:100%;border-collapse:collapse}th,td{padding:9px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}.scroll{overflow:auto}.risk{border-left:4px solid #2457f5}.risk.hit{border-left-color:var(--bad)}details{margin-top:10px}input,textarea,button{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;margin-top:6px}button{background:#2457f5;color:#fff}.message{padding:10px;border-radius:8px;background:#eef4ff}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.desktop{display:none}.card{padding:11px}}</style></head><body><div class="wrap">
<h1>交易运行面板</h1><div class="muted">服务器时间 {{ now }} · 代码 {{ version }}</div>{% if message %}<p class="message">{{ message }}</p>{% endif %}
<section class="grid">{% for label,value,state in stats %}<div class="card stat"><span class="muted">{{ label }}</span><b class="{{ state }}">{{ value }}</b></div>{% endfor %}</section>
<section class="card"><h2>当前异常（{{ issues|length }}）</h2>{% if issues %}<div class="scroll"><table><tr><th>级别</th><th>对象</th><th>状态</th><th>开始</th><th>最近</th></tr>{% for x in issues %}<tr><td class="{{ 'bad' if x.severity in ['ERROR','CRITICAL'] else 'warn' }}">{{ x.severity }}</td><td>{{ x.object_id }}</td><td>{{ x.state }}</td><td>{{ x.stage_started_at }}</td><td>{{ x.last_seen_at }}</td></tr>{% endfor %}</table></div>{% else %}<p class="good">没有活动执行异常</p>{% endif %}</section>
<h2>持仓风险（{{ positions|length }}）</h2>{% for p in positions %}<section class="card risk {{ 'hit' if p.stop_hit else '' }}"><b>{{ p.code }} {{ p.name }}</b><span class="muted"> · {{ p.mode }} · 阶段 {{ p.stage }}</span><div class="grid"><div>成本<br><b>{{ p.cost }}</b></div><div>现价<br><b>{{ p.price }}</b></div><div>盈亏<br><b class="{{ 'good' if p.pnl_value >= 0 else 'bad' }}">{{ p.pnl }}</b></div><div>数量/可卖<br><b>{{ p.qty }}/{{ p.closeable }}</b></div><div>初始止损<br><b>{{ p.initial }}</b></div><div>人工止损<br><b>{{ p.manual }}</b></div><div>移动止损<br><b>{{ p.trailing }}</b></div><div>有效止损<br><b class="{{ 'bad' if p.stop_hit else '' }}">{{ p.effective }}</b></div></div><p class="muted">距有效止损 {{ p.distance }} · 来源 {{ p.source }} · 快照 {{ p.updated_at }}</p>
<details><summary>执行轨迹</summary>{% if p.timeline %}<div class="scroll"><table><tr><th>时间</th><th>类型</th><th>状态/原因</th></tr>{% for t in p.timeline %}<tr><td>{{ t.ts }}</td><td>{{ t.kind }}</td><td>{{ t.text }}</td></tr>{% endfor %}</table></div>{% else %}<p class="muted">暂无退出或成交轨迹</p>{% endif %}</details>
<details><summary>人工止损维护</summary><form method="post" action="{{ url_for('manual_stop', code=p.code) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>新人工止损（留空表示清除）</label><input name="manual_stop_price" inputmode="decimal" value="{{ p.manual_value }}"><label>原因</label><textarea name="reason" required></textarea><button>确认修改并写入审计</button></form><p class="muted">只能上调；清除后仍受冻结初始止损和已激活移动止损保护。</p></details></section>{% else %}<section class="card">当前无持仓</section>{% endfor %}
</div></body></html>
"""


def _git_version() -> str:
    head = Path(__file__).parent / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref:"):
            value = (head.parent / value.split(" ", 1)[1]).read_text(encoding="utf-8").strip()
        return value[:8]
    except OSError:
        return "unknown"


def _dashboard_data() -> tuple[list[tuple[str, str, str]], list[dict], list[dict]]:
    store = TradingStore(TRADING_DB_FILE)
    store.initialize()
    snapshot_store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    cycles = store.get_active_position_cycles()
    with store.connect() as conn:
        state = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM system_state")}
        latest = conn.execute("SELECT * FROM account_snapshots ORDER BY generated_at DESC LIMIT 1").fetchone()
        scan = conn.execute("SELECT * FROM strategy_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        recon = conn.execute("SELECT * FROM reconciliation_runs ORDER BY finished_at DESC LIMIT 1").fetchone()
        issues = [dict(row) for row in conn.execute(
            "SELECT * FROM execution_issue_state WHERE recovered_at IS NULL ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'ERROR' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,last_seen_at DESC LIMIT 30"
        )]
        positions = []
        for code, item in snapshot_store.positions.items():
            if int(item.get("qty") or 0) <= 0 or code not in cycles:
                continue
            cycle = cycles[code]
            current = float(item.get("current_price") or 0)
            state_obj = PositionExitState(
                code=code, mode=str(cycle.get("mode") or "legacy_fixed"),
                initial_qty=int(cycle.get("initial_qty") or item.get("qty") or 0),
                current_qty=int(item.get("qty") or 0), entry_price=float(cycle.get("entry_price") or 0),
                initial_stop_price=float(cycle.get("initial_stop_price") or 0),
                highest_price=max(float(cycle.get("highest_price") or 0), current),
                atr14=float(cycle.get("atr14") or 0), take_profit_stage=int(cycle.get("take_profit_stage") or 0),
                holding_trade_days=0, manual_stop_price=float(cycle.get("manual_stop_price") or 0),
            )
            stop = resolve_effective_stop(state_obj, str(cycle.get("market_state") or "NORMAL"))
            cost = float(item.get("cost_price") or cycle.get("entry_price") or 0)
            pnl = (current / cost - 1) * 100 if cost and current else 0
            distance = (current / stop.effective_stop_price - 1) * 100 if stop.effective_stop_price else 0
            timeline = [dict(row) for row in conn.execute(
                """SELECT created_at ts,'退出意图' kind,status||' / '||reason text FROM exit_intents WHERE stock_code=?
                   UNION ALL SELECT event_at,'委托事件',status||' / '||reason FROM order_events WHERE stock_code=?
                   UNION ALL SELECT filled_at,'成交',action||' '||qty||'@'||price FROM fills WHERE stock_code=?
                   ORDER BY ts DESC LIMIT 20""", (code, code, code)
            )]
            positions.append({
                "code": code, "name": item.get("name", ""), "mode": cycle.get("mode", ""),
                "stage": cycle.get("take_profit_stage", 0), "cost": money(cost), "price": money(current),
                "pnl": f"{pnl:+.2f}%", "pnl_value": pnl, "qty": int(item.get("qty") or 0),
                "closeable": int(item.get("closeable_qty") or 0), "initial": money(stop.initial_stop_price),
                "manual": money(stop.manual_stop_price), "manual_value": stop.manual_stop_price or "",
                "trailing": money(stop.trailing_stop_price), "effective": money(stop.effective_stop_price),
                "distance": f"{distance:+.2f}%", "source": stop.source, "stop_hit": current <= stop.effective_stop_price,
                "updated_at": item.get("updated_at", ""), "timeline": timeline,
            })
    snapshot_stale = True
    if latest:
        try:
            snapshot_stale = (datetime.now() - datetime.fromisoformat(str(latest["generated_at"]))).total_seconds() > app_config.ACCOUNT_SNAPSHOT_MAX_AGE_SEC
        except ValueError:
            pass
    stats = [
        ("交易阶段", "交易中" if datetime.now().weekday() < 5 and 9 <= datetime.now().hour < 15 else "非交易时段", ""),
        ("最近扫描", str(scan["started_at"] if scan else "无"), "" if scan else "warn"),
        ("JoinQuant快照", str(latest["generated_at"] if latest else "无"), "warn" if snapshot_stale else "good"),
        ("数据状态", "陈旧" if snapshot_stale else "新鲜", "warn" if snapshot_stale else "good"),
        ("买入", "允许" if state.get("buy_enabled", "1") == "1" else "暂停", "good" if state.get("buy_enabled", "1") == "1" else "warn"),
        ("卖出", "允许" if state.get("sell_enabled", "1") == "1" else "暂停", "good" if state.get("sell_enabled", "1") == "1" else "bad"),
        ("Kill switch", "开启" if state.get("kill_switch", "0") == "1" else "关闭", "bad" if state.get("kill_switch", "0") == "1" else "good"),
        ("最近对账", str(recon["result"] if recon else "无"), "good" if recon and recon["result"] == "matched" else "warn"),
        ("Schema", str(store.health().schema_version), "good"),
        ("活动异常", str(len(issues)), "bad" if issues else "good"),
    ]
    return stats, issues, positions


@APP.route("/login", methods=["GET", "POST"])
def login():
    if not _web_token():
        abort(503, description="交易运行面板尚未配置认证凭据")
    error = False
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("token", ""), _web_token()):
            session = {"nonce": secrets.token_urlsafe(24)}
            response = make_response(redirect(url_for("index")))
            response.set_cookie(SESSION_COOKIE, _serializer().dumps(session), httponly=True, secure=request.is_secure, samesite="Strict", max_age=12 * 60 * 60)
            return response
        error = True
    return render_template_string(LOGIN_TEMPLATE, error=error)


@APP.get("/")
@authenticated
def index():
    stats, issues, positions = _dashboard_data()
    return render_template_string(
        DASHBOARD_TEMPLATE, now=now_iso(), version=_git_version(), stats=stats, issues=issues,
        positions=positions, csrf_token=_csrf(_session()), message=request.args.get("message", ""),
    )


@APP.post("/manual-stop/<code>")
@authenticated
def manual_stop(code: str):
    _check_csrf()
    code = normalize_code(code)
    value = safe_float(request.form.get("manual_stop_price"))
    reason = str(request.form.get("reason") or "").strip()
    store = TradingStore(TRADING_DB_FILE)
    store.initialize()
    try:
        with store.transaction() as conn:
            store.set_manual_stop(conn, code, value, reason, operator="portfolio_web", now=now_iso())
    except ValueError as exc:
        return redirect(url_for("index", message=str(exc)))
    return redirect(url_for("index", message=f"{code} 人工止损已更新并写入审计"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易运行面板")
    parser.add_argument("--host", default=app_config.PORTFOLIO_WEB_HOST_DEFAULT)
    parser.add_argument("--port", type=int, default=app_config.PORTFOLIO_WEB_PORT_DEFAULT)
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    ensure_dirs()
    print(f"交易运行面板已启动：http://{args.host}:{args.port}", flush=True)
    APP.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
