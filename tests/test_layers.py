"""Regra das camadas: `radClock.core` não importa pygame nem `radClock.ui`."""
import pkgutil
import subprocess
import sys

import radClock.core


def core_modules() -> list[str]:
    return [f"radClock.core.{info.name}" for info in pkgutil.iter_modules(radClock.core.__path__)]


def test_core_has_the_expected_modules():
    assert "radClock.core.fetcher" in core_modules()
    assert "radClock.core.echo" in core_modules()


def test_core_imports_neither_pygame_nor_ui():
    # processo novo: neste aqui outros testes já importaram pygame
    script = (
        "import importlib, sys\n"
        f"for name in {core_modules()!r}:\n"
        "    importlib.import_module(name)\n"
        "bad = sorted(m for m in sys.modules if m == 'pygame' or m.startswith(('pygame.', 'radClock.ui')))\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            check=True, cwd=str(__import__("pathlib").Path(__file__).parents[1]))
    assert result.stdout.strip() == ""
