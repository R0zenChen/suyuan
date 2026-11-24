"""Rebuild the Windows launcher icon with Pillow if the visual identity changes."""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


size = 256
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 248, 248), radius=50, fill="#174d3b")
font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "seguisb.ttf"
font = ImageFont.truetype(str(font_path), 178) if font_path.is_file() else ImageFont.load_default(size=178)
box = draw.textbbox((0, 0), "S", font=font)
draw.text(((size - (box[2] - box[0])) / 2 - box[0],
           (size - (box[3] - box[1])) / 2 - box[1] - 4), "S", font=font, fill="#ffffff")
draw.ellipse((182, 43, 216, 77), fill="#a8d8b3")
image.save(Path(__file__).with_name("suyuan.ico"), format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
