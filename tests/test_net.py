import io
import ssl

import pytest

from radClock.core import config, net


@pytest.fixture(autouse=True)
def strict_again():
    """O contexto SSL é global no módulo: cada teste termina no padrão."""
    yield
    net.configure_tls(True)


def test_tls_is_strict_by_default_and_can_be_relaxed():
    net.configure_tls(True)
    assert net.tls_is_strict()
    net.configure_tls(False)
    assert not net.tls_is_strict()
    # o resto da verificação continua de pé
    assert net._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert net._ssl_context.check_hostname


def test_requests_use_the_configured_context(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout, context):
        seen.append(context)
        response = io.BytesIO(b"{}")
        response.headers = {}
        return response

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    net.configure_tls(False)
    assert net.http_json("https://exemplo.invalid/x") == {}
    assert seen == [net._ssl_context] and not net.tls_is_strict()


def test_network_section(tmp_path):
    path = tmp_path / "c.toml"
    assert config.load(None).network.strict_tls is True
    path.write_text("[network]\nstrict_tls = false\n")
    assert config.load(path).network.strict_tls is False
    path.write_text('[network]\nstrict_tls = "false"\n')
    with pytest.raises(ValueError, match="strict_tls"):
        config.load(path)
