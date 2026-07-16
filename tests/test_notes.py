"""Notes: creation, network scoping, range filtering, deletion."""

import pytest

from netmon_server.db import connect, get_or_create_network, init_db
from netmon_server.notes import create_note, delete_note, list_notes


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def nets(conn):
    return {
        "home": get_or_create_network(conn, "home", "Home"),
        "office": get_or_create_network(conn, "office", "Office"),
    }


def test_create_and_list(conn, nets):
    n = create_note(conn, 1000.0, "router restarted", ["home"])
    assert n["text"] == "router restarted"
    assert [w["name"] for w in n["networks"]] == ["home"]
    got = list_notes(conn, 0, 2000)
    assert [x["id"] for x in got] == [n["id"]]


def test_general_note_has_no_networks(conn, nets):
    n = create_note(conn, 1000.0, "ISP maintenance", [])
    assert n["networks"] == []


def test_scoping_filter(conn, nets):
    general = create_note(conn, 1000.0, "general", [])
    home = create_note(conn, 1100.0, "home only", ["home"])
    office = create_note(conn, 1200.0, "office only", ["office"])
    both = create_note(conn, 1300.0, "both", ["home", "office"])

    for_home = list_notes(conn, 0, 2000, ["home"])
    assert [n["id"] for n in for_home] == [general["id"], home["id"], both["id"]]

    for_both = list_notes(conn, 0, 2000, ["home", "office"])
    assert [n["id"] for n in for_both] == [general["id"], home["id"],
                                           office["id"], both["id"]]

    unfiltered = list_notes(conn, 0, 2000)
    assert len(unfiltered) == 4


def test_range_filter(conn, nets):
    create_note(conn, 1000.0, "early", [])
    late = create_note(conn, 5000.0, "late", [])
    got = list_notes(conn, 2000, 9000)
    assert [n["id"] for n in got] == [late["id"]]


def test_empty_text_rejected(conn, nets):
    with pytest.raises(ValueError):
        create_note(conn, 1000.0, "   ", [])


def test_unknown_network_rejected(conn, nets):
    with pytest.raises(ValueError, match="Unknown network"):
        create_note(conn, 1000.0, "text", ["nope"])
    # nothing half-inserted
    assert list_notes(conn, 0, 2000) == []


def test_delete_cascades(conn, nets):
    n = create_note(conn, 1000.0, "to delete", ["home", "office"])
    assert delete_note(conn, n["id"]) is True
    assert list_notes(conn, 0, 2000) == []
    links = conn.execute("SELECT COUNT(*) c FROM note_networks").fetchone()["c"]
    assert links == 0
    assert delete_note(conn, n["id"]) is False


def test_schema_migrates_existing_db(tmp_path):
    """init_db on a DB created before the notes tables adds them."""
    db_path = str(tmp_path / "old.db")
    init_db(db_path)
    c = connect(db_path)
    c.execute("DROP TABLE note_networks")
    c.execute("DROP TABLE notes")
    c.commit()
    c.close()
    init_db(db_path)
    c = connect(db_path)
    get_or_create_network(c, "x", "X")
    create_note(c, 1.0, "works", ["x"])
    c.close()
