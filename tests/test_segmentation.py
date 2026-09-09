"""Patient segmentation when the recorder's clock and the audio disagree.

A privacy pause keeps the recorder's clock running while producing no audio.
Every event after it therefore sits further along the clock than it does in
the recording. Reading those offsets as audio positions put the beginning of
one patient inside the previous patient's segment -- in David's round, five
seconds of patient 3 attached to patient 2's note.

The numbers here are his: four chunks of 30.000, 21.750, 5.875 and 17.875
seconds, a pause from 51.876 to 56.982, and patient boundaries at 30.016 and
62.864 on the recorder's clock.
"""
from __future__ import annotations

import pytest

from conftest import Recorder

CHUNKS = (30.000, 21.750, 5.875, 17.875)
PAUSE = (51876, 56982)
BOUNDARIES = (30016, 62864)


def _round(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient")
    for i, seconds in enumerate(CHUNKS):
        rec.add_chunk(seconds=seconds, seed=i)
    rec.create()
    rec.upload_all()
    rec.send_events([
        {"event": "session_started", "offset_ms": 0},
        {"event": "patient_boundary", "offset_ms": BOUNDARIES[0]},
        {"event": "privacy_pause_started", "offset_ms": PAUSE[0]},
        {"event": "privacy_pause_ended", "offset_ms": PAUSE[1]},
        {"event": "patient_boundary", "offset_ms": BOUNDARIES[1]},
    ])
    assert rec.complete().json()["ingest_confirmed"] is True
    return rec


def test_a_boundary_after_a_pause_lands_on_the_right_audio(server):
    from app import sessions

    rec = _round(server)
    segments = sessions.patient_segments(rec.session_id)
    assert len(segments) == 3, segments

    # chunk edges in audio time: 0, 30000, 51750, 57625
    assert segments[0]["start_ms"] == 0
    assert segments[1]["start_ms"] == pytest.approx(30000, abs=50)
    # the one that was wrong: 62864 on the clock is 57758 in the audio, and
    # the recorder starts a new chunk there, so it snaps to 57625.
    assert segments[2]["start_ms"] == pytest.approx(57625, abs=50)
    # ...and emphatically NOT the raw clock offset, which sits 5.1 seconds
    # inside chunk 4 and would hand the front of patient 3 to patient 2.
    assert abs(segments[2]["start_ms"] - BOUNDARIES[1]) > 4000


def test_the_conversion_itself(server):
    from app import sessions

    gaps = [{"start_ms": 51876, "end_ms": 56982, "closed": True}]
    # before the pause: unchanged
    assert sessions.to_audio_ms(30016, gaps) == 30016
    # after it: shifted back by the whole pause
    assert sessions.to_audio_ms(62864, gaps) == 57758
    # inside it: the audio stopped when the pause began
    assert sessions.to_audio_ms(54000, gaps) == 51876
    # no gaps at all: identity
    assert sessions.to_audio_ms(12345, []) == 12345


def test_two_pauses_accumulate(server):
    from app import sessions

    gaps = [{"start_ms": 10000, "end_ms": 12000, "closed": True},
            {"start_ms": 30000, "end_ms": 35000, "closed": True}]
    assert sessions.to_audio_ms(5000, gaps) == 5000
    assert sessions.to_audio_ms(20000, gaps) == 18000     # minus 2s
    assert sessions.to_audio_ms(50000, gaps) == 43000     # minus 2s and 5s


def test_a_pause_that_never_ended(server):
    """The recorder stopped while still paused: nothing after it has audio."""
    from app import sessions

    gaps = [{"start_ms": 10000, "end_ms": None, "closed": False}]
    assert sessions.to_audio_ms(9000, gaps) == 9000
    assert sessions.to_audio_ms(90000, gaps) == 10000


def test_a_boundary_far_from_any_chunk_start_is_not_snapped(server):
    """Snapping expresses a promise the recorder makes; when the offset is
    nowhere near a chunk start that promise does not hold for this recording,
    and the arithmetic is better than a guess."""
    from app import sessions

    edges = [0, 30000, 51750, 57625]
    assert sessions._snap_to_chunk(57758, edges) == 57625     # within tolerance
    assert sessions._snap_to_chunk(40000, edges) == 40000     # 10s away: left alone


def test_the_segments_still_cover_the_whole_recording(server):
    from app import sessions

    rec = _round(server)
    segments = sessions.patient_segments(rec.session_id)
    total = sum(s["duration_ms"] for s in segments if s["duration_ms"] is not None)
    audio_ms = int(sum(CHUNKS) * 1000)
    assert total == pytest.approx(audio_ms, abs=100), (segments, audio_ms)
    # and they are contiguous: no audio belongs to two patients, none to none
    for a, b in zip(segments, segments[1:]):
        assert a["end_ms"] == b["start_ms"]


def test_snapping_may_never_merge_two_patients(server):
    """The one outcome this whole mechanism exists to prevent.

    A boundary close to the start of the recording must not be snapped to 0,
    and two boundaries must not be snapped onto the same chunk start. Either
    would put two people's consultations in one note -- far worse than a
    timestamp that is half a second out.
    """
    from app import sessions

    edges = [0, 30000]
    # would collapse the first patient into the second
    assert sessions._snap_to_chunk(500, edges) == 500
    # second boundary cannot take a position the first already holds
    assert sessions._snap_to_chunk(30100, edges, taken=set()) == 30000
    assert sessions._snap_to_chunk(30100, edges, taken={30000}) == 30100


def test_two_boundaries_near_one_chunk_start_stay_two_segments(server):
    from app import sessions

    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient")
    for i in range(2):
        rec.add_chunk(seconds=10.0, seed=i)
    rec.create(); rec.upload_all()
    # two patients changing over within a second of each other, both near the
    # chunk edge at 10.000
    rec.send_events([{"event": "patient_boundary", "offset_ms": 9900},
                     {"event": "patient_boundary", "offset_ms": 10200}])
    rec.complete()

    segments = sessions.patient_segments(rec.session_id)
    starts = [s["start_ms"] for s in segments]
    assert len(segments) == 3, segments
    assert len(set(starts)) == 3, starts


# ---------------------------------------------------------------------------
# clocks on screen
# ---------------------------------------------------------------------------

def test_times_are_shown_on_the_practice_clock(server, monkeypatch):
    """Stored in UTC, shown in local time.

    A round recorded at 21:45 in Leusden was displayed as 19:45, because the
    stored UTC string went straight to the page.
    """
    from app.util import local_time

    monkeypatch.setenv("VS_DISPLAY_TZ", "Europe/Amsterdam")
    assert local_time("2026-09-09T19:45:08Z") == "2026-09-09 21:45:08"
    assert local_time("2026-09-09T19:45:08Z", with_date=False) == "21:45:08"

    # winter time is an hour less, so this is a real conversion and not +2
    assert local_time("2026-01-15T09:00:00Z") == "2026-01-15 10:00:00"

    # an unset or unknown zone must not break a page
    monkeypatch.setenv("VS_DISPLAY_TZ", "Mars/Olympus_Mons")
    assert local_time("2026-09-09T19:45:08Z") == "2026-09-09 19:45:08"
    assert local_time(None) == ""


def test_the_admin_page_shows_local_times(server, monkeypatch):
    monkeypatch.setenv("VS_DISPLAY_TZ", "Europe/Amsterdam")
    rec = _round(server)
    server.admin_login()
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    # the raw UTC form must not be on the page any more
    assert "T" + "Z" not in page
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", page)
