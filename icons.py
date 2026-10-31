"""Small PNG icons for the home screen. No third-party image library."""
import struct
import zlib


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(size, pixel):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y, size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def _pixel(x, y, size):
    margin = int(size * 0.10)
    if not (margin <= x < size - margin and margin <= y < size - margin):
        return (7, 9, 13)
    inner = int(size * 0.22)
    if inner <= x < size - inner and inner <= y < int(size * 0.40):
        return (95, 224, 255)
    block = int(size * 0.46)
    block_end = int(size * 0.74)
    if block <= x < block_end and block <= y < block_end:
        return (255, 77, 94)
    return (15, 20, 28)


_CACHE = {}


def icon_png(size):
    if size not in _CACHE:
        _CACHE[size] = _png(size, _pixel)
    return _CACHE[size]


MANIFEST = """{
  "name": "Trevor Small Cockpit",
  "short_name": "Cockpit",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#07090d",
  "theme_color": "#07090d",
  "icons": [
    {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any"},
    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}
  ]
}
"""
