"""Reassembling a session's chunks into the audio a provider can accept.

The recorder stores audio as many small encrypted FLAC chunks. A provider wants
one file per logical recording — and for a multi-patient session, one file per
patient segment. Chunks are decrypted and decoded one at a time and streamed
straight into the output file, so a 45-minute consultation never sits in memory
whole.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import soundfile as sf

from . import crypto, db, sessions, storage
from .config import settings
from .errors import ApiError
from .util import b64decode_strict


# What we can hand a provider, and how to write it. FLAC is the native form
# -- it is what the recorder produced and it is lossless -- but not every
# provider accepts it: OurMind refuses it with "invalid-format", and their
# documentation says nothing beyond `audio/*`.
FORMATS: dict[str, tuple[str, str, str, str]] = {
    # name: (libsndfile format, subtype, file extension, content type)
    "flac": ("FLAC", "PCM_16", ".flac", "audio/flac"),
    "wav": ("WAV", "PCM_16", ".wav", "audio/wav"),
    "mp3": ("MP3", "MPEG_LAYER_III", ".mp3", "audio/mpeg"),
    "ogg": ("OGG", "OPUS", ".ogg", "audio/ogg"),
}


def content_type(fmt: str) -> str:
    return FORMATS.get(fmt, FORMATS["flac"])[3]


@dataclass
class AudioSlice:
    path: Path
    seconds: float
    sample_rate: int
    channels: int
    segment_index: int | None
    fmt: str = "flac"


def _session_key(session: dict) -> bytes:
    cached = crypto.cached_session_key(session["session_id"])
    if cached is not None:
        return cached
    wrapped = session.get("wrap_ciphertext_b64")
    if not wrapped:
        raise ApiError("INVALID_KEY_WRAP", "Session has no server key wrap")
    try:
        key, _ = crypto.unwrap_session_key(
            b64decode_strict(wrapped), session.get("wrap_key_id")
        )
    except (ValueError, crypto.KeyWrapError) as exc:
        raise ApiError("INVALID_KEY_WRAP", str(exc)) from exc
    crypto.cache_session_key(session["session_id"], key,
                             settings.session_key_cache_seconds)
    return key


def _decoded_chunks(session: dict) -> Iterator[bytes]:
    """Plaintext FLAC for each chunk, in sequence order."""
    key = _session_key(session)
    rows = db.query(
        "SELECT sequence, nonce_b64, aad, blob_path FROM chunks "
        "WHERE session_id = ? ORDER BY sequence",
        (session["session_id"],),
    )
    if not rows:
        raise ApiError("UNKNOWN_SESSION", "Session has no stored audio")
    for row in rows:
        data = storage.read_blob(row["blob_path"])
        yield crypto.decrypt_chunk(
            key, b64decode_strict(row["nonce_b64"]),
            (row["aad"] or "").encode("utf-8"), data,
        )


def build_slice(session_id: str, out_dir: Path, segment_index: int | None = None,
                start_ms: int = 0, end_ms: int | None = None,
                fmt: str = "flac") -> AudioSlice:
    """Write one audio file covering [start_ms, end_ms) of the session.

    FLAC by default: it is what the recorder produced, it is lossless, and it
    is on Mistral's documented list, so nothing is transcoded on the way out.
    A provider that will not take it (OurMind answers "invalid-format") gets
    another container, decoded once and re-encoded here rather than stored
    twice.
    """
    if fmt not in FORMATS:
        raise ApiError("INVALID_REQUEST", f"Onbekend audioformaat {fmt!r}")
    sf_format, sf_subtype, extension, _ = FORMATS[fmt]
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "full" if segment_index is None else f"seg{segment_index:02d}"
    # The extension matters: a provider that sniffs the file name rather than
    # the bytes would otherwise be told the wrong thing.
    out_path = out_dir / f"{session_id}-{suffix}{extension}"

    writer: sf.SoundFile | None = None
    rate = channels = 0
    cursor_ms = 0.0
    written_frames = 0
    try:
        for plaintext in _decoded_chunks(session):
            with sf.SoundFile(io.BytesIO(plaintext)) as handle:
                if writer is None:
                    rate, channels = handle.samplerate, handle.channels
                    writer = sf.SoundFile(
                        out_path, mode="w", samplerate=rate, channels=channels,
                        format=sf_format, subtype=sf_subtype,
                    )
                elif handle.samplerate != rate or handle.channels != channels:
                    raise ApiError(
                        "INVALID_REQUEST",
                        "Chunks in this session have inconsistent audio parameters",
                    )
                chunk_ms = handle.frames * 1000.0 / rate
                chunk_start, chunk_end = cursor_ms, cursor_ms + chunk_ms
                cursor_ms = chunk_end

                if end_ms is not None and chunk_start >= end_ms:
                    continue
                if chunk_end <= start_ms:
                    continue

                # Trim only the chunks that straddle a boundary.
                begin = max(0, int(round((start_ms - chunk_start) * rate / 1000.0)))
                stop = handle.frames
                if end_ms is not None:
                    stop = min(stop, int(round((end_ms - chunk_start) * rate / 1000.0)))
                if stop <= begin:
                    continue
                handle.seek(begin)
                remaining = stop - begin
                while remaining > 0:
                    block = handle.read(min(65536, remaining), dtype="int16",
                                        always_2d=True)
                    if len(block) == 0:
                        break
                    writer.write(block)
                    written_frames += len(block)
                    remaining -= len(block)
    finally:
        if writer is not None:
            writer.close()

    if writer is None or written_frames == 0:
        out_path.unlink(missing_ok=True)
        raise ApiError("INVALID_REQUEST",
                       "No audio falls inside the requested range")

    return AudioSlice(
        path=out_path,
        seconds=written_frames / float(rate),
        sample_rate=rate,
        channels=channels,
        segment_index=segment_index,
        fmt=fmt,
    )


def slices_for(session_id: str, out_dir: Path) -> list[AudioSlice]:
    """One slice per patient segment, or one for the whole session.

    A multi-patient recording is split on the boundaries the recorder reported.
    Segments are never merged: keeping two patients' audio apart is the whole
    point of the boundary events.
    """
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    if session["mode"] != "multi_patient":
        return [build_slice(session_id, out_dir)]
    segments = sessions.patient_segments(session_id)
    if len(segments) <= 1:
        return [build_slice(session_id, out_dir)]
    out = []
    for seg in segments:
        out.append(build_slice(session_id, out_dir, segment_index=seg["index"],
                               start_ms=int(seg["start_ms"]),
                               end_ms=int(seg["end_ms"]) if seg["end_ms"] is not None else None))
    return out
