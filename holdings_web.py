from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, abort, redirect, render_template_string, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

import config as app_config

try:
    from PIL import Image, ImageFilter, ImageOps
except Exception:  # pragma: no cover - optional dependency
    Image = None
    ImageFilter = None
    ImageOps = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None


APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

PORTFOLIO_DIR = app_config.PORTFOLIO_DIR
UPLOAD_DIR = app_config.PORTFOLIO_UPLOAD_DIR
POSITIONS_FILE = app_config.POSITIONS_FILE
EVENTS_FILE = app_config.PORTFOLIO_EVENTS_FILE


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
    except Exception:
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def money(value: Any) -> str:
    num = safe_float(value)
    if num is None:
        return "-"
    return f"{num:.2f}"


def pct_text(value: Any) -> str:
    num = safe_float(value)
    if num is None:
        return "-"
    return f"{num:.2f}%"


def ensure_dirs() -> None:
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def validate_position_numbers(
    qty: int | None,
    cost_price: float | None,
    current_price: float | None,
    stop_pct: float | None,
    take_pct: float | None,
    stop_price: float | None,
    take_price: float | None,
) -> None:
    if qty is not None and qty < 0:
        raise ValueError("持仓数量不能为负数")
    for label, value in (("成本价", cost_price), ("现价", current_price), ("止损价", stop_price), ("止盈价", take_price)):
        if value is not None and value <= 0:
            raise ValueError(f"{label}必须大于 0")
    if stop_pct is not None and not (0 < stop_pct <= 50):
        raise ValueError("止损比例必须在 0 到 50 之间")
    if take_pct is not None and not (0 < take_pct <= 200):
        raise ValueError("止盈比例必须在 0 到 200 之间")
    if cost_price is not None and stop_price is not None and stop_price >= cost_price:
        raise ValueError("止损价必须低于成本价")
    if cost_price is not None and take_price is not None and take_price <= cost_price:
        raise ValueError("止盈价必须高于成本价")


@dataclass
class ParsedRow:
    code: str = ""
    name: str = ""
    qty: str = ""
    cost_price: str = ""
    current_price: str = ""
    stop_pct: str = ""
    take_pct: str = ""
    note: str = ""
    raw_line: str = ""


class PositionStore:
    def __init__(self, positions_file: Path, events_file: Path):
        self.positions_file = positions_file
        self.events_file = events_file
        ensure_dirs()
        self.positions = self._load_positions()

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        if not self.positions_file.exists():
            return {}
        try:
            raw = json.loads(self.positions_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

        if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
            items = raw["positions"]
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = list(raw.values())
        else:
            items = []

        db: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = normalize_code(item.get("code"))
            if code:
                item = dict(item)
                item["code"] = code
                db[code] = item
        return db

    def save(self) -> None:
        payload = {
            "updated_at": now_iso(),
            "positions": sorted(self.positions.values(), key=lambda x: (x.get("status", ""), x.get("code", ""))),
        }
        tmp = self.positions_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.positions_file)

    def append_event(self, action: str, payload: dict[str, Any]) -> None:
        ensure_dirs()
        record = {
            "ts": now_iso(),
            "action": action,
            **payload,
        }
        with self.events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def upsert(self, entry: dict[str, Any], source: str) -> dict[str, Any]:
        code = normalize_code(entry.get("code"))
        if not code:
            raise ValueError("股票代码不能为空")

        current = dict(self.positions.get(code, {}))
        cost_price = safe_float(entry.get("cost_price"), safe_float(current.get("cost_price")))
        current_price = safe_float(entry.get("current_price"), safe_float(current.get("current_price")))
        stop_pct = safe_float(entry.get("stop_pct"), safe_float(current.get("stop_pct"), 3.5))
        take_pct = safe_float(entry.get("take_pct"), safe_float(current.get("take_pct"), 7.0))
        stop_price = safe_float(entry.get("stop_price"), safe_float(current.get("stop_price")))
        take_price = safe_float(entry.get("take_price"), safe_float(current.get("take_price")))
        qty = safe_int(entry.get("qty"), safe_int(current.get("qty")))

        if stop_price is None and cost_price is not None:
            stop_price = round(cost_price * (1 - (stop_pct or 3.5) / 100), 2)
        if take_price is None and cost_price is not None:
            take_price = round(cost_price * (1 + (take_pct or 7.0) / 100), 2)

        validate_position_numbers(qty, cost_price, current_price, stop_pct, take_pct, stop_price, take_price)

        updated = {
            "code": code,
            "name": str(entry.get("name") or current.get("name") or "").strip(),
            "qty": qty,
            "cost_price": cost_price,
            "current_price": current_price,
            "stop_pct": stop_pct,
            "take_pct": take_pct,
            "stop_price": stop_price,
            "take_price": take_price,
            "position_ratio": safe_float(entry.get("position_ratio"), safe_float(current.get("position_ratio"))),
            "status": str(entry.get("status") or current.get("status") or "holding").strip() or "holding",
            "note": str(entry.get("note") or current.get("note") or "").strip(),
            "source": source,
            "screenshot": str(entry.get("screenshot") or current.get("screenshot") or "").strip(),
            "ocr_text": str(entry.get("ocr_text") or current.get("ocr_text") or "").strip(),
            "raw_line": str(entry.get("raw_line") or current.get("raw_line") or "").strip(),
            "entry_time": str(entry.get("entry_time") or current.get("entry_time") or now_iso()),
            "updated_at": now_iso(),
        }

        if not updated["name"]:
            updated["name"] = current.get("name", "")

        self.positions[code] = updated
        self.save()
        self.append_event("upsert", updated)
        return updated

    def close(self, code: str, exit_price: float | None = None, note: str = "") -> dict[str, Any]:
        code = normalize_code(code)
        if code not in self.positions:
            raise ValueError(f"未找到持仓：{code}")
        pos = dict(self.positions[code])
        pos["status"] = "closed"
        pos["exit_price"] = safe_float(exit_price, safe_float(pos.get("current_price")))
        pos["closed_at"] = now_iso()
        if note:
            pos["note"] = (str(pos.get("note") or "").strip() + " " + note.strip()).strip()
        pos["updated_at"] = now_iso()
        self.positions[code] = pos
        self.save()
        self.append_event("close", pos)
        return pos

    def list_positions(self) -> list[dict[str, Any]]:
        items = list(self.positions.values())
        return sorted(items, key=lambda x: (x.get("status", ""), x.get("code", "")))

    def list_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        lines = self.events_file.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    result.append(item)
            except Exception:
                continue
        return list(reversed(result))


def extract_name_guess(before: str, after: str) -> str:
    candidates = []
    for chunk in (before, re.split(r"\d+(?:\.\d+)?", after, maxsplit=1)[0]):
        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", chunk or "")
        if cleaned and not cleaned.isdigit():
            candidates.append(cleaned)
    if candidates:
        return max(candidates, key=len)
    return ""


def parse_ocr_rows(raw_text: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    for line in raw_text.splitlines():
        original = line.strip()
        if not original:
            continue
        match = re.search(r"\b(\d{6})\b", original)
        if not match:
            continue
        code = normalize_code(match.group(1))
        before = original[: match.start()].strip()
        after = original[match.end() :].strip()
        name = extract_name_guess(before, after)
        numbers = re.findall(r"\d+(?:\.\d+)?", after)
        qty = numbers[0] if len(numbers) >= 1 else ""
        cost = numbers[1] if len(numbers) >= 2 else ""
        current = numbers[2] if len(numbers) >= 3 else ""
        rows.append(
            ParsedRow(
                code=code,
                name=name,
                qty=qty,
                cost_price=cost,
                current_price=current,
                raw_line=original,
            )
        )
    return rows


def preprocess_image_for_ocr(image_path: Path) -> Any:
    if Image is None:
        return None
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_image(image_path: Path) -> str:
    if Image is None or pytesseract is None:
        return ""
    img = preprocess_image_for_ocr(image_path)
    if img is None:
        return ""
    configs = ["--psm 6", "--psm 11"]
    for cfg in configs:
        try:
            text = pytesseract.image_to_string(img, lang="chi_sim+eng", config=cfg)
            if text and text.strip():
                return text.strip()
        except Exception:
            continue
    try:
        return pytesseract.image_to_string(img, lang="eng", config="--psm 6").strip()
    except Exception:
        return ""


def suggested_rows(store: PositionStore, code: str | None = None) -> list[dict[str, Any]]:
    items = store.list_positions()
    if code:
        normalized = normalize_code(code)
        for item in items:
            if item.get("code") == normalized:
                return [item]
    return items


def render_message(text: str, level: str = "info") -> str:
    return f'<div class="message {level}">{text}</div>' if text else ""


DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>持仓回写面板</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --line: #dfe5ef;
      --text: #132238;
      --muted: #667085;
      --primary: #2457f5;
      --good: #0a8f4c;
      --warn: #b54708;
      --bad: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .wrap {
      max-width: 1100px;
      margin: 0 auto;
      padding: 14px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 10px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }
    h1 { font-size: 22px; margin: 0; }
    .sub { color: var(--muted); font-size: 13px; }
    .grid {
      display: grid;
      grid-template-columns: 1.05fr 1fr;
      gap: 12px;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
      box-shadow: 0 1px 1px rgba(16,24,40,.02);
    }
    .card h2 { margin: 0 0 10px; font-size: 16px; }
    .message {
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 12px;
      font-size: 14px;
    }
    .message.info { background: #eff4ff; color: #1d4ed8; }
    .message.good { background: #ecfdf3; color: #027a48; }
    .message.warn { background: #fffaeb; color: #b54708; }
    .message.bad { background: #fef3f2; color: #b42318; }
    form { display: grid; gap: 10px; }
    .row {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .row-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .row-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .field { display: grid; gap: 6px; }
    label { font-size: 12px; color: var(--muted); }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 11px;
      font-size: 14px;
      background: #fff;
      color: var(--text);
    }
    textarea { min-height: 88px; resize: vertical; }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    button, .btn {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 14px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    .btn-primary, button.primary { background: var(--primary); color: #fff; }
    .btn-ghost { background: #eef2ff; color: #344054; }
    .btn-warn { background: #fff4e5; color: #b45309; }
    .btn-danger { background: #fef3f2; color: #b42318; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 8px;
      text-align: left;
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; }
    .tag {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      background: #eef2ff;
      color: #3730a3;
      white-space: nowrap;
    }
    .tag.good { background: #ecfdf3; color: #027a48; }
    .tag.warn { background: #fffaeb; color: #b54708; }
    .tag.bad { background: #fef3f2; color: #b42318; }
    .small { font-size: 12px; color: var(--muted); }
    .tight { line-height: 1.45; }
    .preview {
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      background: #fafafa;
    }
    .preview img { width: 100%; display: block; }
    .muted { color: var(--muted); }
    .stack { display: grid; gap: 10px; }
    .form-inline {
      display: flex;
      align-items: end;
      gap: 10px;
      flex-wrap: wrap;
    }
    .form-inline > .field { min-width: 150px; flex: 1 1 160px; }
    .note { white-space: pre-wrap; word-break: break-word; }
    .right { text-align: right; }
    .nowrap { white-space: nowrap; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>持仓回写面板</h1>
        <div class="sub">支持同花顺截图识别，也支持手动录入。最后更新时间：{{ now }}</div>
      </div>
      <div class="actions">
        <a class="btn btn-ghost" href="{{ url_for('index') }}">刷新</a>
      </div>
    </div>

    {{ message_html|safe }}

    <div class="grid">
      <section class="card">
        <h2>上传截图识别</h2>
        <form action="{{ url_for('upload_screenshot') }}" method="post" enctype="multipart/form-data">
          <div class="field">
            <label>同花顺持仓截图</label>
            <input type="file" name="screenshot" accept="image/*" required>
          </div>
          <div class="row row-2">
            <div class="field">
              <label>默认止损百分比</label>
              <input name="stop_pct" value="{{ default_stop_pct }}" placeholder="3.5">
            </div>
            <div class="field">
              <label>默认止盈百分比</label>
              <input name="take_pct" value="{{ default_take_pct }}" placeholder="7.0">
            </div>
          </div>
          <div class="actions">
            <button class="primary" type="submit">上传并识别</button>
          </div>
        </form>
        <div class="small tight" style="margin-top:10px;">
          如果服务器没有安装 OCR 环境，截图也会保存下来，随后可以在下方手动补录。
        </div>
        {% if latest_image %}
        <div class="preview" style="margin-top:10px;">
          <img src="{{ url_for('uploaded_file', filename=latest_image) }}" alt="最新截图">
        </div>
        {% endif %}
      </section>

      <section class="card">
        <h2>手动录入 / 修正持仓</h2>
        <form action="{{ url_for('manual_save') }}" method="post">
          <div class="row row-2">
            <div class="field">
              <label>股票代码</label>
              <input name="code" value="{{ prefill.code }}" placeholder="600000" required>
            </div>
            <div class="field">
              <label>股票名称</label>
              <input name="name" value="{{ prefill.name }}" placeholder="浦发银行">
            </div>
          </div>
          <div class="row row-3">
            <div class="field">
              <label>数量</label>
              <input name="qty" value="{{ prefill.qty }}" placeholder="1000">
            </div>
            <div class="field">
              <label>成本价</label>
              <input name="cost_price" value="{{ prefill.cost_price }}" placeholder="23.68">
            </div>
            <div class="field">
              <label>当前价</label>
              <input name="current_price" value="{{ prefill.current_price }}" placeholder="24.12">
            </div>
          </div>
          <div class="row row-3">
            <div class="field">
              <label>止损百分比</label>
              <input name="stop_pct" value="{{ prefill.stop_pct }}" placeholder="3.5">
            </div>
            <div class="field">
              <label>止盈百分比</label>
              <input name="take_pct" value="{{ prefill.take_pct }}" placeholder="7.0">
            </div>
            <div class="field">
              <label>持仓状态</label>
              <select name="status">
                <option value="holding" {% if prefill.status == 'holding' %}selected{% endif %}>持有中</option>
                <option value="watching" {% if prefill.status == 'watching' %}selected{% endif %}>观察中</option>
                <option value="partial_sell" {% if prefill.status == 'partial_sell' %}selected{% endif %}>部分卖出</option>
                <option value="closed" {% if prefill.status == 'closed' %}selected{% endif %}>已清仓</option>
              </select>
            </div>
          </div>
          <div class="row row-2">
            <div class="field">
              <label>止损价格（可留空自动计算）</label>
              <input name="stop_price" value="{{ prefill.stop_price }}" placeholder="22.82">
            </div>
            <div class="field">
              <label>止盈价格（可留空自动计算）</label>
              <input name="take_price" value="{{ prefill.take_price }}" placeholder="25.30">
            </div>
          </div>
          <div class="field">
            <label>备注</label>
            <textarea name="note" placeholder="比如：突破买入 / 回踩确认 / 计划持有">{{ prefill.note }}</textarea>
          </div>
          <div class="actions">
            <button class="primary" type="submit">保存持仓</button>
            <a class="btn btn-ghost" href="{{ url_for('index', code=prefill.code) }}">预填当前代码</a>
          </div>
        </form>
      </section>
    </div>

    <div class="grid" style="margin-top:12px;">
      <section class="card">
        <h2>当前持仓</h2>
        {% if positions %}
        <table>
          <thead>
            <tr>
              <th>代码 / 名称</th>
              <th>数量 / 成本 / 现价</th>
              <th>止损 / 止盈</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {% for pos in positions %}
            <tr>
              <td>
                <div class="nowrap"><strong>{{ pos.code }}</strong> {{ pos.name }}</div>
                <div class="small">{{ pos.entry_time }}</div>
              </td>
              <td class="tight">
                数量：{{ pos.qty if pos.qty is not none else "-" }}<br>
                成本：{{ pos.cost_price_text }}<br>
                现价：{{ pos.current_price_text }}<br>
                {% if pos.pnl_text %}
                盈亏：<span class="{% if pos.pnl_pct >= 0 %}tag good{% else %}tag bad{% endif %}">{{ pos.pnl_text }}</span>
                {% endif %}
              </td>
              <td class="tight">
                止损：{{ pos.stop_price_text }}<br>
                止盈：{{ pos.take_price_text }}<br>
                <span class="small">止损% {{ pos.stop_pct_text }} / 止盈% {{ pos.take_pct_text }}</span>
              </td>
              <td>
                <span class="tag {% if pos.status == 'holding' %}good{% elif pos.status == 'watching' %}warn{% elif pos.status == 'closed' %}bad{% endif %}">{{ pos.status }}</span>
                <div class="small" style="margin-top:6px;">{{ pos.note }}</div>
              </td>
              <td class="tight">
                <div class="actions">
                  <a class="btn btn-ghost" href="{{ url_for('index', code=pos.code) }}">编辑</a>
                  {% if pos.status != 'closed' %}
                  <form action="{{ url_for('close_position', code=pos.code) }}" method="post" style="display:inline;">
                    <input type="hidden" name="exit_price" value="{{ pos.current_price_text }}">
                    <button class="btn btn-danger" type="submit">清仓</button>
                  </form>
                  {% endif %}
                </div>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}
        <div class="muted">暂无持仓，先上传截图或手动录入一只票。</div>
        {% endif %}
      </section>

      <section class="card">
        <h2>最近操作</h2>
        {% if events %}
        <div class="stack">
          {% for event in events %}
          <div class="card" style="padding:10px; border-radius:8px; background:#fbfcfe;">
            <div class="small">{{ event.ts }} · {{ event.action }}</div>
            <div class="tight">
              {% if event.code %}<strong>{{ event.code }}</strong>{% endif %}
              {% if event.name %}{{ event.name }}{% endif %}
            </div>
            {% if event.note %}
            <div class="small note">{{ event.note }}</div>
            {% endif %}
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="muted">暂无操作记录。</div>
        {% endif %}
      </section>
    </div>
  </div>
</body>
</html>
"""


REVIEW_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OCR 识别确认</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #fff;
      --line: #dfe5ef;
      --text: #132238;
      --muted: #667085;
      --primary: #2457f5;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 14px; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 12px;
    }
    h1 { margin: 0 0 6px; font-size: 22px; }
    .sub { color: var(--muted); font-size: 13px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
    .preview img { width: 100%; display: block; border-radius: 8px; border: 1px solid var(--line); }
    textarea, input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 11px;
      font-size: 14px;
      background: #fff;
      color: var(--text);
    }
    textarea { min-height: 160px; resize: vertical; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; }
    th { color: var(--muted); text-align: left; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button, .btn {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 14px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .primary { background: var(--primary); color: #fff; }
    .ghost { background: #eef2ff; color: #344054; }
    .muted { color: var(--muted); }
    .small { font-size: 12px; color: var(--muted); }
    .tight { line-height: 1.45; }
    .code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>OCR 识别确认</h1>
      <div class="sub">识别结果先修正，再写入持仓文件。截图时间：{{ now }}</div>
      <div class="actions" style="margin-top:10px;">
        <a class="btn ghost" href="{{ url_for('index') }}">返回主页</a>
      </div>
    </div>

    {{ message_html|safe }}

    {% if image_name %}
    <div class="grid">
      <div class="card">
        <h2>截图预览</h2>
        <div class="preview">
          <img src="{{ url_for('uploaded_file', filename=image_name) }}" alt="OCR截图">
        </div>
      </div>
      <div class="card">
        <h2>识别原文</h2>
        <textarea readonly>{{ ocr_text }}</textarea>
      </div>
    </div>
    {% endif %}

    <form class="card" action="{{ url_for('confirm_ocr') }}" method="post">
      <input type="hidden" name="image_name" value="{{ image_name }}">
      <input type="hidden" name="ocr_text" value="{{ ocr_text|e }}">
      <input type="hidden" name="row_count" value="{{ rows|length }}">
      <h2>待确认持仓</h2>
      {% if rows %}
      <table>
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>数量</th>
            <th>成本价</th>
            <th>现价</th>
            <th>止损%</th>
            <th>止盈%</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          {% for row in rows %}
          <tr>
            <td><input class="code" name="code_{{ loop.index0 }}" value="{{ row.code }}"></td>
            <td><input name="name_{{ loop.index0 }}" value="{{ row.name }}"></td>
            <td><input name="qty_{{ loop.index0 }}" value="{{ row.qty }}"></td>
            <td><input name="cost_price_{{ loop.index0 }}" value="{{ row.cost_price }}"></td>
            <td><input name="current_price_{{ loop.index0 }}" value="{{ row.current_price }}"></td>
            <td><input name="stop_pct_{{ loop.index0 }}" value="{{ row.stop_pct }}"></td>
            <td><input name="take_pct_{{ loop.index0 }}" value="{{ row.take_pct }}"></td>
            <td><input name="note_{{ loop.index0 }}" value="{{ row.note }}"></td>
          </tr>
          <tr>
            <td colspan="8" class="small tight">原始行：{{ row.raw_line }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="muted">没有识别到可用持仓，你可以直接在下方手工补一行。</div>
      {% endif %}

      <div class="card" style="padding:12px; margin-top:12px; background:#fbfcfe;">
        <h3 style="margin:0 0 8px;">手工补录一行</h3>
        <div class="grid">
          <div class="field">
            <label class="small">代码</label>
            <input name="manual_code" placeholder="600000">
          </div>
          <div class="field">
            <label class="small">名称</label>
            <input name="manual_name" placeholder="浦发银行">
          </div>
          <div class="field">
            <label class="small">数量</label>
            <input name="manual_qty" placeholder="1000">
          </div>
          <div class="field">
            <label class="small">成本价</label>
            <input name="manual_cost_price" placeholder="23.68">
          </div>
          <div class="field">
            <label class="small">现价</label>
            <input name="manual_current_price" placeholder="24.12">
          </div>
          <div class="field">
            <label class="small">止损%</label>
            <input name="manual_stop_pct" placeholder="3.5">
          </div>
          <div class="field">
            <label class="small">止盈%</label>
            <input name="manual_take_pct" placeholder="7.0">
          </div>
          <div class="field">
            <label class="small">备注</label>
            <input name="manual_note" placeholder="截图没识别到时补录">
          </div>
        </div>
      </div>

      <div class="actions">
        <button class="primary" type="submit">确认并写入持仓</button>
        <a class="btn ghost" href="{{ url_for('index') }}">暂不保存</a>
      </div>
    </form>
  </div>
</body>
</html>
"""


def build_dashboard_prefill(store: PositionStore, code: str | None = None) -> dict[str, Any]:
    base = {
        "code": "",
        "name": "",
        "qty": "",
        "cost_price": "",
        "current_price": "",
        "stop_pct": "",
        "take_pct": "",
        "stop_price": "",
        "take_price": "",
        "status": "holding",
        "note": "",
    }
    if code:
        normalized = normalize_code(code)
        pos = store.positions.get(normalized)
        if pos:
            base.update(
                {
                    "code": pos.get("code", ""),
                    "name": pos.get("name", ""),
                    "qty": pos.get("qty", "") or "",
                    "cost_price": money(pos.get("cost_price")),
                    "current_price": money(pos.get("current_price")),
                    "stop_pct": money(pos.get("stop_pct")),
                    "take_pct": money(pos.get("take_pct")),
                    "stop_price": money(pos.get("stop_price")),
                    "take_price": money(pos.get("take_price")),
                    "status": pos.get("status", "holding"),
                    "note": pos.get("note", ""),
                }
            )
        else:
            base["code"] = normalized
    return base


@APP.get("/")
def index():
    store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    message = request.args.get("message", "")
    level = request.args.get("level", "info")
    prefill = build_dashboard_prefill(store, request.args.get("code"))
    latest_image = request.args.get("image", "")
    positions = []
    for pos in store.list_positions():
        cost_price = safe_float(pos.get("cost_price"))
        current_price = safe_float(pos.get("current_price"))
        pnl_pct = None
        if cost_price and current_price:
            pnl_pct = round((current_price - cost_price) / cost_price * 100, 2)
        positions.append(
            {
                **pos,
                "cost_price_text": money(pos.get("cost_price")),
                "current_price_text": money(pos.get("current_price")),
                "stop_price_text": money(pos.get("stop_price")),
                "take_price_text": money(pos.get("take_price")),
                "stop_pct_text": pct_text(pos.get("stop_pct")),
                "take_pct_text": pct_text(pos.get("take_pct")),
                "pnl_pct": pnl_pct,
                "pnl_text": f"{pnl_pct:+.2f}%" if pnl_pct is not None else "",
            }
        )
    return render_template_string(
        DASHBOARD_TEMPLATE,
        now=now_iso(),
        message_html=render_message(message, level),
        latest_image=latest_image,
        default_stop_pct="3.5",
        default_take_pct="7.0",
        prefill=prefill,
        positions=positions,
        events=store.list_events(10),
    )


def save_uploaded_file(file_storage) -> str:
    filename = secure_filename(file_storage.filename or "screenshot.png")
    suffix = Path(filename).suffix or ".png"
    dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}{suffix}"
    dest_path = UPLOAD_DIR / dest_name
    file_storage.save(dest_path)
    return dest_name


@APP.post("/upload")
def upload_screenshot():
    store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    upload = request.files.get("screenshot")
    if not upload or not upload.filename:
        return redirect(url_for("index", message="没有选择截图文件", level="warn"))

    image_name = save_uploaded_file(upload)
    image_path = UPLOAD_DIR / image_name
    ocr_text = ocr_image(image_path)
    rows = parse_ocr_rows(ocr_text) if ocr_text else []
    store.append_event(
        "upload",
        {
            "image": image_name,
            "ocr_available": bool(ocr_text),
            "row_count": len(rows),
        },
    )
    if not ocr_text:
        rows = []
        message = "截图已保存，但当前服务器没有可用 OCR 环境，已切换为手动确认模式。"
        level = "warn"
    elif not rows:
        message = "识别到了文本，但没有抓到明确持仓行，你可以手动补录。"
        level = "warn"
    else:
        message = f"识别到 {len(rows)} 行候选持仓，请先确认再写入。"
        level = "good"

    return render_template_string(
        REVIEW_TEMPLATE,
        now=now_iso(),
        image_name=image_name,
        ocr_text=ocr_text or "当前未启用 OCR，或识别失败。",
        rows=rows,
        message_html=render_message(message, level),
    )


def collect_confirmed_rows(form) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_count = safe_int(form.get("row_count"), 0) or 0
    for idx in range(row_count):
        code = normalize_code(form.get(f"code_{idx}"))
        name = str(form.get(f"name_{idx}") or "").strip()
        qty = form.get(f"qty_{idx}")
        cost_price = form.get(f"cost_price_{idx}")
        current_price = form.get(f"current_price_{idx}")
        stop_pct = form.get(f"stop_pct_{idx}")
        take_pct = form.get(f"take_pct_{idx}")
        note = form.get(f"note_{idx}")
        if code:
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "qty": qty,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "stop_pct": stop_pct,
                    "take_pct": take_pct,
                    "note": note,
                }
            )
    manual_code = normalize_code(form.get("manual_code"))
    if manual_code:
        rows.append(
            {
                "code": manual_code,
                "name": form.get("manual_name"),
                "qty": form.get("manual_qty"),
                "cost_price": form.get("manual_cost_price"),
                "current_price": form.get("manual_current_price"),
                "stop_pct": form.get("manual_stop_pct"),
                "take_pct": form.get("manual_take_pct"),
                "note": form.get("manual_note"),
            }
        )
    return rows


@APP.post("/confirm-ocr")
def confirm_ocr():
    store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    image_name = str(request.form.get("image_name") or "").strip()
    ocr_text = str(request.form.get("ocr_text") or "").strip()
    rows = collect_confirmed_rows(request.form)
    if not rows:
        return redirect(url_for("index", message="没有可保存的持仓行", level="warn", image=image_name))

    saved = []
    for row in rows:
        row["screenshot"] = image_name
        row["ocr_text"] = ocr_text
        row["status"] = "holding"
        row["source"] = "ocr"
        row.setdefault("note", "")
        saved.append(store.upsert(row, source="ocr"))

    store.append_event(
        "confirm_ocr",
        {
            "image": image_name,
            "count": len(saved),
            "codes": [item.get("code") for item in saved],
        },
    )
    return redirect(url_for("index", message=f"已写入 {len(saved)} 条持仓", level="good", image=image_name, code=saved[0]["code"]))


@APP.post("/manual-save")
def manual_save():
    store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    payload = {
        "code": request.form.get("code"),
        "name": request.form.get("name"),
        "qty": request.form.get("qty"),
        "cost_price": request.form.get("cost_price"),
        "current_price": request.form.get("current_price"),
        "stop_pct": request.form.get("stop_pct"),
        "take_pct": request.form.get("take_pct"),
        "stop_price": request.form.get("stop_price"),
        "take_price": request.form.get("take_price"),
        "status": request.form.get("status"),
        "note": request.form.get("note"),
    }
    try:
        saved = store.upsert(payload, source="manual")
    except Exception as exc:
        return redirect(url_for("index", message=str(exc), level="bad", code=request.form.get("code")))
    return redirect(url_for("index", message=f"已保存 {saved['code']} {saved.get('name', '')}", level="good", code=saved["code"]))


@APP.post("/close/<code>")
def close_position(code: str):
    store = PositionStore(POSITIONS_FILE, EVENTS_FILE)
    exit_price = request.form.get("exit_price")
    note = request.form.get("note", "")
    try:
        store.close(code, exit_price=safe_float(exit_price), note=note)
    except Exception as exc:
        abort(400, description=str(exc))
    return redirect(url_for("index", message=f"已清仓 {normalize_code(code)}", level="good"))


@APP.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    ensure_dirs()
    return send_from_directory(UPLOAD_DIR, filename)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="手机持仓回写网页")
    parser.add_argument("--host", default=app_config.PORTFOLIO_WEB_HOST_DEFAULT)
    parser.add_argument("--port", type=int, default=app_config.PORTFOLIO_WEB_PORT_DEFAULT)
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    ensure_dirs()
    print(f"持仓网页已启动：http://{args.host}:{args.port}", flush=True)
    APP.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
