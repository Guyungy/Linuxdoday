# -*- coding: utf-8 -*-
"""
生成应用图标
"""

from PIL import Image, ImageDraw, ImageFont
import os

def create_icon():
    """创建应用图标"""
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        # 创建图像
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 背景圆形 - 深蓝色
        padding = size // 10
        draw.ellipse(
            [padding, padding, size - padding, size - padding],
            fill='#0f3460'
        )

        # 内圈 - 青色
        inner_padding = size // 5
        draw.ellipse(
            [inner_padding, inner_padding, size - inner_padding, size - inner_padding],
            fill='#1a1a2e'
        )

        # 中心 "L" 字母
        try:
            font_size = size // 2
            font = ImageFont.truetype("arial.ttf", font_size)
        except:
            font = ImageFont.load_default()

        text = "L"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - size // 10
        draw.text((x, y), text, fill='#00d9ff', font=font)

        images.append(img)

    # 保存为 ICO
    icon_path = os.path.join(os.path.dirname(__file__), 'icon.ico')
    images[0].save(
        icon_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print(f"图标已生成: {icon_path}")
    return icon_path

if __name__ == "__main__":
    create_icon()
