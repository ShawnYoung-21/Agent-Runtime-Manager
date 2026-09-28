# -*- coding: utf-8 -*-
"""生成 ARM 品牌图标：盾牌 + 心跳脉冲线（守护 × 生命监测）。

设计语义：
- 盾牌 = 守护/保护（产品的核心动作）
- 盾牌上的绿色脉冲线 = Agent 生命体征监测（三信号感知）
- 深蓝底 = 冷静可靠的工程感；绿色脉冲 = 活着/运行中
输出：多尺寸 .ico（托盘/窗口用）+ 大尺寸 png（文档/发布用）
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(exist_ok=True)

# 品牌色
BG_DARK = (13, 16, 23, 255)        # #0d1017 深底
SHIELD_BLUE = (56, 88, 233, 255)   # #3858e9 盾牌主色（类 Edge 蓝，辨识度）
SHIELD_LIGHT = (100, 128, 243, 255)
PULSE_GREEN = (74, 222, 128, 255)  # #4ade80 生命脉冲
EDGE = (140, 160, 245, 255)


def draw_logo(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = s / 64.0  # 以 64 为基准的缩放因子

    # 圆角方底
    r = int(12 * f)
    d.rounded_rectangle([2*f, 2*f, s-2*f, s-2*f], radius=r, fill=BG_DARK)

    # 盾牌（上宽下尖）：用多边形
    cx = s / 2
    top = int(11 * f)
    bot = int(54 * f)
    half_w = int(19 * f)   # 盾牌半宽
    mid = int(40 * f)      # 收腰位置
    shield = [
        (cx - half_w, top),
        (cx + half_w, top),
        (cx + half_w, mid),
        (cx, bot),                # 右下斜边到尖点
        (cx - half_w, mid),
    ]
    d.polygon(shield, fill=SHIELD_BLUE)
    # 盾牌内描边（高光）
    d.line(shield + [shield[0]], fill=EDGE, width=max(1, int(1.5*f)))

    # 心跳脉冲线（横穿盾牌中部）
    y = int(28 * f)
    lw = max(2, int(3.5 * f))
    pts = [
        (cx - int(14*f), y),
        (cx - int(7*f), y),
        (cx - int(4*f), y - int(6*f)),
        (cx - int(1*f), y + int(7*f)),
        (cx + int(2*f), y - int(4*f)),
        (cx + int(4*f), y),
        (cx + int(14*f), y),
    ]
    d.line(pts, fill=PULSE_GREEN, width=lw, joint="curve")
    return img


# 多尺寸 ico
base = draw_logo(256)
base.save(OUT / "arm_logo_256.png")
base.resize((64, 64), Image.LANCZOS).save(OUT / "arm_logo_64.png")
base.resize((32, 32), Image.LANCZOS).save(OUT / "arm_logo_32.png")
base.resize((16, 16), Image.LANCZOS).save(OUT / "arm_logo_16.png")
base.save(OUT / "arm.ico", sizes=[(256, 256), (64, 64), (32, 32), (16, 16)])
print("icons saved to", OUT)
