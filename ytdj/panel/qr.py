"""A minimal QR code encoder — byte mode, versions 1–6, error correction L/M.

Enough for a URL on the panel (up to ~100 bytes) without another dependency
on the Pi. Follows ISO/IEC 18004 the way Project Nayuki's reference
implementation lays it out; `encode()` returns rows of booleans (True = dark),
without the quiet zone.
"""

from __future__ import annotations

# index 1..6; [ecl][version]
_ECC_PER_BLOCK = {"L": (0, 7, 10, 15, 20, 26, 18), "M": (0, 10, 16, 26, 18, 24, 16)}
_NUM_BLOCKS = {"L": (0, 1, 1, 1, 1, 1, 2), "M": (0, 1, 1, 1, 2, 2, 4)}
_FORMAT_ECL = {"L": 1, "M": 0}
MAX_VERSION = 6


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
    return result


def _capacity(ver: int, ecl: str) -> int:
    """Data codewords (bytes) for a version and level."""
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _NUM_BLOCKS[ecl][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int, size: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    return sorted([6] + [size - 7 - i * step for i in range(n - 1)])


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Grid:
    def __init__(self, ver: int) -> None:
        self.ver = ver
        self.size = ver * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver, n)
        last = len(pos) - 1
        for i, ay in enumerate(pos):
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits("M", 0)  # reserve; the real bits come later

    def format_bits(self, ecl: str, mask: int) -> None:
        data = _FORMAT_ECL[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0  # noqa: E731
        n = self.size
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, True)

    def codewords(self, data: list[int]) -> None:
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.m[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        f = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self) -> int:
        n, m = self.size, self.m
        score = 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run = 1
            for k in range(1, n + 1):
                if k < n and line[k] == line[k - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            for k in range(n - 10):
                seg = line[k : k + 11]
                if seg == finder_a or seg == finder_b:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = n * n
        score += (abs(dark * 20 - total * 10) + total - 1) // total * 10
        return score


def encode(text: str, ecl: str = "M") -> list[list[bool]]:
    payload = text.encode("utf-8")
    ver = next((v for v in range(1, MAX_VERSION + 1) if _capacity(v, ecl) >= len(payload) + 2), 0)
    if not ver:
        raise ValueError(f"text je na QR kód verze ≤{MAX_VERSION} moc dlouhý ({len(payload)} B)")
    cap = _capacity(ver, ecl)

    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)  # byte mode
    put(len(payload), 8)
    for b in payload:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    put(0, (-len(bits)) % 8)
    data = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap:
        data.append(pad)
        pad ^= 0xEC ^ 0x11

    # split into blocks, add error correction, interleave
    nblocks, ecclen = _NUM_BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    shortlen = raw // nblocks
    div = _rs_divisor(ecclen)
    blocks = []
    k = 0
    for i in range(nblocks):
        dat = data[k : k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    words = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                words.append(blk[i])

    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8):
        g = _Grid(ver)
        g.function_patterns()
        g.codewords(words)
        g.apply_mask(mask)
        g.format_bits(ecl, mask)
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g.m)
    assert best is not None
    return best[1]
