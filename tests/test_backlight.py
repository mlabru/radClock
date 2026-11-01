"""Brilho pelo /sys/class/backlight, com uma pasta falsa no lugar do sysfs."""
from pathlib import Path

from radClock.core.backlight import Backlight


def make_device(sysfs: Path, name: str = "10-0045", max_level: int = 255,
                level: int = 200) -> Path:
    folder = sysfs / name
    folder.mkdir(parents=True)
    (folder / "max_brightness").write_text(f"{max_level}\n")
    (folder / "brightness").write_text(f"{level}\n")
    return folder


def level(folder: Path) -> int:
    return int((folder / "brightness").read_text())


def test_auto_finds_the_first_device(tmp_path):
    make_device(tmp_path, "b-second")
    first = make_device(tmp_path, "a-first")
    backlight = Backlight.find("auto", sysfs=tmp_path)
    assert backlight is not None and backlight.folder == first


def test_no_device_or_empty_setting_means_no_control(tmp_path):
    assert Backlight.find("auto", sysfs=tmp_path) is None             # sysfs vazio
    assert Backlight.find("auto", sysfs=tmp_path / "nao-existe") is None
    make_device(tmp_path)
    assert Backlight.find("", sysfs=tmp_path) is None                 # "" = não mexer
    assert Backlight.find(str(tmp_path / "outro")) is None            # pasta errada


def test_percent_is_scaled_and_never_zero(tmp_path):
    folder = make_device(tmp_path, max_level=255, level=255)
    backlight = Backlight.find(str(folder))
    backlight.set_percent(40)
    assert level(folder) == 102
    backlight.set_percent(0)
    assert level(folder) == 1                       # 0 apagaria a tela de vez


def test_writes_only_on_change_and_restores_on_exit(tmp_path):
    folder = make_device(tmp_path, level=200)
    backlight = Backlight.find(str(folder))
    backlight.set_percent(10)
    (folder / "brightness").write_text("77\n")      # alguém mexeu por fora
    backlight.set_percent(10)                       # mesmo valor: não escreve de novo
    assert level(folder) == 77
    backlight.restore()
    assert level(folder) == 200


def test_write_error_is_logged_once_and_never_raises(tmp_path, caplog):
    folder = make_device(tmp_path)
    backlight = Backlight.find(str(folder))
    (folder / "brightness").unlink()
    (folder / "brightness").mkdir()                 # escrever num diretório falha
    backlight.set_percent(40)
    backlight.set_percent(10)
    backlight.restore()
    assert sum("brilho" in record.message for record in caplog.records
               if record.levelname == "WARNING") == 1
