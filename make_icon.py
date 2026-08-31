"""Draw the two desktop icons: start the app, and stop it.

Rendered large and downsampled into multi-resolution .ico files, so Windows
can pick the right size for the desktop, the taskbar and alt-tab. The shapes
are deliberately blunt — three by three, one accent — because anything finer
turns to porridge at 16 pixels.

The stop icon is the same grid gone cold, with the selection greyed out and a
red bar across it, so the pair reads as one app at a glance without the two
icons being confusable.
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
BACKDROP = (24, 33, 45, 255)     # slate, close to the app's own chrome
TILE = (58, 84, 112, 255)        # unselected tile
SELECTED = (232, 163, 61, 255)   # the amber the app uses for a selection
DORMANT = (72, 82, 96, 255)      # the selection with the power off
EDGE = (92, 126, 163, 255)
STOP = (214, 78, 68, 255)
ICO_SIZES = [(s, s) for s in (256, 128, 64, 48, 32, 24, 16)]
HERE = Path(__file__).resolve().parent


def grid(selected_fill):
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad, radius = SIZE // 16, SIZE // 6
    d.rounded_rectangle([pad, pad, SIZE - pad, SIZE - pad], radius, fill=BACKDROP)

    margin, gap = SIZE * 0.17, SIZE * 0.035
    cell = (SIZE - 2 * margin - 2 * gap) / 3
    tile_radius = int(cell * 0.16)

    for row in range(3):
        for col in range(3):
            x0 = margin + col * (cell + gap)
            y0 = margin + row * (cell + gap)
            chosen = row >= 1 and col >= 1   # a 2x2 block, the shape of a map selection
            d.rounded_rectangle([x0, y0, x0 + cell, y0 + cell], tile_radius,
                                fill=selected_fill if chosen else TILE,
                                outline=None if chosen else EDGE,
                                width=max(2, SIZE // 220))
    return img, d


def start_icon():
    img, _ = grid(SELECTED)
    return img


def stop_icon():
    img, d = grid(DORMANT)
    # a bar across the grid: unmistakable at 16 px, where a symbol would not be
    thickness = SIZE * 0.115
    y = SIZE / 2
    d.rounded_rectangle([SIZE * 0.16, y - thickness / 2,
                         SIZE * 0.84, y + thickness / 2],
                        radius=thickness / 2, fill=STOP)
    return img


def write(img, stem):
    ico = HERE / f"{stem}.ico"
    img.save(ico, format="ICO", sizes=ICO_SIZES)
    img.resize((256, 256), Image.LANCZOS).save(HERE / f"{stem}.png")
    print(f"wrote {ico.name} ({ico.stat().st_size:,} bytes) and its png preview")


def main():
    write(start_icon(), "ign_lidar_tiler")
    write(stop_icon(), "ign_lidar_tiler_stop")


if __name__ == "__main__":
    main()
