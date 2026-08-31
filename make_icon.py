"""Draw the desktop icon: a tile grid with one tile selected.

Rendered large and downsampled into a multi-resolution .ico, so Windows can
pick the right size for the desktop, the taskbar and alt-tab. The shapes are
deliberately blunt — three by three, one accent — because anything finer turns
to porridge at 16 pixels.
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
BACKDROP = (24, 33, 45, 255)     # slate, close to the app's own chrome
TILE = (58, 84, 112, 255)        # unselected tile
SELECTED = (232, 163, 61, 255)   # the amber the app uses for a selection
EDGE = (92, 126, 163, 255)
ICO_SIZES = [(s, s) for s in (256, 128, 64, 48, 32, 24, 16)]
OUT = Path(__file__).resolve().parent / "ign_lidar_tiler.ico"


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad, radius = SIZE // 16, SIZE // 6
    d.rounded_rectangle([pad, pad, SIZE - pad, SIZE - pad], radius, fill=BACKDROP)

    # 3x3 grid, inset from the backdrop
    margin = SIZE * 0.17
    gap = SIZE * 0.035
    span = SIZE - 2 * margin
    cell = (span - 2 * gap) / 3
    tile_radius = int(cell * 0.16)

    for row in range(3):
        for col in range(3):
            x0 = margin + col * (cell + gap)
            y0 = margin + row * (cell + gap)
            box = [x0, y0, x0 + cell, y0 + cell]
            chosen = row >= 1 and col >= 1   # a 2x2 block, the shape of a map selection
            d.rounded_rectangle(box, tile_radius,
                                fill=SELECTED if chosen else TILE,
                                outline=None if chosen else EDGE,
                                width=max(2, SIZE // 220))
    return img


def main():
    img = draw()
    img.save(OUT, format="ICO", sizes=ICO_SIZES)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(ICO_SIZES)} sizes)")
    png = OUT.with_suffix(".png")
    img.resize((256, 256), Image.LANCZOS).save(png)
    print(f"preview {png}")


if __name__ == "__main__":
    main()
