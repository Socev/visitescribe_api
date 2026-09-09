"""FLAC container validation.

Two layers:

1. **Structural** (always available, no dependencies): the `fLaC` magic, a
   well-formed STREAMINFO block, a sane metadata block chain, and a valid
   frame sync code at the start of the audio data. Yields sample rate,
   channel count, bit depth and duration.
2. **Deep** (when libsndfile is present): the stream is actually decoded.
   When STREAMINFO carries a non-zero MD5 of the unencoded audio, the decoded
   PCM is hashed and compared, which catches silent corruption the GCM tag
   would not (because the ciphertext was authentic but the plaintext was
   encoded from damaged input).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

# Deep verification needs both libsndfile (via soundfile) and numpy; soundfile
# does not itself depend on numpy, so both are checked.
try:  # pragma: no cover - exercised by whichever branch the image provides
    import numpy as _numpy
    import soundfile as _soundfile
except Exception:  # noqa: BLE001
    _numpy = None
    _soundfile = None

FLAC_MAGIC = b"fLaC"

# A FLAC stream of digital silence compresses enormously: a few hundred KiB of
# ciphertext can declare hours of audio, and decoding it would allocate
# gigabytes. Both limits are checked from the header *before* any decode, and
# again while streaming, so a malicious or malfunctioning recorder cannot
# exhaust the pod's memory with one upload.
MAX_DECODED_BYTES = 64 * 1024 * 1024      # ~5.5 minutes of 48 kHz mono, int32
MAX_TOTAL_SAMPLES = 1 << 32
DECODE_BLOCK_FRAMES = 65536

BLOCK_STREAMINFO = 0
BLOCK_PADDING = 1
BLOCK_APPLICATION = 2
BLOCK_SEEKTABLE = 3
BLOCK_VORBIS_COMMENT = 4
BLOCK_CUESHEET = 5
BLOCK_PICTURE = 6
BLOCK_INVALID = 127


class FlacError(ValueError):
    """The bytes are not a usable FLAC stream."""


@dataclass
class FlacInfo:
    valid: bool = False
    deep_verified: bool = False
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    total_samples: int = 0
    duration_ms: int = 0
    min_block_size: int = 0
    max_block_size: int = 0
    md5: str = ""
    md5_verified: bool | None = None
    decoder: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "deep_verified": self.deep_verified,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
            "total_samples": self.total_samples,
            "duration_ms": self.duration_ms,
            "md5": self.md5,
            "md5_verified": self.md5_verified,
            "decoder": self.decoder,
            "warnings": self.warnings,
        }


def _read_streaminfo(block: bytes) -> dict[str, int | str]:
    if len(block) != 34:
        raise FlacError(f"STREAMINFO must be 34 bytes, got {len(block)}")
    bits = int.from_bytes(block[:18], "big")  # first 144 bits
    min_block = (bits >> 128) & 0xFFFF
    max_block = (bits >> 112) & 0xFFFF
    min_frame = (bits >> 88) & 0xFFFFFF
    max_frame = (bits >> 64) & 0xFFFFFF
    sample_rate = (bits >> 44) & 0xFFFFF
    channels = ((bits >> 41) & 0x7) + 1
    bits_per_sample = ((bits >> 36) & 0x1F) + 1
    total_samples = bits & 0xFFFFFFFFF
    md5 = block[18:34].hex()
    if sample_rate == 0:
        raise FlacError("STREAMINFO declares sample rate 0")
    if not 1 <= channels <= 8:
        raise FlacError(f"STREAMINFO declares {channels} channels")
    if not 4 <= bits_per_sample <= 32:
        raise FlacError(f"STREAMINFO declares {bits_per_sample} bits per sample")
    if min_block and max_block and min_block > max_block:
        raise FlacError("STREAMINFO block sizes are inconsistent")
    return {
        "min_block_size": min_block,
        "max_block_size": max_block,
        "min_frame_size": min_frame,
        "max_frame_size": max_frame,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits_per_sample,
        "total_samples": total_samples,
        "md5": md5,
    }


def parse_structure(data: bytes) -> FlacInfo:
    """Validate the container and return what STREAMINFO declares."""
    if len(data) < 42:
        raise FlacError("stream is too short to be FLAC")
    if data[:4] != FLAC_MAGIC:
        raise FlacError("missing fLaC magic")

    pos = 4
    info: dict[str, int | str] | None = None
    seen_last = False
    blocks = 0
    while pos + 4 <= len(data):
        header = data[pos]
        is_last = bool(header & 0x80)
        block_type = header & 0x7F
        length = int.from_bytes(data[pos + 1 : pos + 4], "big")
        pos += 4
        if block_type == BLOCK_INVALID:
            raise FlacError("metadata block type 127 is invalid")
        if pos + length > len(data):
            raise FlacError("metadata block runs past end of stream")
        if blocks == 0 and block_type != BLOCK_STREAMINFO:
            raise FlacError("first metadata block must be STREAMINFO")
        if block_type == BLOCK_STREAMINFO:
            if info is not None:
                raise FlacError("more than one STREAMINFO block")
            info = _read_streaminfo(data[pos : pos + length])
        pos += length
        blocks += 1
        if is_last:
            seen_last = True
            break
        if blocks > 128:
            raise FlacError("unreasonable number of metadata blocks")

    if info is None:
        raise FlacError("no STREAMINFO block")
    if not seen_last:
        raise FlacError("metadata block chain has no last-block marker")
    if pos + 2 > len(data):
        raise FlacError("no audio frames after metadata")

    # Frame sync code: 14 bits of 1s then 0 then the blocking strategy bit.
    if (int.from_bytes(data[pos : pos + 2], "big") & 0xFFFE) != 0xFFF8:
        raise FlacError("first audio frame has no valid sync code")

    sample_rate = int(info["sample_rate"])
    total = int(info["total_samples"])
    if total > MAX_TOTAL_SAMPLES:
        raise FlacError(
            f"STREAMINFO declares {total} samples, above the {MAX_TOTAL_SAMPLES} limit"
        )
    result = FlacInfo(
        valid=True,
        sample_rate=sample_rate,
        channels=int(info["channels"]),
        bits_per_sample=int(info["bits_per_sample"]),
        total_samples=total,
        duration_ms=int(round(total * 1000 / sample_rate)) if total else 0,
        min_block_size=int(info["min_block_size"]),
        max_block_size=int(info["max_block_size"]),
        md5=str(info["md5"]),
        decoder="structural",
    )
    if total == 0:
        result.warnings.append("STREAMINFO declares 0 total samples (streamed encode)")
    return result


def _pcm_bytes(samples, bits_per_sample: int) -> bytes | None:
    """The unencoded audio bytes as FLAC defines them, for 8/16/24-bit."""
    np = _numpy
    if np is None:
        return None
    if bits_per_sample == 16:
        return samples.astype("<i2").tobytes()
    if bits_per_sample == 8:
        return (samples.astype("<i2") + 128).astype("u1").tobytes()
    if bits_per_sample == 24:
        as32 = samples.astype("<i4").reshape(-1)
        return np.frombuffer(as32.tobytes(), dtype="u1").reshape(-1, 4)[:, :3].tobytes()
    return None


def deep_verify(data: bytes, info: FlacInfo,
                max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Fully decode the stream in bounded blocks. Never raises for a missing
    decoder, always raises for a stream that would decode too large."""
    if _soundfile is None or _numpy is None:
        info.warnings.append(
            "libsndfile or numpy unavailable; structural validation only"
        )
        return info

    bytes_per_frame = max(info.channels, 1) * 4  # soundfile hands back int32
    if info.total_samples:
        declared = info.total_samples * bytes_per_frame
        if declared > max_decoded_bytes:
            raise FlacError(
                f"stream declares {declared} decoded bytes, above the "
                f"{max_decoded_bytes} limit"
            )

    digest = hashlib.md5()  # noqa: S324 - format-mandated, not security
    md5_usable = bool(info.md5) and info.md5 != "0" * 32
    frames = 0
    try:
        with _soundfile.SoundFile(io.BytesIO(data)) as handle:
            if handle.format != "FLAC":
                raise FlacError(f"libsndfile reports format {handle.format}, not FLAC")
            if handle.samplerate != info.sample_rate:
                raise FlacError("decoded sample rate disagrees with STREAMINFO")
            if handle.channels != info.channels:
                raise FlacError("decoded channel count disagrees with STREAMINFO")
            if handle.frames and handle.frames * bytes_per_frame > max_decoded_bytes:
                raise FlacError(
                    f"stream would decode to {handle.frames * bytes_per_frame} "
                    f"bytes, above the {max_decoded_bytes} limit"
                )
            shift = 32 - info.bits_per_sample
            for block in handle.blocks(blocksize=DECODE_BLOCK_FRAMES, dtype="int32",
                                       always_2d=True):
                frames += int(block.shape[0])
                if frames * bytes_per_frame > max_decoded_bytes:
                    raise FlacError(
                        f"stream decoded past the {max_decoded_bytes} byte limit"
                    )
                if md5_usable:
                    raw = _pcm_bytes(block >> shift, info.bits_per_sample)
                    if raw is None:
                        md5_usable = False
                    else:
                        digest.update(raw)
    except FlacError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlacError(f"stream is not decodable: {exc}") from exc

    if info.total_samples and frames != info.total_samples:
        raise FlacError(
            f"decoded {frames} frames but STREAMINFO declares {info.total_samples}"
        )
    info.deep_verified = True
    info.decoder = "libsndfile"
    if not info.total_samples:
        info.total_samples = frames
        info.duration_ms = int(round(frames * 1000 / info.sample_rate))

    if md5_usable:
        info.md5_verified = digest.hexdigest() == info.md5
        if not info.md5_verified:
            raise FlacError("decoded audio does not match the STREAMINFO MD5")
    return info


def validate(data: bytes, deep: bool = True,
             max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    info = parse_structure(data)
    if deep:
        info = deep_verify(data, info, max_decoded_bytes=max_decoded_bytes)
    return info


def check_against_manifest(info: FlacInfo, audio: dict[str, Any]) -> list[str]:
    """Return human-readable conflicts between the manifest and the audio."""
    problems: list[str] = []
    declared_rate = audio.get("sample_rate")
    if isinstance(declared_rate, int) and declared_rate and declared_rate != info.sample_rate:
        problems.append(
            f"manifest declares sample_rate {declared_rate} but the FLAC is {info.sample_rate}"
        )
    declared_channels = audio.get("channels")
    if isinstance(declared_channels, int) and declared_channels and declared_channels != info.channels:
        problems.append(
            f"manifest declares {declared_channels} channels but the FLAC has {info.channels}"
        )
    fmt = audio.get("sample_format")
    if isinstance(fmt, str) and fmt:
        expected = {"S16_LE": 16, "S24_LE": 24, "S32_LE": 32, "S8": 8, "U8": 8}.get(fmt.upper())
        if expected and expected != info.bits_per_sample:
            problems.append(
                f"manifest declares {fmt} but the FLAC is {info.bits_per_sample}-bit"
            )
    return problems


def decoder_available() -> bool:
    return _soundfile is not None and _numpy is not None
