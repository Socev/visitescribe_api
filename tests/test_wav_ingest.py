"""The CoreS3-Lite recorder uploads PCM WAV instead of FLAC.

flacinfo grew a second container in e0f8edf, but nothing drove a WAV chunk
through the real ingest endpoint, and nothing checked that a session recorded
in WAV can still be reassembled into the file a provider is handed. A chunk
format the pipeline accepts at the door but cannot decode at the other end is
worse than one it refuses outright: the recorder reports success and the
consultation is unusable.

So these tests build canonical PCM WAV the way recorder firmware does -- by
writing the RIFF header itself, not by asking libsndfile for one -- encrypt it
with the real session key, PUT it at the real socket, and then reassemble the
session through app.audio into every container a provider takes.
"""
from __future__ import annotations

import io
import json
import struct

import pytest
import soundfile as sf

from conftest import Recorder, make_flac


# --------------------------------------------------------------------------
# Canonical PCM WAV, written by hand
# --------------------------------------------------------------------------

def pcm16(seconds: float, sample_rate: int, channels: int, seed: int = 0) -> bytes:
    """Interleaved little-endian signed 16-bit samples."""
    import numpy as np

    rng = np.random.default_rng(seed)
    frames = int(round(seconds * sample_rate))
    t = np.linspace(0, seconds, frames, endpoint=False)
    tone = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.02 * rng.standard_normal(frames)
    data = tone if channels == 1 else np.column_stack([tone] * channels)
    return (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def make_wav(seconds: float = 1.0, sample_rate: int = 16000, channels: int = 1,
             seed: int = 0, bits: int = 16, *, extra_chunks: bytes = b"",
             pcm: bytes | None = None) -> bytes:
    """A 44-byte-header canonical RIFF/WAVE PCM file, built byte by byte.

    `extra_chunks` is inserted between `fmt ` and `data`, which is where a
    recorder that writes a LIST/INFO block would put it.
    """
    body = pcm if pcm is not None else pcm16(seconds, sample_rate, channels, seed)
    block_align = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate,
                      sample_rate * block_align, block_align, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += extra_chunks
    chunks += b"data" + struct.pack("<I", len(body)) + body
    payload = b"WAVE" + chunks
    return b"RIFF" + struct.pack("<I", len(payload)) + payload

def make_ulaw_wav(seconds: float = 1.0, sample_rate: int = 12000,
                  channels: int = 1, seed: int = 0) -> bytes:
    """G.711 mu-law WAV as emitted by the fast demo sync path."""
    import numpy as np

    frames = int(round(seconds * sample_rate))
    t = np.linspace(0, seconds, frames, endpoint=False)
    tone = 0.25 * np.sin(2 * np.pi * 440 * t)
    data = tone if channels == 1 else np.column_stack([tone] * channels)
    out = io.BytesIO()
    sf.write(out, data, sample_rate, format="WAV", subtype="ULAW")
    return out.getvalue()


def chunk_info(server, session_id: str, sequence: int) -> dict:
    row = server.db.query_one(
        "SELECT flac_json FROM chunks WHERE session_id = ? AND sequence = ?",
        (session_id, sequence),
    )
    return json.loads(row["flac_json"] or "{}")


def wav_manifest(rec: Recorder, sample_rate: int, channels: int) -> dict:
    manifest = rec.manifest()
    manifest["audio"] = dict(manifest["audio"], codec="wav",
                             sample_rate=sample_rate, channels=channels)
    return manifest


# --------------------------------------------------------------------------
# The parser, on its own
# --------------------------------------------------------------------------

def test_canonical_wav_parses_to_the_right_duration():
    from app import flacinfo

    info = flacinfo.validate(make_wav(seconds=30, sample_rate=16000, channels=1))
    assert info.valid is True
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.bits_per_sample == 16
    assert info.total_samples == 30 * 16000
    assert info.duration_ms == 30_000
    assert info.deep_verified is True
    assert info.decoder == "libsndfile-wav"


def test_mulaw_wav_parses_and_deep_decodes():
    from app import flacinfo

    info = flacinfo.validate(make_ulaw_wav(seconds=2.0, sample_rate=12000, channels=1))
    assert info.valid is True
    assert info.sample_rate == 12000
    assert info.channels == 1
    assert info.bits_per_sample == 8
    assert info.total_samples == 24000
    assert info.duration_ms == 2000
    assert info.deep_verified is True
    assert info.decoder == "libsndfile-wav"


def test_flac_still_parses_as_flac():
    """The second container must not have cost us the first one."""
    from app import flacinfo

    info = flacinfo.validate(make_flac(1.0, 48000, 1))
    assert info.valid is True
    assert info.duration_ms == 1000
    assert info.decoder == "libsndfile-flac"


def test_structural_only_wav_still_reports_duration():
    """With no decoder the header alone has to carry sample rate and length."""
    from app import flacinfo

    info = flacinfo.validate(make_wav(seconds=2, sample_rate=16000), deep=False)
    assert info.deep_verified is False
    assert info.duration_ms == 2000
    assert info.decoder == "structural-wav"


def test_metadata_chunk_between_fmt_and_data_is_tolerated():
    """The docstring promises forward compatibility; check that it is real."""
    from app import flacinfo

    note = b"LIST" + struct.pack("<I", 4) + b"INFO"
    info = flacinfo.validate(make_wav(seconds=1, extra_chunks=note))
    assert info.duration_ms == 1000
    assert info.total_samples == 16000


@pytest.mark.parametrize("mangle, expected", [
    # a compressed WAV is not lossless and the pipeline must not take it
    (lambda w: w[:20] + struct.pack("<H", 2) + w[22:], "not supported"),
    # header fields that disagree with each other
    (lambda w: w[:32] + struct.pack("<H", 99) + w[34:], "block_align"),
    (lambda w: w[:28] + struct.pack("<I", 12345) + w[32:], "byte_rate"),
    # a data chunk that claims more bytes than were uploaded
    (lambda w: w[:40] + struct.pack("<I", len(w)) + w[44:], "past end"),
    # a RIFF size that claims more than was uploaded
    (lambda w: w[:4] + struct.pack("<I", 1 << 30) + w[8:], "past end"),
    # truncated before the header is complete
    (lambda w: w[:20], "RIFF/WAVE"),
])
def test_malformed_wav_is_refused(mangle, expected):
    from app import flacinfo

    with pytest.raises(flacinfo.FlacError) as exc:
        flacinfo.validate(mangle(make_wav(seconds=0.5)))
    assert expected in str(exc.value)


def test_unaligned_data_size_is_refused():
    """An odd byte count cannot be whole 16-bit frames."""
    from app import flacinfo

    body = pcm16(0.5, 16000, 1)[:-1]
    with pytest.raises(flacinfo.FlacError, match="frame aligned"):
        flacinfo.validate(make_wav(pcm=body))


def test_a_payload_that_is_neither_container_is_refused():
    from app import flacinfo

    with pytest.raises(flacinfo.FlacError, match="neither FLAC nor supported WAV"):
        flacinfo.validate(b"OggS" + b"\x00" * 200)
    with pytest.raises(flacinfo.FlacError):
        flacinfo.validate(b"")


def test_an_oversized_wav_is_refused_before_it_is_decoded():
    """The header alone has to stop it; nothing may allocate first."""
    from app import flacinfo

    header = make_wav(seconds=0.01, sample_rate=16000)
    # Claim a data chunk far past the limit without shipping the bytes.
    huge = header[:40] + struct.pack("<I", 1 << 30) + header[44:]
    with pytest.raises(flacinfo.FlacError) as exc:
        flacinfo.validate(huge)
    assert "past end" in str(exc.value) or "limit" in str(exc.value)


def test_the_size_limit_means_the_same_thing_in_both_layers():
    """Otherwise a stream is accepted at the door and refused by the decoder.

    libsndfile returns int32 frames whatever the container, so the limit has to
    count the audio at that width. Measuring WAV's stored PCM instead lets a
    PCM16 stream through at twice the size FLAC is allowed -- and with deep
    verification switched off it would be accepted outright and only fail much
    later, while a consultation is being reassembled for a provider.
    """
    from app import flacinfo

    # 40 MiB of stored PCM16 is under the 64 MiB limit as bytes-on-the-wire,
    # but decodes to 80 MiB.
    silence = b"\x00" * (40 * 1024 * 1024)
    stream = make_wav(pcm=silence, sample_rate=16000)

    with pytest.raises(flacinfo.FlacError, match="above the"):
        flacinfo.validate(stream, deep=False)
    with pytest.raises(flacinfo.FlacError, match="above the"):
        flacinfo.validate(stream, deep=True)


def test_an_empty_wav_data_chunk_is_flagged_rather_than_silently_zero():
    from app import flacinfo

    info = flacinfo.validate(make_wav(pcm=b""))
    assert info.total_samples == 0
    assert info.duration_ms == 0
    assert any("empty" in w for w in info.warnings)


# --------------------------------------------------------------------------
# The real socket
# --------------------------------------------------------------------------

def test_wav_chunk_is_accepted_by_the_ingest_endpoint(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    rec.add_chunk(flac=make_wav(seconds=1.0, sample_rate=16000))

    assert rec.create(manifest=wav_manifest(rec, 16000, 1)).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 200, put.text
    payload = put.json()
    assert payload["decrypt_verified"] is True
    assert payload["plaintext_sha256_verified"] is True
    assert payload["flac_valid"] is True
    assert payload["flac_deep_verified"] is True

    info = chunk_info(server, rec.session_id, 1)
    assert info["duration_ms"] == 1000
    assert info["sample_rate"] == 16000
    assert info["decoder"] == "libsndfile-wav"


def test_a_corrupt_wav_chunk_is_refused_at_the_socket(server):
    """Authenticated bytes are not automatically usable audio."""
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    good = make_wav(seconds=0.5, sample_rate=16000)
    broken = good[:20] + struct.pack("<H", 2) + good[22:]  # says it is compressed
    rec.add_chunk(flac=broken)

    assert rec.create(manifest=wav_manifest(rec, 16000, 1)).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 422, put.text
    assert put.json()["error"]["code"] == "INVALID_FLAC"
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,)
    )["n"] == 0


def test_a_wav_that_contradicts_the_manifest_is_refused(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    rec.add_chunk(flac=make_wav(seconds=0.5, sample_rate=16000))

    # The recorder declares 48 kHz but ships 16 kHz audio.
    assert rec.create(manifest=wav_manifest(rec, 48000, 1)).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 422, put.text
    assert put.json()["error"]["code"] == "INVALID_FLAC"
    assert "sample_rate" in put.json()["error"]["message"]


# --------------------------------------------------------------------------
# Out the other end: a WAV session must still become provider audio
# --------------------------------------------------------------------------

def _finish(rec: Recorder, sample_rate: int, channels: int):
    assert rec.create(manifest=wav_manifest(rec, sample_rate, channels)).status_code == 201
    for response in rec.upload_all():
        assert response.status_code == 200, response.text
    assert rec.complete().status_code == 200


@pytest.mark.parametrize("fmt", ["flac", "wav", "mp3", "ogg"])
def test_a_wav_session_reassembles_into_every_provider_format(server, tmp_path, fmt):
    from app import audio

    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    for i in range(3):
        rec.add_chunk(flac=make_wav(seconds=2.0, sample_rate=16000, seed=i))
    _finish(rec, 16000, 1)

    sliced = audio.build_slice(rec.session_id, tmp_path, fmt=fmt)
    assert sliced.path.exists()
    assert sliced.seconds == pytest.approx(6.0, abs=0.15)
    with sf.SoundFile(io.BytesIO(sliced.path.read_bytes())) as handle:
        assert handle.samplerate == 16000
        assert handle.channels == 1


def test_a_session_that_changes_container_mid_flight_still_reassembles(server, tmp_path):
    """A practice running both recorders, or one updated between chunks."""
    from app import audio

    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    rec.add_chunk(flac=make_flac(2.0, 16000, 1, seed=1))
    rec.add_chunk(flac=make_wav(seconds=2.0, sample_rate=16000, seed=2))
    rec.add_chunk(flac=make_flac(2.0, 16000, 1, seed=3))
    _finish(rec, 16000, 1)

    sliced = audio.build_slice(rec.session_id, tmp_path, fmt="flac")
    assert sliced.seconds == pytest.approx(6.0, abs=0.15)


def test_the_admin_download_labels_a_chunk_by_its_bytes(server):
    """Blobs are stored under a .flac name whatever the recorder sent."""
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    rec.add_chunk(flac=make_wav(seconds=0.5, sample_rate=16000))
    rec.add_chunk(flac=make_flac(0.5, 16000, 1, seed=9))
    _finish(rec, 16000, 1)
    server.admin_login()

    base = f"/admin/api/sessions/{rec.session_id}/chunks"
    wav = server.admin.get(f"{base}/1/download?form=decrypted")
    assert wav.status_code == 200
    assert wav.headers["content-type"].startswith("audio/wav")
    assert wav.headers["content-disposition"].endswith('000001.wav"')
    assert wav.content[:4] == b"RIFF"

    flac = server.admin.get(f"{base}/2/download?form=decrypted")
    assert flac.status_code == 200
    assert flac.headers["content-type"].startswith("audio/flac")
    assert flac.headers["content-disposition"].endswith('000002.flac"')
    assert flac.content[:4] == b"fLaC"


def test_the_admin_session_wav_export_covers_a_wav_session(server):
    """The export decodes every chunk; a container it cannot read would 500."""
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=16000, channels=1)
    for i in range(2):
        rec.add_chunk(flac=make_wav(seconds=1.0, sample_rate=16000, seed=i))
    _finish(rec, 16000, 1)
    server.admin_login()

    export = server.admin.get(f"/admin/api/sessions/{rec.session_id}/audio.wav")
    assert export.status_code == 200, export.text
    with sf.SoundFile(io.BytesIO(export.content)) as handle:
        assert handle.samplerate == 16000
        assert handle.frames == pytest.approx(32000, abs=32)


def test_patient_segments_of_a_wav_session_use_the_wav_durations(server, tmp_path):
    """Segmentation reads duration_ms out of flac_json; WAV has to fill it in."""
    from app import audio, sessions

    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient", sample_rate=16000, channels=1)
    for i in range(3):
        rec.add_chunk(flac=make_wav(seconds=2.0, sample_rate=16000, seed=i))
    assert rec.create(manifest=wav_manifest(rec, 16000, 1)).status_code == 201
    for response in rec.upload_all():
        assert response.status_code == 200, response.text
    assert rec.send_events([
        {"event": "session_started", "offset_ms": 0,
         "at": "2026-09-09T06:00:00+02:00"},
        {"event": "patient_boundary", "offset_ms": 2000, "patient_index": 2,
         "at": "2026-09-09T06:00:02+02:00"},
        {"event": "patient_boundary", "offset_ms": 4000, "patient_index": 3,
         "at": "2026-09-09T06:00:04+02:00"},
    ]).status_code == 200
    assert rec.complete().status_code == 200

    assert sessions.chunk_edges(rec.session_id) == [0, 2000, 4000]
    segments = sessions.patient_segments(rec.session_id)
    assert [s["start_ms"] for s in segments] == [0, 2000, 4000]
    assert [s["index"] for s in segments] == [1, 2, 3]

    middle = audio.build_slice(rec.session_id, tmp_path, segment_index=2,
                               start_ms=2000, end_ms=4000, fmt="wav")
    assert middle.seconds == pytest.approx(2.0, abs=0.1)
