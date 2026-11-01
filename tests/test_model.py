from radClock.core.model import parse_aircraft_json


def test_readsb_document():
    doc = {"now": 1.0, "aircraft": [
        {"hex": "E48A1B", "flight": "GLO1234 ", "lat": -23.1, "lon": -45.9,
         "alt_baro": 35000, "gs": 450.5, "track": 271.3, "baro_rate": -64,
         "squawk": "7700", "t": "B738", "r": "PR-GXA", "category": "A3",
         "seen": 0.1, "seen_pos": 0.8},
        {"hex": "e48a1c", "alt_baro": 8000},                       # sem posição
        {"hex": "e48a1d", "lat": -23.0, "lon": -45.8, "alt_baro": "ground"},
    ]}
    acs = parse_aircraft_json(doc)
    assert len(acs) == 2
    a = acs[0]
    assert (a.hex, a.callsign, a.alt_ft, a.type, a.reg) == ("e48a1b", "GLO1234", 35000, "B738", "PR-GXA")
    assert a.emergency == "EMERGENCIA"
    assert a.seen_pos_s == 0.8
    g = acs[1]
    assert g.on_ground and g.alt_ft == 0 and g.label == "E48A1D"


def test_v2_api_document_uses_ac_key():
    doc = {"now": 1_700_000_000_000, "ac": [{"hex": "abc123", "lat": 1, "lon": 2, "alt_geom": 1200}]}
    (a,) = parse_aircraft_json(doc)
    assert a.alt_ft == 1200 and a.seen_pos_s == 0.0


def test_bool_is_not_a_number():
    (a,) = parse_aircraft_json({"aircraft": [{"hex": "1", "lat": 0, "lon": 0, "gs": True}]})
    assert a.gs_kt is None


def test_nan_is_not_a_number():
    # o json do Python aceita NaN; uma posição NaN quebraria a projeção
    doc = {"aircraft": [{"hex": "1", "lat": float("nan"), "lon": 0},
                        {"hex": "2", "lat": 0, "lon": 0, "track": float("inf")}]}
    (a,) = parse_aircraft_json(doc)
    assert a.hex == "2" and a.track is None
