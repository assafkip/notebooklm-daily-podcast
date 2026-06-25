#!/usr/bin/env python3
"""Render a v1 podcast cover for the show (kicker from $PODCAST_SHOW_NAME).

Apple/Spotify want a square 1400-3000px RGB JPG/PNG. This makes a clean,
dark 3000x3000 PNG so the feed is submittable. The look is your call -- this
is a starting point, not a final identity. Re-run with different ACCENT / fonts
to iterate, or hand the slot to a designed asset and just `build_rss.py upload`.

Usage:
  make_cover.py [--out cover.png] [--accent "#E8FF4D"]
"""
import argparse
import os

from PIL import Image, ImageDraw, ImageFont

SIZE = 3000
BG = (18, 18, 20)          # near-black charcoal
INK = (245, 245, 245)      # off-white
MUTED = (150, 150, 155)    # subtitle grey
DEFAULT_ACCENT = "#E8FF4D"  # electric lime

SF = "/System/Library/Fonts/SFNS.ttf"
ARIAL_BLACK = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _font(path, size, fallback):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(fallback, size)


def _center_x(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return (SIZE - (box[2] - box[0])) // 2


def render(out_path, accent_hex):
    accent = tuple(int(accent_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)

    title_font = _font(ARIAL_BLACK, 320, SF)
    kicker_font = _font(ARIAL, 150, SF)
    sub_font = _font(ARIAL, 130, SF)

    # Accent bar across the top third.
    draw.rectangle([0, 980, SIZE, 1060], fill=accent)

    # Kicker.
    kicker = os.environ.get("PODCAST_SHOW_NAME", "AI NEWS").upper()[:18]
    draw.text((_center_x(draw, kicker, kicker_font), 660), kicker,
              font=kicker_font, fill=accent)

    # Stacked title.
    for i, line in enumerate(("AI DAILY", "NEWS")):
        y = 1200 + i * 380
        draw.text((_center_x(draw, line, title_font), y), line,
                  font=title_font, fill=INK)

    # Subtitle.
    sub = "AI news that matters. Every morning."
    draw.text((_center_x(draw, sub, sub_font), 2200), sub,
              font=sub_font, fill=MUTED)

    img.save(out_path, "PNG")
    return out_path


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="render the podcast cover")
    parser.add_argument("--out", default=os.path.join(here, "cover.png"))
    parser.add_argument("--accent", default=DEFAULT_ACCENT)
    args = parser.parse_args(argv)
    path = render(args.out, args.accent)
    print(f"cover written: {path}  ({SIZE}x{SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
