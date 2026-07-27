"""ToDoChat の PWA アイコンを標準ライブラリ(zlib+struct)だけで生成する。

外部の画像ライブラリ(Pillow 等)に依存せず、青地に白いチェックマークの
アイコンを PNG で描き出す。ホーム画面追加時の maskable 表示に耐えるよう
背景は正方形いっぱいを塗り、チェックは中央 80% のセーフゾーン内に収める。

再生成:  python app/icons/gen_icons.py
出力:    icon-192.png / icon-512.png / icon-180.png(apple-touch-icon 用)
"""
import struct
import zlib
from pathlib import Path

BG = (74, 127, 214)      # accent blue (#4a7fd6)
FG = (255, 255, 255)     # white check

HERE = Path(__file__).resolve().parent


def _dist_to_segment(px, py, ax, ay, bx, by):
    """点(px,py)から線分 a-b までの最短距離。"""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _render(size):
    """size×size の RGB ピクセル列(bytearray, 各行の先頭にフィルタ0)を返す。"""
    # チェックマークの3点(正規化座標)と太さ。
    p = [(0.30, 0.54), (0.44, 0.70), (0.72, 0.34)]
    pts = [(x * size, y * size) for (x, y) in p]
    half = size * 0.055                       # ストローク半径
    aa = size * 0.012                          # アンチエイリアス幅
    raw = bytearray()
    for y in range(size):
        raw.append(0)                          # PNG フィルタタイプ 0
        cy = y + 0.5
        for x in range(size):
            cx = x + 0.5
            d = min(
                _dist_to_segment(cx, cy, *pts[0], *pts[1]),
                _dist_to_segment(cx, cy, *pts[1], *pts[2]),
            )
            # 距離に応じて背景色と前景色を線形ブレンド(簡易アンチエイリアス)。
            a = max(0.0, min(1.0, (half - d) / aa + 0.5)) if aa else (1.0 if d <= half else 0.0)
            r = round(BG[0] + (FG[0] - BG[0]) * a)
            g = round(BG[1] + (FG[1] - BG[1]) * a)
            b = round(BG[2] + (FG[2] - BG[2]) * a)
            raw += bytes((r, g, b))
    return raw


def _png(size, raw):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8bit, RGB
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def main():
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png"), (180, "icon-180.png")):
        (HERE / name).write_bytes(_png(size, _render(size)))
        print("wrote", name)


if __name__ == "__main__":
    main()
