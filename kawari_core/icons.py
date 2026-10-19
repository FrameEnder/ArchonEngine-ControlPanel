"""Item icons from the game, as PNGs a browser can show.

The game keeps icons as ``.tex`` textures under ``ui/icon/`` in its sqpack.
Kawari Web never ships them: ``kawari-questdump extract`` copies the ones the
shop needs out of the server's own game install, and this turns each into a
PNG in the data folder (Pillow decodes the compressed formats).

A ``.tex`` file is an 80-byte header, then the image and its mipmaps:

    0   u32  attribute flags
    4   u32  format (below)
    8   u16  width
    10  u16  height
    12  u16  depth
    14  u16  mip levels (newer files: u8 mip levels, u8 array size)
    16  u32  x3  LoD mip offsets
    28  u32  x13 offset of each surface; [0] is the full-size image

Only the full-size image is read.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

HEADER = 80

# Texture formats (values as the game stores them).
L8 = 0x1130
A8 = 0x1131
B4G4R4A4 = 0x1440
B5G5R5A1 = 0x1441
B8G8R8A8 = 0x1450
B8G8R8X8 = 0x1451
BC1 = 0x3420
BC2 = 0x3430
BC3 = 0x3431
BC5 = 0x6230
BC7 = 0x6432

# format: (bits per pixel or None, block-compressed: Pillow's BCn number)
_BCN = {BC1: 1, BC2: 2, BC3: 3, BC5: 5, BC7: 7}
_BYTES_PER_BLOCK = {1: 8, 2: 16, 3: 16, 5: 16, 7: 16}


class TexError(ValueError):
    pass


def game_paths(icon_id: int) -> list[str]:
    """Where the game keeps an icon: the high-resolution copy first."""
    icon_id = int(icon_id)
    folder = f"ui/icon/{icon_id // 1000 * 1000:06d}"
    return [f"{folder}/{icon_id:06d}_hr1.tex", f"{folder}/{icon_id:06d}.tex"]


def icon_id_from_name(file_name: str) -> tuple[int, bool] | None:
    """(icon ID, is high-resolution) from an extracted file's name, e.g. 050443_hr1.tex."""
    stem = Path(file_name).name
    if not stem.endswith(".tex"):
        return None
    stem = stem[:-4]
    hires = stem.endswith("_hr1")
    if hires:
        stem = stem[:-4]
    return (int(stem), hires) if stem.isdigit() else None


def _expand4(value: int) -> int:
    return value * 17


def decode(data: bytes):
    """The full-size image in a .tex file, as a Pillow image."""
    from PIL import Image

    if len(data) < HEADER:
        raise TexError("too short to be a texture")
    fmt, width, height = struct.unpack_from("<IHH", data, 4)
    surface = struct.unpack_from("<I", data, 28)[0] or HEADER
    if not width or not height or width > 4096 or height > 4096:
        raise TexError(f"unlikely size {width}x{height}")
    body = data[surface:]
    size = (width, height)

    if fmt in _BCN:
        n = _BCN[fmt]
        blocks = ((width + 3) // 4) * ((height + 3) // 4)
        need = blocks * _BYTES_PER_BLOCK[n]
        if len(body) < need:
            raise TexError("texture data is cut short")
        mode = "RGB" if n == 5 else "RGBA"
        return Image.frombytes(mode, size, body[:need], "bcn", n)

    pixels = width * height
    if fmt == B8G8R8A8:
        return Image.frombytes("RGBA", size, body[:pixels * 4], "raw", "BGRA")
    if fmt == B8G8R8X8:
        return Image.frombytes("RGB", size, body[:pixels * 4], "raw", "BGRX")
    if fmt in (L8, A8):
        plane = Image.frombytes("L", size, body[:pixels])
        if fmt == L8:
            return plane
        out = Image.new("RGBA", size, (255, 255, 255, 0))
        out.putalpha(plane)
        return out
    if fmt in (B4G4R4A4, B5G5R5A1):
        raw = body[:pixels * 2]
        if len(raw) < pixels * 2:
            raise TexError("texture data is cut short")
        out = bytearray(pixels * 4)
        for i, (value,) in enumerate(struct.iter_unpack("<H", raw)):
            if fmt == B4G4R4A4:
                b, g, r, a = (_expand4(value & 0xF), _expand4(value >> 4 & 0xF),
                              _expand4(value >> 8 & 0xF), _expand4(value >> 12 & 0xF))
            else:
                b, g, r = ((value & 0x1F) * 255 // 31, (value >> 5 & 0x1F) * 255 // 31,
                           (value >> 10 & 0x1F) * 255 // 31)
                a = 255 if value & 0x8000 else 0
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
        return Image.frombytes("RGBA", size, bytes(out))
    raise TexError(f"unsupported texture format 0x{fmt:04x}")


def to_png(data: bytes) -> bytes:
    """A .tex file's full-size image as PNG bytes."""
    image = decode(data)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def convert_folder(source: Path, destination: Path) -> tuple[int, list[str]]:
    """Turn every extracted icon in source into <icon id>.png in destination.

    The high-resolution copy wins when both were extracted. Returns how many
    were written and a message for each file that couldn't be read.
    """
    destination.mkdir(parents=True, exist_ok=True)
    best: dict[int, tuple[bool, Path]] = {}
    for path in sorted(Path(source).glob("*.tex")):
        parsed = icon_id_from_name(path.name)
        if not parsed:
            continue
        icon_id, hires = parsed
        if icon_id not in best or (hires and not best[icon_id][0]):
            best[icon_id] = (hires, path)
    written, problems = 0, []
    for icon_id, (_hires, path) in sorted(best.items()):
        try:
            png = to_png(path.read_bytes())
        except (TexError, OSError, ValueError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        (destination / f"{icon_id}.png").write_bytes(png)
        written += 1
    return written, problems
