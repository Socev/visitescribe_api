"""A recorder may send a category the server has never seen.

It must become a category like any other: stored, shown in every user's
settings, routable automatically -- not refused because of its spelling.
"""
from __future__ import annotations

import pytest

from conftest import Recorder


@pytest.mark.parametrize("raw, key", [
    ("MDO", "mdo"), ("mdo", "mdo"), ("Tel-consult", "tel_consult"),
    ("  Huis bezoek ", "huis_bezoek"), ("SINGLE_PATIENT", "single_patient"),
    ("x", "x"), ("2e lijn", "2e_lijn"),
])
def test_modes_are_normalised(raw, key):
    from app import users

    assert users.normalise_mode(raw) == key


def _send(server, mode):
    rec = Recorder(server, mode=mode)
    rec.add_chunk(seconds=1.0)
    response = rec.create()
    return rec, response


def test_an_unknown_category_becomes_a_recording_type(server):
    from app import users

    server.register_device("visitescribe-001")
    rec, response = _send(server, "MDO")
    assert response.status_code == 201, response.text
    row = server.db.query_one("SELECT mode FROM sessions WHERE session_id = ?",
                              (rec.session_id,))
    assert row["mode"] == "mdo"
    kinds = {t["mode"]: t for t in users.recording_types()}
    assert kinds["mdo"]["title"] == "MDO"
    assert kinds["mdo"]["patient_audio"] == 1      # fails closed

    # the same category in another spelling is the same category
    _send(server, "mdo")
    assert sum(1 for t in users.recording_types() if t["mode"] == "mdo") == 1


def test_a_new_category_can_be_routed_automatically(server):
    from app import users

    server.register_device("visitescribe-001")
    _send(server, "Huisbezoek")
    user = users.create("dokter@example.nl")
    users.bind_device("visitescribe-001", user["user_id"])
    users.set_rule(user["user_id"], "huisbezoek", route="ourmind", auto=True)
    assert users.rule(user["user_id"], "huisbezoek")["auto"] == 1
    assert users.type_title("huisbezoek") == "Huisbezoek"


def test_a_mode_with_nothing_usable_is_refused(server):
    server.register_device("visitescribe-001")
    _, response = _send(server, "---")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_MANIFEST"


def test_the_round_is_called_visite():
    from app import users

    assert users.type_title("multi_patient") == "Visite"
    assert users.type_title("meeting") == "Vergadering"
