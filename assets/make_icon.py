"""生成桌面图标 claude_switch.ico（只用标准库）。

图案和界面同一套风格：深色圆角方块 + 荧光绿的终端提示符「>_」，右上角一对青色的切换箭头。
用有向距离场画，自带抗锯齿；小尺寸自动去掉扫描线、光晕和箭头这些细节。
改了颜色或形状后重新运行：
    python assets/make_icon.py
"""

import math
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
UNIT = 256  # 下面的坐标都按 256×256 的画布写

BG_TOP, BG_BOTTOM = (0x0B, 0x22, 0x14), (0x02, 0x07, 0x04)
EDGE = (0x00, 0xFF, 0x7B)
GREEN = (0x00, 0xFF, 0x7B)
CYAN = (0x00, 0xE5, 0xFF)

TILE = (14, 14, 242, 242, 54)  # 圆角方块：左、上、右、下、圆角半径
CHEVRON = [(58, 72), (90, 72), (146, 130), (90, 188), (58, 188), (114, 130)]  # 「>」
CURSOR = (150, 166, 206, 190)  # 「_」
ARROWS = [  # 两条反向的箭头：(杆, 箭头三角)
    ((152, 64, 192, 74), [(190, 52), (212, 69), (190, 86)]),
    ((172, 102, 212, 112), [(174, 90), (152, 107), (174, 124)]),
]


def sd_box(px, py, x0, y0, x1, y1, r=0.0):
    """到圆角矩形边缘的有向距离（里面为负）。"""
    qx = abs(px - (x0 + x1) / 2) - ((x1 - x0) / 2 - r)
    qy = abs(py - (y0 + y1) / 2) - ((y1 - y0) / 2 - r)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def sd_polygon(px, py, points):
    """到多边形边缘的有向距离（里面为负）。"""
    best = (px - points[0][0]) ** 2 + (py - points[0][1]) ** 2
    sign = 1
    j = len(points) - 1
    for i, (ax, ay) in enumerate(points):
        bx, by = points[j]
        ex, ey, wx, wy = bx - ax, by - ay, px - ax, py - ay
        h = max(0.0, min(1.0, (wx * ex + wy * ey) / (ex * ex + ey * ey)))
        best = min(best, (wx - ex * h) ** 2 + (wy - ey * h) ** 2)
        inside = (py >= ay, py < by, ex * wy > ey * wx)
        if all(inside) or not any(inside):
            sign = -sign
        j = i
    return sign * math.sqrt(best)


def over(dst, color, alpha):
    """把一层颜色按透明度叠到 dst（r, g, b, a，直通 alpha）上。"""
    if alpha <= 0:
        return dst
    r, g, b, a = dst
    out = alpha + a * (1 - alpha)
    mix = lambda c, d: (c * alpha + d * a * (1 - alpha)) / out
    return (mix(color[0], r), mix(color[1], g), mix(color[2], b), out)


def render(size: int) -> list:
    """按尺寸画一张图标，返回每行的 RGBA 字节。"""
    k = UNIT / size  # 一个像素有多少画布单位
    cover = lambda d: max(0.0, min(1.0, 0.5 - d / k))
    detail = size >= 40
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            u, v = (x + 0.5) * k, (y + 0.5) * k
            pixel = (0.0, 0.0, 0.0, 0.0)
            d_tile = sd_box(u, v, *TILE)
            t = max(0.0, min(1.0, (v - TILE[1]) / (TILE[3] - TILE[1])))
            bg = tuple(top + (bottom - top) * t for top, bottom in zip(BG_TOP, BG_BOTTOM))
            if detail and int(v) % 8 < 3:  # 扫描线
                bg = tuple(c * 0.72 for c in bg)
            pixel = over(pixel, bg, cover(d_tile))
            ring = abs(d_tile + max(5.0, k * 0.6)) - max(3.0, k * 0.55)  # 内描边
            pixel = over(pixel, EDGE, cover(ring) * (0.55 if detail else 0.8) * (1.0 - 0.45 * t))
            d_glyph = min(sd_polygon(u, v, CHEVRON), sd_box(u, v, *CURSOR))
            if size >= 32:  # 光晕
                pixel = over(pixel, GREEN, 0.42 * math.exp(-max(d_glyph, 0.0) / 13) * cover(d_tile))
            pixel = over(pixel, GREEN, cover(d_glyph))
            if detail:
                d_arrow = min(min(sd_box(u, v, *shaft), sd_polygon(u, v, head)) for shaft, head in ARROWS)
                pixel = over(pixel, CYAN, 0.22 * math.exp(-max(d_arrow, 0.0) / 9) * cover(d_tile))
                pixel = over(pixel, CYAN, cover(d_arrow))
            r, g, b, a = pixel
            row += bytes((round(r), round(g), round(b), round(a * 255)))
        rows.append(bytes(row))
    return rows


def png(size: int, rows: list) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images: dict) -> bytes:
    """把各尺寸的 PNG 打包成 .ico（Vista 起支持 PNG 格式的图标帧）。"""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images.items():
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> None:
    images = {size: png(size, render(size)) for size in SIZES}
    target = HERE / "claude_switch.ico"
    target.write_bytes(ico(images))
    (HERE / "claude_switch.png").write_bytes(images[256])
    print(f"已生成 {target}（{', '.join(map(str, SIZES))}）")


if __name__ == "__main__":
    main()
