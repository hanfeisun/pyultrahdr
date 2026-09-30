"""Public API for the SDR to Ultra HDR JPEG converter."""

from sdr_to_hdr import (
    BT709_TO_BT2020,
    build_ultra_hdr,
    compute_gain_map,
    encode_gain_map,
    inverse_tone_map,
    main,
    srgb_to_linear,
)

__all__ = [
    "BT709_TO_BT2020",
    "build_ultra_hdr",
    "compute_gain_map",
    "encode_gain_map",
    "inverse_tone_map",
    "srgb_to_linear",
]

if __name__ == "__main__":
    main()
