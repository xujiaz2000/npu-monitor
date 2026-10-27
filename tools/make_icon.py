# -*- coding: utf-8 -*-
"""生成应用图标 (纯标准库)

- 设计: Win11 深色圆角方底 + 2×2 NPU 状态方块 (3 绿=空闲, 1 橙=占用)
- 输出: icon.ico (16~256 七尺寸) / icon.png (256)
- 并把 ICO 的 base64 写回 npu_monitor.py 的 ICON_ICO_B64 占位常量

用法: python tools/make_icon.py
"""
import base64
import os
import re
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BG     = (33, 33, 33, 255)      # #212121 深色卡片底
GREEN  = (108, 203, 95, 255)    # #6ccb5f 空闲
ORANGE = (255, 184, 77, 255)    # #ffb84d 占用


def rr_cov(x, y, cx, cy, hw, hh, r):
    """圆角矩形覆盖度 (SDF 抗锯齿, 0~1)"""
    qx = abs(x - cx) - (hw - r)
    qy = abs(y - cy) - (hh - r)
    if qx > 0 or qy > 0:
        dist = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 - r
    else:
        dist = max(qx, qy) - r
    return max(0.0, min(1.0, 0.5 - dist))


def render(size):
    """渲染指定尺寸的 RGBA 像素行"""
    half = size / 2.0
    rows = []
    pad = size * 0.22
    gap = size * 0.06
    tile = (size - 2 * pad - gap) / 2.0
    r_bg = size * 0.225
    r_t = tile * 0.24
    cells = [(0, 0, GREEN), (1, 0, ORANGE), (0, 1, GREEN), (1, 1, GREEN)]
    for py in range(size):
        row = bytearray()
        y = py + 0.5
        for px in range(size):
            x = px + 0.5
            cov = rr_cov(x, y, half, half, half - 0.5, half - 0.5, r_bg)
            r, g, b = BG[0], BG[1], BG[2]
            if cov > 0:
                a = cov
                for gx, gy, col in cells:
                    cx = pad + tile / 2 + gx * (tile + gap)
                    cy = pad + tile / 2 + gy * (tile + gap)
                    if abs(x - cx) > tile / 2 + 2 or abs(y - cy) > tile / 2 + 2:
                        continue
                    t = rr_cov(x, y, cx, cy, tile / 2, tile / 2, r_t)
                    if t > 0:
                        r = int(col[0] * t + r * (1 - t))
                        g = int(col[1] * t + g * (1 - t))
                        b = int(col[2] * t + b * (1 - t))
                row += bytes((r, g, b, int(255 * a)))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(row)
    return rows


def png_bytes(rows, w, h):
    raw = b''.join(b'\x00' + bytes(r) for r in rows)

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
            chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def ico_bytes(images):
    """images: [(size, png_bytes)]"""
    out = struct.pack('<HHH', 0, 1, len(images))
    offset = 6 + 16 * len(images)
    blobs = b''
    for size, data in images:
        out += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0,
                           1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return out + blobs


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for s in sizes:
        images.append((s, png_bytes(render(s), s, s)))
    ico = ico_bytes(images)
    ico_path = os.path.join(ROOT, 'icon.ico')
    with open(ico_path, 'wb') as f:
        f.write(ico)
    png_path = os.path.join(ROOT, 'icon.png')
    with open(png_path, 'wb') as f:
        f.write(images[-1][1])          # 256px
    b64 = base64.b64encode(ico).decode('ascii')

    # 写回 npu_monitor.py 的常量。兼容首次的空占位和已经嵌入过的块，
    # 保证改图标后重复运行也能更新。
    src = os.path.join(ROOT, 'npu_monitor.py')
    with open(src, 'r', encoding='utf-8', newline='') as f:
        txt = f.read()
    new_block = "ICON_ICO_B64 = (\n" + "\n".join(
        "    '%s'" % b64[i:i + 100] for i in range(0, len(b64), 100)) + "\n)"
    pat = re.compile(r"ICON_ICO_B64 = (?:''|\(\s*(?:'[^']*'\s*)+\))", re.S)
    if not pat.search(txt):
        print('!! 未在 npu_monitor.py 找到 ICON_ICO_B64 块, 跳过嵌入')
        return 1
    txt = pat.sub(lambda _m: new_block, txt, count=1)
    with open(src, 'w', encoding='utf-8', newline='') as f:
        f.write(txt)
    print('icon.ico  %6.1f KB (%s)' % (len(ico) / 1024.0,
                                       '/'.join(str(s) for s in sizes)))
    print('icon.png  %6.1f KB (256px)' % (len(images[-1][1]) / 1024.0))
    print('ICON_ICO_B64 已嵌入 npu_monitor.py (%d 字符)' % len(b64))


if __name__ == '__main__':
    main()
