"""生成双码赞赏卡片（微信 + 支付宝）。

用法：
    ~/.videoseek-venv/bin/python packaging/make-donate-card.py

依赖 PIL（venv 里已装）。二维码图片默认读取：
    docs/assets/wechat-donate.jpg  （微信赞赏码）
    docs/assets/alipay.jpg         （支付宝转账码）

输出：docs/assets/donate-card.png

改文案/配色/尺寸直接改下方常量即可。
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

# ---- 输入/输出 ----
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WECHAT_PATH = os.path.join(BASE, "docs", "assets", "wechat-donate.jpg")
ALIPAY_PATH = os.path.join(BASE, "docs", "assets", "alipay.jpg")
OUT_PATH = os.path.join(BASE, "docs", "assets", "donate-card.png")

# ---- 文案 ----
TITLE = "请我喝杯咖啡"
SUBTITLE = "开发不易，你的支持是我持续更新的动力"
WECHAT_LABEL = "微信赞赏"
ALIPAY_LABEL = "支付宝转账"
SCAN_HINT = "扫一扫赞赏"
FOOTER = "感谢每一份支持 · 硬核水墨"

# ---- 配色（RGB）----
BG_TOP = (255, 247, 237)
BG_BOTTOM = (255, 236, 216)
TITLE_COLOR = (82, 56, 30)
SUB_COLOR = (150, 120, 90)
WECHAT_COLOR = (7, 150, 100)
ALIPAY_COLOR = (22, 120, 200)
HINT_COLOR = (140, 120, 100)
CARD_FILL = (255, 255, 255)
CARD_BORDER = (235, 222, 205)

# ---- 尺寸 ----
PAD = 70
CARD_W = 1400
TITLE_H = 210
FOOT_H = 90
QR_TARGET_H = 660
QR_GAP = 100


def find_font(size: int):
    for c in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ):
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    return ImageFont.load_default()


def fit_size(im: Image.Image, target_h: int):
    w, h = im.size
    return int(w * target_h / h), target_h


def main() -> None:
    wechat = Image.open(WECHAT_PATH).convert("RGB")
    alipay = Image.open(ALIPAY_PATH).convert("RGB")

    wechat_fit = fit_size(wechat, QR_TARGET_H)
    alipay_fit = fit_size(alipay, QR_TARGET_H)

    label_h = 130
    qr_area_h = QR_TARGET_H + 40 + label_h
    card_h = TITLE_H + qr_area_h + FOOT_H + 30

    img = Image.new("RGB", (CARD_W + PAD * 2, card_h + PAD * 2), (255, 255, 255))
    d = ImageDraw.Draw(img)

    # 卡片背景渐变
    grad = Image.new("RGB", (CARD_W, card_h))
    gd = ImageDraw.Draw(grad)
    for y in range(card_h):
        t = y / card_h
        gd.line(
            [(0, y), (CARD_W, y)],
            fill=tuple(int(BG_TOP[i] * (1 - t) + BG_BOTTOM[i] * t) for i in range(3)),
        )
    mask = Image.new("L", (CARD_W, card_h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, CARD_W, card_h], radius=40, fill=255)
    img.paste(grad, (PAD, PAD), mask)

    # 标题
    title_font = find_font(72)
    sub_font = find_font(34)
    tw = d.textlength(TITLE, font=title_font)
    sw = d.textlength(SUBTITLE, font=sub_font)
    d.text((PAD + (CARD_W - tw) // 2, PAD + 50), TITLE, font=title_font, fill=TITLE_COLOR)
    d.text((PAD + (CARD_W - sw) // 2, PAD + 140), SUBTITLE, font=sub_font, fill=SUB_COLOR)

    label_font = find_font(42)
    scan_font = find_font(30)

    def draw_qr(cx: int, qr_img: Image.Image, label: str, label_color: tuple):
        fw, fh = fit_size(qr_img, QR_TARGET_H)
        card_w = max(fw, 300) + 40
        card_h_box = fh + 40
        x0 = cx - card_w // 2
        y0 = PAD + TITLE_H + 20
        d.rounded_rectangle(
            [x0, y0, x0 + card_w, y0 + card_h_box],
            radius=24,
            fill=CARD_FILL,
            outline=CARD_BORDER,
            width=3,
        )
        qx = cx - fw // 2
        qy = y0 + 20
        qr = qr_img.resize((fw, fh), Image.LANCZOS)
        img.paste(qr, (qx, qy))

        lt = d.textlength(label, font=label_font)
        d.text((cx - lt // 2, y0 + card_h_box + 22), label, font=label_font, fill=label_color)
        st = d.textlength(SCAN_HINT, font=scan_font)
        d.text((cx - st // 2, y0 + card_h_box + 76), SCAN_HINT, font=scan_font, fill=HINT_COLOR)

    draw_qr(PAD + CARD_W // 4, wechat, WECHAT_LABEL, WECHAT_COLOR)
    draw_qr(PAD + CARD_W * 3 // 4, alipay, ALIPAY_LABEL, ALIPAY_COLOR)

    # 底部脚注
    foot_font = find_font(30)
    ft = d.textlength(FOOTER, font=foot_font)
    d.text(
        (PAD + (CARD_W - ft) // 2, PAD + card_h - FOOT_H + 16),
        FOOTER,
        font=foot_font,
        fill=HINT_COLOR,
    )

    img.save(OUT_PATH)
    print("saved", OUT_PATH, img.size)


if __name__ == "__main__":
    main()
