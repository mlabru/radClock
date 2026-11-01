import sqlite3

import pytest

from radClock.core.acdb import AircraftDB
from radClock.core.model import Aircraft

SCHEMA = """
CREATE TABLE silhouettes (id INTEGER PRIMARY KEY, name TEXT, png BLOB);
CREATE TABLE types (icao TEXT PRIMARY KEY, mfr TEXT, model TEXT, cls TEXT, eng_n INTEGER,
                    eng_t INTEGER, eng TEXT, span_m REAL, len_m REAL, mtow_kg REAL,
                    cruise_kt REAL, sil INTEGER);
CREATE TABLE cat_sil (cat TEXT PRIMARY KEY, sil INTEGER);
CREATE TABLE operators (code TEXT PRIMARY KEY, name TEXT, radio TEXT);
CREATE TABLE hex_blocks (lo INTEGER PRIMARY KEY, hi INTEGER, country TEXT);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "aircraft.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO types VALUES ('B738','BOEING','737-800','LandPlane',2,3,'CFM56',"
              "34.3,39.5,79000,453,7)")
    c.execute("INSERT INTO types VALUES ('ZZZZ','X','Y','',0,0,'',NULL,NULL,NULL,NULL,NULL)")
    c.execute("INSERT INTO cat_sil VALUES ('A1', 3)")
    c.execute("INSERT INTO operators VALUES ('GLO','GOL LINHAS AEREAS S.A.','GOL TRANSPORTE')")
    c.execute("INSERT INTO hex_blocks VALUES (?, ?, 'Brazil')", (0xE40000, 0xE7FFFF))
    c.execute("INSERT INTO hex_blocks VALUES (?, ?, 'Argentina')", (0xE00000, 0xE3FFFF))
    c.commit()
    c.close()
    return AircraftDB(path)


def ac(**kw):
    return Aircraft(hex=kw.pop("hex", "e4917b"), lat=0, lon=0, **kw)


def test_full_card(db):
    card = db.card(ac(type="B738", callsign="GLO1234", category="A3"))
    assert card.type.mfr == "BOEING" and card.type.span_m == 34.3
    assert card.type.eng_kind == "jato" and card.type.eng_n == 2
    assert (card.operator, card.radio, card.country) == ("GOL LINHAS AEREAS S.A.",
                                                         "GOL TRANSPORTE", "Brazil")
    assert card.sil == 7 and not card.sil_generic


def test_generic_silhouette_by_category(db):
    card = db.card(ac(type="ZZZZ", category="A1"))
    assert card.type.span_m is None
    assert card.sil == 3 and card.sil_generic


def test_registration_callsign_has_no_operator(db):
    assert db.operator("PRGXA") is None          # matrícula, não código de empresa
    assert db.operator("GLO") is None


def test_country_lookup_edges(db):
    assert db.country("e40000") == "Brazil"
    assert db.country("e3ffff") == "Argentina"
    assert db.country("f00000") == ""            # fora de todos os blocos
    assert db.country("~e40000") == ""           # TIS-B: endereço não-ICAO


def test_missing_file_is_harmless(tmp_path):
    db = AircraftDB(tmp_path / "nope.db")
    assert not db.available
    card = db.card(ac(type="B738", callsign="GLO1234", category="A3"))
    assert card.type is None and card.sil is None and card.country == ""
