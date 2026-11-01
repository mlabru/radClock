import json

from radClock.core.state import StateFile, default_path


def test_roundtrip_and_only_writes_on_change(tmp_path):
    f = tmp_path / "sub" / "state.json"
    st = StateFile(f)
    assert st.get("range_km", int, 50) == 50          # arquivo ainda não existe
    st.set("range_km", 100)
    st.set("echo_on", False)
    mtime = f.stat().st_mtime_ns
    st.set("range_km", 100)                            # igual: não regrava
    assert f.stat().st_mtime_ns == mtime
    again = StateFile(f)
    assert again.get("range_km", int) == 100 and again.get("echo_on", bool) is False


def test_bad_values_are_ignored(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"range_km": True, "echo_on": "sim", "future_key": 1}))
    st = StateFile(f)
    assert st.get("range_km", int, 50) == 50          # bool não passa por int
    assert st.get("echo_on", bool, True) is True
    st.set("range_km", 25)
    assert json.loads(f.read_text())["future_key"] == 1   # chave desconhecida mantida


def test_corrupt_or_unwritable_never_raises(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{trunc")
    assert StateFile(f).data == {}
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        StateFile(ro / "state.json").set("range_km", 10)  # só loga
    finally:
        ro.chmod(0o700)


def test_default_path_prefers_systemd_state_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    assert default_path() == tmp_path / "state.json"
    monkeypatch.delenv("STATE_DIRECTORY")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_path() == tmp_path / "radClock" / "state.json"
