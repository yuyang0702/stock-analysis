from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def wrap_text(draw: ImageDraw.ImageDraw, text: str, width: int, font_obj: ImageFont.FreeTypeFont) -> list[str]:
    lines: list[str] = []
    current = ""
    for ch in text:
        if draw.textlength(current + ch, font=font_obj) <= width:
            current += ch
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def draw_box(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    colors: dict[str, str],
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    text: str,
    fill: str,
) -> None:
    draw.rounded_rectangle((x1, y1, x2, y2), radius=22, fill=fill, outline=colors["border"], width=3)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(wrap_text(draw, paragraph, x2 - x1 - 40, font))
    total_h = len(lines) * (font.size + 10) - 10 if lines else 0
    start_y = y1 + max(18, ((y2 - y1) - total_h) // 2)
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        draw.text((x1 + (x2 - x1 - tw) / 2, start_y + i * (font.size + 10)), line, fill=colors["text"], font=font)


def draw_arrow(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, color: str) -> None:
    draw.line((x1, y1, x2, y2), fill=color, width=5)
    ang = math.atan2(y2 - y1, x2 - x1)
    length = 18
    for a in (ang + math.pi * 0.85, ang - math.pi * 0.85):
        x = x2 - length * math.cos(a)
        y = y2 - length * math.sin(a)
        draw.line((x2, y2, x, y), fill=color, width=5)


def main() -> Path:
    base = Path.cwd()
    out_dir = base / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "a_share_strategy_flowchart.png"

    image = Image.new("RGB", (1800, 1700), "white")
    draw = ImageDraw.Draw(image)

    colors = {
        "title": "#0f172a",
        "arrow": "#365a88",
        "text": "#10253f",
        "muted": "#56657a",
        "border": "#7da2d6",
    }

    font_path = r"C:\Windows\Fonts\msyh.ttc"
    font_bold_path = r"C:\Windows\Fonts\msyhbd.ttc"
    font = ImageFont.truetype(font_path, 28)
    font_sub = ImageFont.truetype(font_path, 24)
    font_title = ImageFont.truetype(font_bold_path, 40)

    title = "A股风控引擎与扫描通知流程图"
    subtitle = "当前实现：扫描 -> 风控 -> 持仓联动 -> 企业微信推送"
    draw.text((1800 / 2 - draw.textlength(title, font=font_title) / 2, 30), title, fill=colors["title"], font=font_title)
    draw.text((1800 / 2 - draw.textlength(subtitle, font=font_sub) / 2, 88), subtitle, fill=colors["muted"], font=font_sub)

    box_fill_1 = "#f8fbff"
    box_fill_2 = "#eef6ff"
    box_fill_3 = "#f3faf7"
    box_fill_4 = "#fff7ed"

    draw_box(draw, font, colors, 90, 150, 500, 270, "1. 触发运行\n自动识别盘前 / 盘中 / 盘后", box_fill_1)
    draw_box(draw, font, colors, 620, 150, 1100, 270, "2. 拉取基础市场数据\n全市场行情、大盘状态、行业缓存", box_fill_2)
    draw_box(draw, font, colors, 1230, 150, 1710, 270, "3. 组建候选池\n过滤价格、成交额、模式与风险条件", box_fill_3)

    draw_box(draw, font, colors, 90, 340, 500, 470, "4. 补充题材与新闻\n行业映射、个股新闻、公告、龙虎榜", box_fill_1)
    draw_box(draw, font, colors, 620, 340, 1100, 470, "5. 生成技术面快照\nMA5 / MA10 / MA20 / MA30 / ATR14\n支撑位 / 压力位 / 趋势状态", box_fill_2)
    draw_box(draw, font, colors, 1230, 340, 1710, 470, "6. 风控引擎决策\n识别 short / mid\n计算入场价 / 止损 / 止盈 / 仓位 / 置信度", box_fill_3)

    draw_box(draw, font, colors, 90, 540, 500, 670, "7. 持仓联动\n读取 JoinQuant 快照 / 统一有效止损", box_fill_4)
    draw_box(draw, font, colors, 620, 540, 1100, 670, "8. 分流处理\n候选票 -> 入场建议\n持仓票 -> 风控建议", box_fill_2)
    draw_box(draw, font, colors, 1230, 540, 1710, 670, "9. 输出结果\nCSV / Markdown / 终端摘要", box_fill_3)

    draw_box(draw, font, colors, 220, 770, 790, 940, "10. 企业微信通知\n手机端摘要、去重、冷却、重点信号优先推送", box_fill_1)
    draw_box(draw, font, colors, 1010, 770, 1580, 940, "11. 后续维护闭环\n持仓页回写 -> 再次扫描 -> 更新止损止盈 / 买点判断", box_fill_4)

    draw_box(draw, font, colors, 340, 1060, 1460, 1230, "结果用途\n用于盘前选股、盘中临近买点提醒、盘后复盘和历史推演\n不自动下单，只给出可解释、可回测、可调参的建议", box_fill_2)

    draw_box(draw, font, colors, 120, 1340, 1680, 1540, "关键约束\n1. 低频扫描，减少封禁风险\n2. 新闻只抓催化，不做噪声堆叠\n3. 手机消息尽量一屏看完\n4. 风控优先于止盈，止损先定\n5. 同一标的不重复刷屏", box_fill_3)

    draw_arrow(draw, 500, 210, 620, 210, colors["arrow"])
    draw_arrow(draw, 1100, 210, 1230, 210, colors["arrow"])
    draw_arrow(draw, 1480, 270, 1480, 340, colors["arrow"])
    draw_arrow(draw, 500, 405, 620, 405, colors["arrow"])
    draw_arrow(draw, 1100, 405, 1230, 405, colors["arrow"])
    draw_arrow(draw, 1480, 470, 1480, 540, colors["arrow"])
    draw_arrow(draw, 500, 605, 620, 605, colors["arrow"])
    draw_arrow(draw, 1100, 605, 1230, 605, colors["arrow"])
    draw_arrow(draw, 960, 670, 960, 770, colors["arrow"])
    draw_arrow(draw, 510, 840, 220, 840, colors["arrow"])
    draw_arrow(draw, 790, 840, 1010, 840, colors["arrow"])
    draw_arrow(draw, 1050, 940, 1050, 1060, colors["arrow"])
    draw_arrow(draw, 840, 1230, 840, 1340, colors["arrow"])

    image.save(out)
    return out


if __name__ == "__main__":
    print(main())
