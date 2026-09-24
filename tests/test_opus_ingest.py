"""Ogg/Opus sessions: the compact upload route (PC sync app, later the recorder).

The recorder keeps its lossless 48 kHz stereo master on the SD card; what goes
over the wire only has to be good speech. A session declared ``codec: "opus"``
therefore carries self-contained Ogg/Opus chunks of 16 kHz mono at ~24 kbit/s,
about a tenth of the PCM16 route.

What these tests pin down:

* each chunk's duration is sample-exact, because patient segmentation adds
  chunk durations together and snaps boundaries to chunk edges;
* the codec is bound to the session: lossy audio never lands in a session that
  promised lossless, and a legacy session behaves exactly as before 1.6.0;
* an Opus session reassembles into every container a provider takes, and a
  segment cut out of it is exactly as long as the boundaries say.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import shutil
import struct
import subprocess

import pytest
import soundfile as sf

from conftest import Recorder, make_flac
from test_wav_ingest import make_wav


RATE = 16000


# --------------------------------------------------------------------------
# Building and taking apart Ogg/Opus
# --------------------------------------------------------------------------

def speech_like(seconds: float, seed: int = 0):
    import numpy as np

    rng = np.random.default_rng(seed)
    frames = int(round(seconds * RATE))
    t = np.arange(frames) / RATE
    voiced = 0.2 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    return (np.clip(voiced + 0.02 * rng.standard_normal(frames), -1, 1) * 32767
            ).astype("int16")


def make_opus(seconds: float = 1.0, seed: int = 0, channels: int = 1) -> bytes:
    """A complete Ogg/Opus stream, as libsndfile (libopus) writes it."""
    samples = speech_like(seconds, seed)
    if channels == 2:
        import numpy as np
        samples = np.column_stack([samples, samples])
    buf = io.BytesIO()
    sf.write(buf, samples, RATE, format="OGG", subtype="OPUS")
    return buf.getvalue()


def split_pages(data: bytes) -> list[dict]:
    pages, pos = [], 0
    while pos < len(data):
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        end = pos + 27 + nseg + sum(lacing)
        pages.append({
            "flags": data[pos + 5],
            "granule": struct.unpack("<q", data[pos + 6:pos + 14])[0],
            "serial": struct.unpack("<I", data[pos + 14:pos + 18])[0],
            "sequence": struct.unpack("<I", data[pos + 18:pos + 22])[0],
            "lacing": lacing,
            "body": data[pos + 27 + nseg:end],
        })
        pos = end
    return pages


def join_pages(pages: list[dict], fix_crc: bool = True) -> bytes:
    from app.flacinfo import _ogg_crc

    out = b""
    for p in pages:
        header = (b"OggS" + bytes([0, p["flags"]]) + struct.pack("<q", p["granule"])
                  + struct.pack("<II", p["serial"], p["sequence"]) + b"\x00" * 4
                  + bytes([len(p["lacing"])]) + p["lacing"])
        page = header + p["body"]
        crc = _ogg_crc(page) if fix_crc else 0xDEADBEEF
        out += page[:22] + struct.pack("<I", crc) + page[26:]
    return out


def chunk_info(server, session_id: str, sequence: int) -> dict:
    row = server.db.query_one(
        "SELECT flac_json FROM chunks WHERE session_id = ? AND sequence = ?",
        (session_id, sequence),
    )
    return json.loads(row["flac_json"] or "{}")


# The exact audio block docs/RECORDER.md tells the PC app and recorder to send.
CONTRACT_AUDIO = {
    "codec": "opus",
    "container": "ogg",
    "sample_rate": 16000,
    "channels": 1,
    "sample_format": "opus",
    "chunk_seconds": 30,
    "bitrate": 24000,
    "frame_ms": 20,
}


def opus_manifest(rec: Recorder, **overrides) -> dict:
    manifest = rec.manifest()
    manifest["audio"] = dict(CONTRACT_AUDIO, **overrides)
    return manifest


def opus_recorder(server, **kwargs) -> Recorder:
    server.register_device("visitescribe-001")
    return Recorder(server, sample_rate=RATE, channels=1, **kwargs)


# --------------------------------------------------------------------------
# The parser, on its own
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seconds", [0.02, 0.5, 12.345, 29.99, 30.0])
def test_opus_duration_is_sample_exact(seconds):
    """Segmentation adds these up; an off-by-pre-skip here moves a patient."""
    from app import flacinfo

    info = flacinfo.validate(make_opus(seconds))
    assert info.codec == "opus"
    assert info.sample_rate == RATE
    assert info.channels == 1
    assert info.total_samples == int(round(seconds * RATE))
    assert info.deep_verified is True
    assert info.decoder == "libsndfile-ogg-opus"


def test_structural_only_opus_still_reports_duration():
    from app import flacinfo

    info = flacinfo.validate(make_opus(2.0), deep=False)
    assert info.deep_verified is False
    assert info.total_samples == 32000
    assert info.duration_ms == 2000
    assert info.decoder == "structural-ogg-opus"


def test_a_30_second_chunk_is_a_tenth_of_pcm16():
    """The point of the exercise: 960 kB of PCM16 becomes roughly 100 kB."""
    size = len(make_opus(30.0))
    assert size < 160_000, size


def _mangle_granule_middle(pages):
    pages[3]["granule"] += 960
    return pages


def _mangle_rate(pages):
    head = bytearray(pages[0]["body"])
    head[12:16] = struct.pack("<I", 44100)
    pages[0]["body"] = bytes(head)
    return pages


def _mangle_family(pages):
    head = bytearray(pages[0]["body"])
    head[18] = 1
    pages[0]["body"] = bytes(head)
    return pages


def _drop_eos(pages):
    pages[-1]["flags"] &= ~0x04
    return pages


def _gap(pages):
    for p in pages[3:]:
        p["sequence"] += 1
    return pages


def _second_stream(pages):
    pages[4]["serial"] ^= 1
    return pages


def _lying_final_granule(pages):
    pages[-1]["granule"] += 48000 * 60
    return pages


def _no_audio(pages):
    pages = pages[:2]
    pages[-1]["flags"] |= 0x04
    return pages


def _no_bos(pages):
    pages[0]["flags"] &= ~0x02
    return pages


@pytest.mark.parametrize("mangle, expected", [
    (_mangle_granule_middle, "does not match"),
    (_mangle_rate, "input_sample_rate 44100"),
    (_mangle_family, "mapping family"),
    (_drop_eos, "EOS"),
    (_gap, "sequence jumps"),
    (_second_stream, "more than one logical"),
    (_lying_final_granule, "final granule"),
    (_no_audio, "no audio packets"),
    (_no_bos, "BOS"),
])
def test_malformed_opus_is_refused(mangle, expected):
    from app import flacinfo

    pages = split_pages(make_opus(3.0))
    with pytest.raises(flacinfo.FlacError) as exc:
        flacinfo.validate(join_pages(mangle(pages)))
    assert expected in str(exc.value)


def test_a_bad_ogg_crc_is_refused():
    from app import flacinfo

    with pytest.raises(flacinfo.FlacError, match="CRC"):
        flacinfo.validate(join_pages(split_pages(make_opus(1.0)), fix_crc=False))


@pytest.mark.parametrize("cut", [lambda d: d[:-10], lambda d: d + b"junk" * 10,
                                 lambda d: d[:30]])
def test_a_truncated_or_padded_opus_stream_is_refused(cut):
    from app import flacinfo

    with pytest.raises(flacinfo.FlacError):
        flacinfo.validate(cut(make_opus(1.0)))


def test_the_decoded_size_limit_applies_to_opus():
    from app import flacinfo

    data = make_opus(10.0)
    with pytest.raises(flacinfo.FlacError, match="above the"):
        flacinfo.validate(data, deep=False, max_decoded_bytes=100_000)


def test_the_codec_is_bound_to_the_manifest():
    from app import flacinfo

    opus = flacinfo.validate(make_opus(0.5))
    wav = flacinfo.validate(make_wav(seconds=0.5, sample_rate=RATE))
    flac = flacinfo.validate(make_flac(0.5, RATE, 1))

    assert flacinfo.check_against_manifest(opus, CONTRACT_AUDIO) == []
    assert flacinfo.check_against_manifest(wav, CONTRACT_AUDIO)
    assert flacinfo.check_against_manifest(flac, CONTRACT_AUDIO)
    for legacy in ({"codec": "wav"}, {"codec": "flac"}, {}):
        assert flacinfo.check_against_manifest(opus, legacy)
        assert flacinfo.check_against_manifest(wav, legacy) == []


# --------------------------------------------------------------------------
# The real socket
# --------------------------------------------------------------------------

def v4_chunk(rec: Recorder, opus: bytes) -> dict:
    """A chunk encrypted the way the v4 contract describes, AAD and all."""
    from app.crypto import encrypt_chunk_for_test
    from app.util import sha256_hex

    seq = len(rec.chunks) + 1
    nonce = hmac.new(rec.session_key, f"nonce:v4-opus:{rec.session_id}:{seq}".encode(),
                     hashlib.sha256).digest()[:12]
    aad = f"visitescribe-v4-opus:{rec.session_id}:{seq}"
    ciphertext = encrypt_chunk_for_test(rec.session_key, nonce, aad.encode(), opus)
    chunk = {
        "sequence": seq,
        "file": f"audio/chunk-{seq:06d}.opus.enc",
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "aad": aad,
        "plaintext_sha256": sha256_hex(opus),
        "ciphertext_sha256": sha256_hex(ciphertext),
        "plaintext_size": len(opus),
        "ciphertext_size": len(ciphertext),
        "_plaintext": opus,
        "_ciphertext": ciphertext,
    }
    rec.chunks.append(chunk)
    return chunk


def test_the_contract_session_is_accepted_end_to_end(server):
    rec = opus_recorder(server)
    for i in range(3):
        v4_chunk(rec, make_opus(2.0, seed=i))

    created = rec.create(manifest=opus_manifest(rec))
    assert created.status_code == 201, created.text
    for response in rec.upload_all():
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["codec"] == "opus"
        assert body["flac_valid"] is True
        assert body["flac_deep_verified"] is True
        assert body["duration_ms"] == 2000
    done = rec.complete()
    assert done.status_code == 200
    assert done.json()["ingest_confirmed"] is True

    info = chunk_info(server, rec.session_id, 2)
    assert info["codec"] == "opus"
    assert info["total_samples"] == 32000


@pytest.mark.parametrize("override, fragment", [
    ({"sample_rate": 48000}, None),   # a valid Opus rate, just not ours -- allowed
    ({"sample_rate": 44100}, "sample_rate"),
    ({"sample_format": "S16_LE"}, "sample_format"),
    ({"container": "webm"}, "container"),
    ({"channels": 6}, "channels"),
])
def test_an_opus_manifest_is_checked_when_the_session_is_created(server, override, fragment):
    rec = opus_recorder(server)
    v4_chunk(rec, make_opus(0.5))
    response = rec.create(manifest=opus_manifest(rec, **override))
    if fragment is None:
        assert response.status_code == 201, response.text
        return
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_MANIFEST"
    assert fragment in error["message"]


def test_a_legacy_session_still_refuses_lossy_audio(server):
    """A v3 manifest (codec wav) promised lossless; an Opus chunk breaks that."""
    rec = opus_recorder(server)
    v4_chunk(rec, make_opus(0.5))
    manifest = rec.manifest()
    manifest["audio"] = {"codec": "wav", "sample_rate": RATE, "channels": 1,
                         "sample_format": "S16_LE", "chunk_seconds": 30}
    assert rec.create(manifest=manifest).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 422, put.text
    assert put.json()["error"]["code"] == "INVALID_FLAC"
    assert "lossy audio is only accepted" in put.json()["error"]["message"]
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,)
    )["n"] == 0


def test_an_opus_session_refuses_a_wav_chunk(server):
    rec = opus_recorder(server)
    v4_chunk(rec, make_wav(seconds=0.5, sample_rate=RATE))
    assert rec.create(manifest=opus_manifest(rec)).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 422, put.text
    assert "codec opus" in put.json()["error"]["message"]


def test_an_opus_chunk_at_the_wrong_rate_is_refused(server):
    """The manifest says 16 kHz; OpusHead must say the same."""
    rec = opus_recorder(server)
    samples = speech_like(0.5)
    buf = io.BytesIO()
    sf.write(buf, samples, 24000, format="OGG", subtype="OPUS")
    v4_chunk(rec, buf.getvalue())
    assert rec.create(manifest=opus_manifest(rec)).status_code == 201
    put = rec.put_chunk(1)
    assert put.status_code == 422, put.text
    assert "sample_rate" in put.json()["error"]["message"]


def test_a_v3_session_cannot_be_re_declared_as_opus(server):
    """Same session_id, different audio block: refused, never silently mixed.

    This is why the PC app finishes a session in the format it was created in.
    """
    rec = opus_recorder(server)
    v4_chunk(rec, make_opus(0.5))
    legacy = rec.manifest()
    legacy["audio"] = {"codec": "wav", "sample_rate": RATE, "channels": 1,
                       "sample_format": "S16_LE", "chunk_seconds": 30}
    assert rec.create(manifest=legacy).status_code == 201
    again = rec.create(manifest=opus_manifest(rec), idem="another-key")
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# --------------------------------------------------------------------------
# Out the other end
# --------------------------------------------------------------------------

def _finish(rec: Recorder):
    assert rec.create(manifest=opus_manifest(rec)).status_code == 201
    for response in rec.upload_all():
        assert response.status_code == 200, response.text
    assert rec.complete().status_code == 200


@pytest.mark.parametrize("fmt", ["flac", "wav", "mp3", "ogg"])
def test_an_opus_session_reassembles_into_every_provider_format(server, tmp_path, fmt):
    from app import audio

    rec = opus_recorder(server)
    for i in range(3):
        v4_chunk(rec, make_opus(2.0, seed=i))
    _finish(rec)

    sliced = audio.build_slice(rec.session_id, tmp_path, fmt=fmt)
    assert sliced.sample_rate == RATE
    if fmt in ("flac", "wav"):
        assert sliced.seconds == 6.0          # exact: 96000 frames
    with sf.SoundFile(io.BytesIO(sliced.path.read_bytes())) as handle:
        assert handle.samplerate == RATE
        assert handle.channels == 1


def test_patient_segments_of_an_opus_session_are_sample_exact(server, tmp_path):
    from app import audio, sessions

    rec = opus_recorder(server, mode="multi_patient")
    for i, seconds in enumerate((2.0, 1.5, 2.5)):
        v4_chunk(rec, make_opus(seconds, seed=i))
    assert rec.create(manifest=opus_manifest(rec)).status_code == 201
    for response in rec.upload_all():
        assert response.status_code == 200, response.text
    assert rec.send_events([
        {"event": "session_started", "offset_ms": 0, "at": "2026-09-09T06:00:00+02:00"},
        {"event": "patient_boundary", "offset_ms": 2000, "patient_index": 2,
         "at": "2026-09-09T06:00:02+02:00"},
        {"event": "patient_boundary", "offset_ms": 3500, "patient_index": 3,
         "at": "2026-09-09T06:00:03.5+02:00"},
    ]).status_code == 200
    assert rec.complete().status_code == 200

    assert sessions.chunk_edges(rec.session_id) == [0, 2000, 3500]
    segments = sessions.patient_segments(rec.session_id)
    assert [s["start_ms"] for s in segments] == [0, 2000, 3500]

    middle = audio.build_slice(rec.session_id, tmp_path, segment_index=2,
                               start_ms=2000, end_ms=3500, fmt="flac")
    assert middle.seconds == 1.5
    # A boundary inside a chunk has to seek into Opus and still be exact.
    inside = audio.build_slice(rec.session_id, tmp_path, segment_index=9,
                               start_ms=500, end_ms=2750, fmt="wav")
    assert inside.seconds == 2.25


def test_the_admin_download_labels_an_opus_chunk(server):
    rec = opus_recorder(server)
    v4_chunk(rec, make_opus(0.5))
    _finish(rec)
    server.admin_login()

    got = server.admin.get(
        f"/admin/api/sessions/{rec.session_id}/chunks/1/download?form=decrypted")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("audio/ogg")
    assert got.headers["content-disposition"].endswith('000001.ogg"')
    assert got.content[:4] == b"OggS"


def test_the_admin_session_wav_export_covers_an_opus_session(server):
    rec = opus_recorder(server)
    for i in range(2):
        v4_chunk(rec, make_opus(1.0, seed=i))
    _finish(rec)
    server.admin_login()

    export = server.admin.get(f"/admin/api/sessions/{rec.session_id}/audio.wav")
    assert export.status_code == 200, export.text
    with sf.SoundFile(io.BytesIO(export.content)) as handle:
        assert handle.samplerate == RATE
        assert handle.frames == 32000


# --------------------------------------------------------------------------
# The PC app's encoder, if this machine has it
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("seconds", [30.0, 7.13])
def test_the_documented_ffmpeg_command_produces_an_accepted_chunk(tmp_path, seconds):
    """docs/RECORDER.md gives this exact command line to the PC sync app."""
    from app import flacinfo

    src = tmp_path / "in.wav"
    sf.write(src, speech_like(seconds), RATE, subtype="PCM_16")
    out = tmp_path / "chunk.opus"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000",
         "-c:a", "libopus", "-b:a", "24k", "-vbr", "on", "-compression_level", "10",
         "-application", "voip", "-frame_duration", "20",
         "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
         "-f", "ogg", str(out)],
        check=True,
    )
    info = flacinfo.validate(out.read_bytes())
    assert info.codec == "opus"
    assert info.sample_rate == RATE
    assert info.total_samples == int(round(seconds * RATE))
    assert flacinfo.check_against_manifest(info, CONTRACT_AUDIO) == []

    # Without +bitexact the Ogg serial is random, so a re-encode would change
    # the plaintext hash the manifest pinned. With it, the bytes repeat.
    again = tmp_path / "again.opus"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000",
         "-c:a", "libopus", "-b:a", "24k", "-vbr", "on", "-compression_level", "10",
         "-application", "voip", "-frame_duration", "20",
         "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
         "-f", "ogg", str(again)],
        check=True,
    )
    assert again.read_bytes() == out.read_bytes()
