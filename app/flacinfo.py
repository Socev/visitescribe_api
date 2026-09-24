"""Recorder-audio container validation.

The original recorder uploads FLAC chunks; the CoreS3-Lite records canonical
PCM WAV locally and uploads independently valid WAV chunks.  Both are lossless.
Since 1.6.0 a session may instead be declared ``codec: "opus"`` and carry
self-contained Ogg/Opus chunks (the PC sync app, and later the recorder
itself): the lossless 48 kHz master stays on the recorder's card, and the
upload copy only has to be good speech.  Lossy audio is accepted *only* in a
session whose manifest says so -- see ``check_against_manifest``.  Every chunk
is fully authenticated by AES-GCM before this module sees it.

Validation has two layers:

1. **Structural** (always available): FLAC STREAMINFO/frame checks or RIFF/WAVE
   PCM header/chunk checks.  This yields sample rate, channel count, bit depth
   and duration without trusting large decoder allocations.
2. **Deep** (when libsndfile is present): the stream is actually decoded in
   bounded blocks.  FLAC additionally verifies STREAMINFO MD5 when present.

``FlacInfo`` and ``FlacError`` retain their historical names because they are
part of the server's internal/public diagnostic shape.  The fields are generic
for either accepted lossless container.
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
WAV_RIFF = b"RIFF"
WAV_WAVE = b"WAVE"
OGG_MAGIC = b"OggS"

# Opus always counts granule positions at 48 kHz, whatever the encoder was fed.
OPUS_GRANULE_RATE = 48000
# The rates an Opus decoder can produce natively.  OpusHead's input_sample_rate
# must be one of them: libsndfile then decodes at exactly that rate, so the
# sample rate the manifest declares is the rate the pipeline sees downstream.
OPUS_DECODE_RATES = (8000, 12000, 16000, 24000, 48000)

# A FLAC stream of digital silence compresses enormously: a few hundred KiB of
# ciphertext can declare hours of audio, and decoding it would allocate
# gigabytes. Both limits are checked from the header *before* any decode, and
# again while streaming, so a malicious or malfunctioning recorder cannot
# exhaust the pod's memory with one upload. WAV is bounded by the same decoded
# limit even though its stored PCM is already uncompressed.
MAX_DECODED_BYTES = 64 * 1024 * 1024
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
    """The bytes are not a usable supported lossless audio stream."""


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
    codec: str = ""
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
            "codec": self.codec,
            "warnings": self.warnings,
        }


def _read_streaminfo(block: bytes) -> dict[str, int | str]:
    if len(block) != 34:
        raise FlacError(f"STREAMINFO must be 34 bytes, got {len(block)}")
    bits = int.from_bytes(block[:18], "big")
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
    """Validate a FLAC container and return what STREAMINFO declares."""
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
        decoder="structural-flac",
        codec="flac",
    )
    if total == 0:
        result.warnings.append("STREAMINFO declares 0 total samples (streamed encode)")
    return result


def parse_wav_structure(data: bytes,
                        max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Validate RIFF/WAVE PCM without relying on a decoder.

    The CoreS3 currently emits PCM16 canonical WAV chunks, but the parser walks
    RIFF chunks instead of assuming a fixed 44-byte header so harmless metadata
    chunks remain forward compatible.
    """
    if len(data) < 44 or data[:4] != WAV_RIFF or data[8:12] != WAV_WAVE:
        raise FlacError("missing RIFF/WAVE header")
    declared_riff = int.from_bytes(data[4:8], "little") + 8
    if declared_riff > len(data):
        raise FlacError("RIFF length runs past end of stream")

    pos = 12
    fmt: tuple[int, int, int, int, int] | None = None
    data_size: int | None = None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        end = body + size
        if end > len(data):
            raise FlacError("WAV chunk runs past end of stream")
        if chunk_id == b"fmt ":
            if size < 16:
                raise FlacError("WAV fmt chunk is too short")
            audio_format = int.from_bytes(data[body:body + 2], "little")
            channels = int.from_bytes(data[body + 2:body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4:body + 8], "little")
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
            block_align = int.from_bytes(data[body + 12:body + 14], "little")
            bits = int.from_bytes(data[body + 14:body + 16], "little")
            # 1 = integer PCM.  The recorder deliberately uses this simplest,
            # most interoperable form rather than WAVE_FORMAT_EXTENSIBLE.
            if audio_format != 1:
                raise FlacError(f"WAV format {audio_format} is not PCM")
            if not 1 <= channels <= 8:
                raise FlacError(f"WAV declares {channels} channels")
            if sample_rate <= 0:
                raise FlacError("WAV sample rate is invalid")
            if bits not in (8, 16, 24, 32):
                raise FlacError(f"WAV declares unsupported {bits}-bit samples")
            expected_align = channels * ((bits + 7) // 8)
            if block_align != expected_align:
                raise FlacError("WAV block_align is inconsistent")
            if byte_rate != sample_rate * block_align:
                raise FlacError("WAV byte_rate is inconsistent")
            fmt = (channels, sample_rate, bits, block_align, byte_rate)
        elif chunk_id == b"data":
            data_size = size
            break
        pos = end + (size & 1)

    if fmt is None:
        raise FlacError("WAV has no fmt chunk")
    if data_size is None:
        raise FlacError("WAV has no data chunk")
    channels, sample_rate, bits, block_align, _ = fmt
    if data_size % block_align:
        raise FlacError("WAV data size is not frame aligned")
    total = data_size // block_align
    if total > MAX_TOTAL_SAMPLES:
        raise FlacError(
            f"WAV declares {total} samples, above the {MAX_TOTAL_SAMPLES} limit"
        )
    # The limit counts what the audio becomes in memory, not what it weighs on
    # the wire: libsndfile hands every container back as int32 frames. Counting
    # WAV's stored bytes instead would let a stream through this door at up to
    # twice the size the decoder will refuse -- and with deep verification off
    # it would be accepted here and only fail later, during reassembly.
    declared = total * channels * 4
    if declared > max_decoded_bytes:
        raise FlacError(
            f"stream declares {declared} decoded bytes, above the "
            f"{max_decoded_bytes} limit"
        )
    result = FlacInfo(
        valid=True,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        total_samples=total,
        duration_ms=int(round(total * 1000 / sample_rate)),
        decoder="structural-wav",
        codec="pcm",
    )
    if total == 0:
        result.warnings.append("WAV data chunk is empty")
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
    """Fully decode FLAC in bounded blocks."""
    if _soundfile is None or _numpy is None:
        info.warnings.append("libsndfile or numpy unavailable; structural validation only")
        return info

    bytes_per_frame = max(info.channels, 1) * 4
    if info.total_samples:
        declared = info.total_samples * bytes_per_frame
        if declared > max_decoded_bytes:
            raise FlacError(
                f"stream declares {declared} decoded bytes, above the {max_decoded_bytes} limit"
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
                    f"stream would decode to {handle.frames * bytes_per_frame} bytes, "
                    f"above the {max_decoded_bytes} limit"
                )
            shift = 32 - info.bits_per_sample
            for block in handle.blocks(blocksize=DECODE_BLOCK_FRAMES, dtype="int32",
                                       always_2d=True):
                frames += int(block.shape[0])
                if frames * bytes_per_frame > max_decoded_bytes:
                    raise FlacError(f"stream decoded past the {max_decoded_bytes} byte limit")
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
        raise FlacError(f"decoded {frames} frames but STREAMINFO declares {info.total_samples}")
    info.deep_verified = True
    info.decoder = "libsndfile-flac"
    if not info.total_samples:
        info.total_samples = frames
        info.duration_ms = int(round(frames * 1000 / info.sample_rate))

    if md5_usable:
        info.md5_verified = digest.hexdigest() == info.md5
        if not info.md5_verified:
            raise FlacError("decoded audio does not match the STREAMINFO MD5")
    return info


def deep_verify_wav(data: bytes, info: FlacInfo,
                    max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Decode WAV in bounded blocks and verify the structural metadata."""
    if _soundfile is None or _numpy is None:
        info.warnings.append("libsndfile or numpy unavailable; structural validation only")
        return info
    frames = 0
    bytes_per_frame = max(info.channels, 1) * 4
    try:
        with _soundfile.SoundFile(io.BytesIO(data)) as handle:
            if handle.format not in ("WAV", "WAVEX"):
                raise FlacError(f"libsndfile reports format {handle.format}, not WAV")
            if handle.samplerate != info.sample_rate:
                raise FlacError("decoded sample rate disagrees with WAV header")
            if handle.channels != info.channels:
                raise FlacError("decoded channel count disagrees with WAV header")
            if handle.frames and handle.frames * bytes_per_frame > max_decoded_bytes:
                raise FlacError(
                    f"stream would decode to {handle.frames * bytes_per_frame} bytes, "
                    f"above the {max_decoded_bytes} limit"
                )
            for block in handle.blocks(blocksize=DECODE_BLOCK_FRAMES, dtype="int32",
                                       always_2d=True):
                frames += int(block.shape[0])
                if frames * bytes_per_frame > max_decoded_bytes:
                    raise FlacError(f"stream decoded past the {max_decoded_bytes} byte limit")
    except FlacError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlacError(f"stream is not decodable: {exc}") from exc
    if frames != info.total_samples:
        raise FlacError(
            f"decoded {frames} frames but WAV header declares {info.total_samples}"
        )
    info.deep_verified = True
    info.decoder = "libsndfile-wav"
    return info


# ---------------------------------------------------------------------------
# Ogg/Opus (RFC 7845)
# ---------------------------------------------------------------------------

def _make_ogg_crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_OGG_CRC_TABLE = _make_ogg_crc_table()


def _ogg_crc(page: bytes) -> int:
    """Ogg's CRC-32: polynomial 0x04C11DB7, zero init, no reflection."""
    crc = 0
    table = _OGG_CRC_TABLE
    for byte in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _opus_packet_samples(packet: bytes) -> int:
    """Samples (at 48 kHz) in one Opus packet, from its TOC byte (RFC 6716 3.1)."""
    if not packet:
        raise FlacError("empty Opus packet in the audio stream")
    toc = packet[0]
    config = toc >> 3
    if config < 12:          # SILK-only: 10, 20, 40, 60 ms
        frame = (480, 960, 1920, 2880)[config & 3]
    elif config < 16:        # hybrid: 10, 20 ms
        frame = (480, 960)[config & 1]
    else:                    # CELT-only: 2.5, 5, 10, 20 ms
        frame = (120, 240, 480, 960)[config & 3]
    code = toc & 3
    if code == 0:
        count = 1
    elif code in (1, 2):
        count = 2
    else:
        if len(packet) < 2:
            raise FlacError("Opus code-3 packet has no frame count byte")
        count = packet[1] & 0x3F
        if count == 0:
            raise FlacError("Opus code-3 packet declares zero frames")
    samples = frame * count
    if samples > 5760:  # 120 ms is the most one packet may hold
        raise FlacError("Opus packet holds more than 120 ms of audio")
    return samples


def parse_ogg_opus_structure(data: bytes,
                             max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Validate one self-contained Ogg/Opus chunk without a decoder.

    The contract (docs/RECORDER.md) is a complete, single-stream Ogg/Opus file
    per chunk: BOS page with OpusHead alone, OpusTags, audio, EOS on the last
    page.  Every page CRC is checked, page sequence numbers must be contiguous,
    and granule positions must count exactly the samples the packets carry --
    the last page may trim the tail (RFC 7845 end trimming) and nothing else.
    That is what makes each chunk's duration exact, and with it every offset
    the patient segmentation computes.
    """
    if len(data) < 27 or data[:4] != OGG_MAGIC:
        raise FlacError("missing OggS capture pattern")

    pos = 0
    page_index = 0
    serial: int | None = None
    pending = b""                 # packet bytes carried over into the next page
    packets: list[tuple[bytes, int]] = []   # (packet, index of page it ends on)
    pages: list[dict[str, int]] = []
    while pos < len(data):
        if len(data) - pos < 27:
            raise FlacError("trailing bytes after the last Ogg page")
        if data[pos:pos + 4] != OGG_MAGIC:
            raise FlacError(f"Ogg page {page_index} has no capture pattern")
        if data[pos + 4] != 0:
            raise FlacError(f"Ogg page {page_index} has stream structure version "
                            f"{data[pos + 4]}")
        flags = data[pos + 5]
        granule = int.from_bytes(data[pos + 6:pos + 14], "little", signed=True)
        page_serial = int.from_bytes(data[pos + 14:pos + 18], "little")
        sequence = int.from_bytes(data[pos + 18:pos + 22], "little")
        crc = int.from_bytes(data[pos + 22:pos + 26], "little")
        segments = data[pos + 26]
        lacing_end = pos + 27 + segments
        if lacing_end > len(data):
            raise FlacError(f"Ogg page {page_index} header runs past end of stream")
        lacing = data[pos + 27:lacing_end]
        end = lacing_end + sum(lacing)
        if end > len(data):
            raise FlacError(f"Ogg page {page_index} runs past end of stream")
        page = data[pos:end]
        if _ogg_crc(page[:22] + b"\x00\x00\x00\x00" + page[26:]) != crc:
            raise FlacError(f"Ogg page {page_index} fails its CRC")

        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise FlacError("chunk contains more than one logical Ogg stream")
        if sequence != page_index:
            raise FlacError(f"Ogg page sequence jumps to {sequence} at page {page_index}")
        if bool(flags & 0x02) != (page_index == 0):
            raise FlacError("only the first Ogg page may, and must, carry BOS")
        if bool(flags & 0x01) != bool(pending):
            raise FlacError(f"Ogg page {page_index} continuation flag is inconsistent")

        body = lacing_end
        completed = 0
        for value in lacing:
            pending += data[body:body + value]
            body += value
            if value < 255:
                packets.append((pending, page_index))
                pending = b""
                completed += 1
        pages.append({"granule": granule, "completed": completed,
                      "eos": int(bool(flags & 0x04)), "packets_end": len(packets),
                      "open": int(bool(pending))})
        pos = end
        page_index += 1

    if pending:
        raise FlacError("the last Ogg page ends inside a packet")
    if not pages[-1]["eos"] or any(p["eos"] for p in pages[:-1]):
        raise FlacError("only the last Ogg page may, and must, carry EOS")

    # --- OpusHead: alone on page 0 ------------------------------------------
    if pages[0]["completed"] != 1 or pages[0]["open"] or packets[0][1] != 0:
        raise FlacError("the first Ogg page must hold exactly the OpusHead packet")
    head = packets[0][0]
    if len(head) < 19 or head[:8] != b"OpusHead":
        raise FlacError("first packet is not OpusHead")
    if head[8] >> 4:
        raise FlacError(f"unsupported OpusHead version {head[8]}")
    channels = head[9]
    pre_skip = int.from_bytes(head[10:12], "little")
    input_rate = int.from_bytes(head[12:16], "little")
    gain = int.from_bytes(head[16:18], "little", signed=True)
    family = head[18]
    if family != 0:
        raise FlacError(f"channel mapping family {family} is not supported; use 0")
    if channels not in (1, 2):
        raise FlacError(f"OpusHead declares {channels} channels")
    if input_rate not in OPUS_DECODE_RATES:
        raise FlacError(
            f"OpusHead input_sample_rate {input_rate} is not one of "
            f"{', '.join(map(str, OPUS_DECODE_RATES))}"
        )
    if pages[0]["granule"] != 0:
        raise FlacError("the OpusHead page must have granule position 0")

    # --- OpusTags: starts on page 1, and its last page holds nothing else ----
    if len(packets) < 2 or packets[1][0][:8] != b"OpusTags":
        raise FlacError("second packet is not OpusTags")
    tags_page = packets[1][1]
    if pages[tags_page]["packets_end"] != 2 or pages[tags_page]["open"]:
        raise FlacError("audio shares a page with OpusTags")
    for index in range(1, tags_page + 1):
        if pages[index]["granule"] not in (0, -1) or (
            pages[index]["completed"] and pages[index]["granule"] != 0
        ):
            raise FlacError("the OpusTags pages must have granule position 0")

    # --- audio: granule positions count exactly what the packets carry -------
    audio = packets[2:]
    if not audio:
        raise FlacError("Ogg/Opus stream has no audio packets")
    counted = 0
    audio_index = 0
    last_page = len(pages) - 1
    for index in range(tags_page + 1, len(pages)):
        page = pages[index]
        before = counted
        while audio_index < len(audio) and audio[audio_index][1] == index:
            counted += _opus_packet_samples(audio[audio_index][0])
            audio_index += 1
        if not page["completed"]:
            if page["granule"] != -1:
                raise FlacError(f"Ogg page {index} completes no packet but has a granule")
            continue
        if index == last_page:
            # End trimming may only drop samples of the final page.
            if not before <= page["granule"] <= counted:
                raise FlacError(
                    f"final granule position {page['granule']} is outside the "
                    f"last page's audio ({before}..{counted})"
                )
        elif page["granule"] != counted:
            raise FlacError(
                f"Ogg page {index} granule position {page['granule']} does not "
                f"match the {counted} samples its packets carry"
            )

    final = pages[-1]["granule"]
    if final < pre_skip:
        raise FlacError("final granule position is smaller than the pre-skip")
    at_48k = final - pre_skip
    if (at_48k * input_rate) % OPUS_GRANULE_RATE:
        raise FlacError(
            f"stream length {at_48k} (48 kHz) is not a whole number of "
            f"{input_rate} Hz samples"
        )
    total = at_48k * input_rate // OPUS_GRANULE_RATE
    declared = total * channels * 4
    if declared > max_decoded_bytes:
        raise FlacError(
            f"stream declares {declared} decoded bytes, above the "
            f"{max_decoded_bytes} limit"
        )
    result = FlacInfo(
        valid=True,
        sample_rate=input_rate,
        channels=channels,
        bits_per_sample=0,     # Opus has no stored sample width
        total_samples=total,
        duration_ms=int(round(total * 1000 / input_rate)),
        decoder="structural-ogg-opus",
        codec="opus",
    )
    if gain:
        result.warnings.append(f"OpusHead output gain is {gain} (Q7.8 dB), not 0")
    if total == 0:
        result.warnings.append("Ogg/Opus stream contains no audio after pre-skip")
    return result


def deep_verify_ogg_opus(data: bytes, info: FlacInfo,
                         max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Decode Ogg/Opus in bounded blocks; the sample count must match exactly."""
    if _soundfile is None or _numpy is None:
        info.warnings.append("libsndfile or numpy unavailable; structural validation only")
        return info
    frames = 0
    bytes_per_frame = max(info.channels, 1) * 4
    try:
        with _soundfile.SoundFile(io.BytesIO(data)) as handle:
            if handle.format != "OGG" or handle.subtype != "OPUS":
                raise FlacError(
                    f"libsndfile reports {handle.format}/{handle.subtype}, not OGG/OPUS"
                )
            if handle.samplerate != info.sample_rate:
                raise FlacError(
                    f"libsndfile decodes at {handle.samplerate} Hz, but OpusHead "
                    f"declares {info.sample_rate}"
                )
            if handle.channels != info.channels:
                raise FlacError("decoded channel count disagrees with OpusHead")
            for block in handle.blocks(blocksize=DECODE_BLOCK_FRAMES, dtype="int32",
                                       always_2d=True):
                frames += int(block.shape[0])
                if frames * bytes_per_frame > max_decoded_bytes:
                    raise FlacError(f"stream decoded past the {max_decoded_bytes} byte limit")
    except FlacError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlacError(f"stream is not decodable: {exc}") from exc
    if frames != info.total_samples:
        raise FlacError(
            f"decoded {frames} frames but the granule positions declare "
            f"{info.total_samples}"
        )
    info.deep_verified = True
    info.decoder = "libsndfile-ogg-opus"
    return info


def container(data: bytes) -> str | None:
    """Which accepted container these bytes are, from the magic alone.

    Chunks are stored under a .flac blob name whatever the recorder sent, so
    anything that hands a chunk back -- an admin download, a debug export --
    has to look at the bytes rather than the file name to label it correctly.
    """
    if data[:4] == FLAC_MAGIC:
        return "flac"
    if data[:4] == WAV_RIFF and len(data) >= 12 and data[8:12] == WAV_WAVE:
        return "wav"
    if data[:4] == OGG_MAGIC:
        return "ogg"
    return None


def validate(data: bytes, deep: bool = True,
             max_decoded_bytes: int = MAX_DECODED_BYTES) -> FlacInfo:
    """Validate an authenticated FLAC, PCM WAV or Ogg/Opus recorder chunk.

    Which of these a *session* may carry is decided afterwards, against its
    manifest, by ``check_against_manifest``.
    """
    if data[:4] == FLAC_MAGIC:
        info = parse_structure(data)
        if deep:
            info = deep_verify(data, info, max_decoded_bytes=max_decoded_bytes)
        return info
    if data[:4] == WAV_RIFF and len(data) >= 12 and data[8:12] == WAV_WAVE:
        info = parse_wav_structure(data, max_decoded_bytes=max_decoded_bytes)
        if deep:
            info = deep_verify_wav(data, info, max_decoded_bytes=max_decoded_bytes)
        return info
    if data[:4] == OGG_MAGIC:
        info = parse_ogg_opus_structure(data, max_decoded_bytes=max_decoded_bytes)
        if deep:
            info = deep_verify_ogg_opus(data, info, max_decoded_bytes=max_decoded_bytes)
        return info
    raise FlacError("payload is neither FLAC, PCM WAV nor Ogg/Opus")


def check_against_manifest(info: FlacInfo, audio: dict[str, Any]) -> list[str]:
    """Return human-readable conflicts between the manifest and the audio.

    The codec is bound to the session: a session declared ``codec: "opus"``
    takes only Ogg/Opus chunks, and every other session -- every recorder that
    existed before 1.6.0 -- takes only lossless FLAC/PCM WAV, exactly as it
    always did.  A lossy chunk can therefore never slip into a session whose
    manifest promised lossless audio.
    """
    problems: list[str] = []
    declared_codec = str(audio.get("codec") or "").strip().lower()
    actual_codec = info.codec or "flac"
    if declared_codec == "opus":
        if actual_codec != "opus":
            problems.append(
                f"manifest declares codec opus but the chunk is {actual_codec}"
            )
    elif actual_codec == "opus":
        problems.append(
            "the chunk is Ogg/Opus but the manifest declares codec "
            f"{declared_codec or '(none)'}; lossy audio is only accepted in a "
            'session declared as codec "opus"'
        )
    declared_rate = audio.get("sample_rate")
    if isinstance(declared_rate, int) and declared_rate and declared_rate != info.sample_rate:
        problems.append(
            f"manifest declares sample_rate {declared_rate} but the audio is {info.sample_rate}"
        )
    declared_channels = audio.get("channels")
    if isinstance(declared_channels, int) and declared_channels and declared_channels != info.channels:
        problems.append(
            f"manifest declares {declared_channels} channels but the audio has {info.channels}"
        )
    fmt = audio.get("sample_format")
    if actual_codec == "opus":
        if isinstance(fmt, str) and fmt and fmt.strip().lower() != "opus":
            problems.append(f'manifest declares sample_format {fmt}; an Opus '
                            'session must declare "opus"')
    elif isinstance(fmt, str) and fmt:
        expected = {"S16_LE": 16, "S24_LE": 24, "S32_LE": 32, "S8": 8, "U8": 8}.get(fmt.upper())
        if expected and expected != info.bits_per_sample:
            problems.append(
                f"manifest declares {fmt} but the audio is {info.bits_per_sample}-bit"
            )
    return problems


def decoder_available() -> bool:
    return _soundfile is not None and _numpy is not None


def manifest_audio_problems(audio: dict[str, Any]) -> list[str]:
    """Problems with an Opus session's ``audio`` block, caught at POST /sessions.

    Refusing a bad manifest up front beats accepting the session and refusing
    every chunk of it later.  Sessions with any other codec are not touched:
    their manifests are exactly as permissive as they were before 1.6.0.
    """
    if str(audio.get("codec") or "").strip().lower() != "opus":
        return []
    problems: list[str] = []
    container_name = audio.get("container")
    if container_name is not None and str(container_name).strip().lower() != "ogg":
        problems.append('audio.container must be "ogg" for codec opus')
    rate = audio.get("sample_rate")
    if rate not in OPUS_DECODE_RATES:
        problems.append(
            "audio.sample_rate must be the encoder input rate, one of "
            f"{', '.join(map(str, OPUS_DECODE_RATES))} (16000 for VisiteScribe)"
        )
    if audio.get("channels") not in (1, 2):
        problems.append("audio.channels must be 1 or 2 for codec opus")
    fmt = audio.get("sample_format")
    if fmt is not None and str(fmt).strip().lower() != "opus":
        problems.append('audio.sample_format must be "opus" for codec opus')
    return problems
