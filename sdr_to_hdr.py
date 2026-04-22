"""sdr_to_hdr.py — SDR PNG/JPEG → Ultra HDR JPEG (gain map format)

Ultra HDR = standard 8-bit JPEG base + embedded gain map (Google / ISO 21496-1).
Supported by: Chrome 116+, Android 14+, iOS 17.4+, Windows 11 24H2 Photos.
On SDR devices: shows normal JPEG. On HDR displays: applies gain map to boost.

Open-source algorithm used here:
  - Inverse TMO: Banterle-style knee + power highlight expansion
  - Gain map: per-pixel log2 luminance ratio (HDR vs SDR), BT.2020-normalised
  - Assembly: JPEG + XMP (hdrgm:) + MPF APP2 (Multi-Picture Format CIPA DC-007)

Usage:
  python sdr_to_hdr.py input.png [--peak-nits 1000] [--sdr-white 203] [--quality 92]
"""
from __future__ import annotations

import argparse
import io
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image


# ── Transfer functions ──────────────────────────────────────────────────────

def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


# ── BT.709 → BT.2020 (linear light) ────────────────────────────────────────

BT709_TO_BT2020 = np.array([
    [0.6274040, 0.3292820, 0.0433136],
    [0.0690970, 0.9195400, 0.0113612],
    [0.0163916, 0.0880132, 0.8955950],
], dtype=np.float32)


# ── Inverse tone mapping ────────────────────────────────────────────────────

def inverse_tone_map(
    rgb_lin: np.ndarray,        # H×W×3, linear SDR [0,1]
    sdr_white: float = 203.0,   # nits at SDR ref white (BT.2408)
    peak_nits: float = 1000.0,
    shadow_boost: float = 1.3,  # minimum boost applied to darks (so shadows also "pop" in HDR)
    hi_gamma: float = 1.0,      # curve exponent; <1 boosts mids more, >1 reserves boost for highlights
) -> np.ndarray:
    """Expand SDR linear to HDR (nits) with a visible, monotonic per-pixel boost.

    Boost curve:  boost(L) = shadow_boost + (peak_boost - shadow_boost) * L^hi_gamma
    i.e. every pixel gets at least `shadow_boost` times brighter, highlights reach peak_boost.
    Avoids the darkening artefact of a knee-based curve.
    """
    w = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    L = np.clip((rgb_lin * w).sum(-1, keepdims=True), 1e-6, 1.0)
    peak_boost = peak_nits / sdr_white
    boost = shadow_boost + (peak_boost - shadow_boost) * L ** hi_gamma
    return rgb_lin * sdr_white * boost  # H×W×3, nits


# ── Gain map ────────────────────────────────────────────────────────────────

_OFF_SDR = 0.0  # match Google's libultrahdr reference output
_OFF_HDR = 0.0


def compute_gain_map(
    sdr_lin: np.ndarray,   # H×W×3 linear [0,1]
    hdr_nits: np.ndarray,  # H×W×3 nits (BT.2020)
    sdr_white: float,
) -> tuple[np.ndarray, float, float]:
    """Return (gain H×W float32 in log2, gain_min, gain_max).

    Convention: gain >= 0 (HDR can only boost, never darken) so GainMapMin=0.
    Chrome / libultrahdr assume non-negative gain; negative values disable HDR.
    """
    hdr_norm = hdr_nits / sdr_white
    w = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    # Small floor avoids log(0); offsets in XMP stay at 0 to match reference samples.
    L_sdr = np.clip((sdr_lin  * w).sum(-1), 1e-4, None)
    L_hdr = np.clip((hdr_norm * w).sum(-1), 1e-4, None)
    gain = np.log2(L_hdr) - np.log2(L_sdr)
    gain = np.maximum(gain, 0.0)  # clamp: no darkening
    return gain.astype(np.float32), 0.0, float(gain.max())


# ── JPEG / XMP / MPF helpers ────────────────────────────────────────────────

def _xmp_hdrgm(gain_min: float, gain_max: float, hdr_capacity: float) -> bytes:
    """Build an XMP APP1 segment with hdrgm:1.0 metadata.

    Matches the formatting of Google's libultrahdr reference output:
    each attribute on its own line, 6 decimal places, no Gamma attribute.
    """
    xml = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.5.0">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"\n'
        '   hdrgm:Version="1.0"\n'
        f'   hdrgm:GainMapMin="{gain_min:.6f}"\n'
        f'   hdrgm:GainMapMax="{gain_max:.6f}"\n'
        f'   hdrgm:HDRCapacityMin="0.000000"\n'
        f'   hdrgm:HDRCapacityMax="{hdr_capacity:.6f}"\n'
        '   hdrgm:OffsetHDR="0.000000"\n'
        '   hdrgm:OffsetSDR="0.000000"/>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )
    payload = b'http://ns.adobe.com/xap/1.0/\x00' + xml.encode('utf-8')
    return b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload


def _mpf_app2(primary_size: int, gainmap_size: int, gainmap_offset: int) -> bytes:
    """Build 90-byte MPF APP2 segment (CIPA DC-007, little-endian TIFF IFD)."""
    # Layout from start of APP2 data ("MPF\0"):
    #   [0..3]   "MPF\0"
    #   [4..11]  TIFF header (II, 0x002A, IFD-offset=8)
    #   [12..53] IFD: count(2) + 3 entries×12 + next_ifd(4)  = 42 bytes
    #   [54..69] MP Entry 1 (primary)
    #   [70..85] MP Entry 2 (gain map)
    # Total data = 86 bytes → APP2 segment = FF E2 + len(88) + data(86) = 90 bytes
    # TIFF offsets are from TIFF start (= data[4] = "MPF\0"[4]).
    # MP Entry value offset in IFD → TIFF offset = 54 - 4 = 50.

    TIFF_HDR = b'II' + struct.pack('<H', 42) + struct.pack('<I', 8)
    MP_ENTRIES_TIFF_OFF = 50

    # CIPA DC-007 tags: 0xB000 MPFVersion, 0xB001 NumberOfImages, 0xB002 MPEntry
    e_ver   = struct.pack('<HHI4s', 0xB000, 7, 4, b'0100')
    e_num   = struct.pack('<HHII',  0xB001, 4, 1, 2)
    e_entry = struct.pack('<HHII',  0xB002, 7, 32, MP_ENTRIES_TIFF_OFF)
    ifd = struct.pack('<H', 3) + e_ver + e_num + e_entry + struct.pack('<I', 0)

    # MP Attribute encoding (matches libultrahdr output on LE systems):
    #   primary  = 0x00030000  (bits encode "Baseline MP Primary Image" in CIPA layout)
    #   gain map = 0x00000000  (no flags; non-primary)
    mp1 = struct.pack('<IIIHH', 0x00030000, primary_size, 0,              0, 0)
    mp2 = struct.pack('<IIIHH', 0x00000000, gainmap_size, gainmap_offset, 0, 0)

    data = b'MPF\x00' + TIFF_HDR + ifd + mp1 + mp2
    assert len(data) == 86
    return b'\xff\xe2' + struct.pack('>H', 88) + data


# ── Ultra HDR assembly ──────────────────────────────────────────────────────

def build_ultra_hdr(
    sdr_uint8: np.ndarray,
    gain_log2: np.ndarray,
    gain_min: float,
    gain_max: float,
    peak_nits: float,
    sdr_white: float,
    base_quality: int = 92,
    gainmap_quality: int = 85,
) -> bytes:
    # Shared hdrgm XMP (identical in primary and gain map, matches real samples)
    hdr_capacity = math.log2(peak_nits / sdr_white)
    xmp_app1 = _xmp_hdrgm(gain_min, gain_max, hdr_capacity)

    # 1. Base SDR JPEG (plain, no extra markers)
    buf = io.BytesIO()
    Image.fromarray(sdr_uint8, 'RGB').save(buf, format='JPEG',
                                           quality=base_quality, subsampling=0)
    base_jpeg = buf.getvalue()

    # 2. Gain map JPEG (grayscale uint8) with FULL hdrgm XMP (not just Version)
    gain_range = gain_max - gain_min or 1.0
    gain_u8 = np.clip((gain_log2 - gain_min) / gain_range * 255 + 0.5, 0, 255).astype(np.uint8)
    gm_buf = io.BytesIO()
    Image.fromarray(gain_u8, 'L').save(gm_buf, format='JPEG', quality=gainmap_quality)
    gm_raw = gm_buf.getvalue()
    gainmap_jpeg = gm_raw[:2] + xmp_app1 + gm_raw[2:]  # inject same XMP after SOI

    # 4. Compute sizes and offsets, build MPF APP2
    # Final layout: SOI(2) + xmp_app1(A) + mpf_app2(90) + base_jpeg[2:](B-2) + gainmap_jpeg(G)
    A = len(xmp_app1)
    B = len(base_jpeg)
    G = len(gainmap_jpeg)
    primary_size = 2 + A + 90 + (B - 2)       # = A + B + 90
    # Per CIPA DC-007, MP Entry offset is measured from the *TIFF header* start
    # (i.e. from the byte right after "MPF\0"), NOT from "MPF\0" itself.
    # TIFF header starts at file offset  SOI(2) + xmp(A) + FFE2(2) + len(2) + MPF\0(4) = A + 10
    # Gain map SOI starts at file offset A + B + 90
    gainmap_offset = (A + B + 90) - (A + 10)      # = B + 80
    mpf_app2 = _mpf_app2(primary_size, G, gainmap_offset)

    # 5. Assemble
    return b'\xff\xd8' + xmp_app1 + mpf_app2 + base_jpeg[2:] + gainmap_jpeg


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description='SDR image → Ultra HDR JPEG')
    ap.add_argument('input', type=Path)
    ap.add_argument('--peak-nits',       type=float, default=1000.0)
    ap.add_argument('--sdr-white',       type=float, default=203.0)
    ap.add_argument('--quality',         type=int,   default=92)
    ap.add_argument('--gainmap-quality', type=int,   default=85)
    args = ap.parse_args()

    sdr_uint8 = np.array(Image.open(args.input).convert('RGB'), dtype=np.uint8)
    sdr_lin   = srgb_to_linear(sdr_uint8.astype(np.float32) / 255.0)

    hdr_nits   = inverse_tone_map(sdr_lin, args.sdr_white, args.peak_nits)
    hdr_bt2020 = np.clip(hdr_nits @ BT709_TO_BT2020.T, 0.0, args.peak_nits)

    gain_log2, gain_min, gain_max = compute_gain_map(sdr_lin, hdr_bt2020, args.sdr_white)

    ultra_hdr = build_ultra_hdr(
        sdr_uint8, gain_log2, gain_min, gain_max,
        args.peak_nits, args.sdr_white,
        args.quality, args.gainmap_quality,
    )

    out = args.input.with_name(args.input.stem + '_ultrahdr.jpg')
    out.write_bytes(ultra_hdr)
    print(f'wrote  {out}')
    print(f'size   {len(ultra_hdr) // 1024} KB')
    print(f'gain   min={gain_min:.3f}  max={gain_max:.3f}  (log2 stops)')
    print(f'boost  peak ~{2**gain_max * args.sdr_white:.0f} nits  ({2**gain_max:.1f}x SDR white)')


if __name__ == '__main__':
    main()
