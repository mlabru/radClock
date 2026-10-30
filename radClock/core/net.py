"""
Funções HTTP e exceções de rede usadas por todo o app.

`http_request` (GET ou POST, com cabeçalhos da resposta), `http_get` e
`http_json` mandam sempre o User-Agent do radClock e transformam qualquer falha
numa destas exceções:
- `SourceError`: falha esperada (rede, arquivo, resposta ruim);
- `HttpStatus`: o servidor respondeu um código de erro (`code`);
- `RateLimited`: HTTP 429 (`retry_after_s`, se o servidor informou).

Quem usa: as fontes de tráfego (`sources`, `opensky`), o eco de chuva
(`echo`), o tempo (`weather`) e a busca de tipo (`lookup`). Por isso fica num
módulo próprio, sem depender de nenhum deles.

TLS: todo HTTPS usa o mesmo contexto SSL do módulo. `configure_tls(False)`
desliga só a verificação estrita do Python 3.13+ (`VERIFY_X509_STRICT`), para
redes com proxy que intercepta o TLS e cuja CA não segue a RFC 5280 (ver
`[network] strict_tls`). A cadeia e o nome do servidor continuam conferidos.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from email.message import Message

from radClock import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"radClock/{__version__}"
DEFAULT_HTTP_TIMEOUT_S = 10.0

# contexto SSL de todos os pedidos HTTPS; trocado só por configure_tls(),
# na partida, antes de as threads de rede começarem
_ssl_context = ssl.create_default_context()


# ---------------------------------------------------------------- erros

class SourceError(Exception):
    """
    Falha esperada de uma fonte (rede, arquivo, resposta ruim).

    A thread de coleta trata este erro como "a fonte está fora" e segue
    tentando. Qualquer outra exceção é tratada como defeito do programa.
    """


class HttpStatus(SourceError):
    """
    O servidor respondeu com um código HTTP de erro (não é falha de rede).

    Atributos:
        code: o código HTTP (ex.: 404, 500).
    """

    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


class RateLimited(HttpStatus):
    """
    HTTP 429: a fonte pediu para diminuir o ritmo.

    Atributos:
        retry_after_s: quantos segundos esperar, se o servidor informou
            (o OpenSky informa; adsb.lol e adsb.fi não). None se não informou.
    """

    def __init__(self, retry_after_s: float | None = None):
        super().__init__(429)
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------- TLS

def configure_tls(strict: bool) -> None:
    """
    Escolhe a verificação de certificados de todos os pedidos HTTPS.

    Chamar uma vez, na partida, antes de iniciar as threads de rede.

    Parâmetros:
        strict: True = padrão do Python. False = tira só a verificação
            estrita (VERIFY_X509_STRICT, ligada por padrão desde o Python
            3.13), que recusa CAs sem a extensão Authority Key Identifier,
            como a de proxies que interceptam o TLS em redes corporativas.
            A cadeia até uma CA instalada no sistema e o nome do servidor
            continuam sendo verificados.
    """
    global _ssl_context
    context = ssl.create_default_context()
    if not strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        log.warning("TLS: verificação estrita desligada ([network] strict_tls = false)")
    _ssl_context = context


def tls_is_strict() -> bool:
    """True se os pedidos HTTPS usam a verificação estrita (VERIFY_X509_STRICT)."""
    return bool(_ssl_context.verify_flags & ssl.VERIFY_X509_STRICT)


# ---------------------------------------------------------------- HTTP

def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """
    Lê o cabeçalho de espera do OpenSky num 429 (None se não houver).
    """
    if not error.headers:
        return None
    value = error.headers.get("X-Rate-Limit-Retry-After-Seconds")
    if value and value.isdigit():
        return float(value)
    return None


def http_request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
                 timeout: float = DEFAULT_HTTP_TIMEOUT_S) -> tuple[bytes, Message]:
    """
    Faz um GET (ou um POST, se `data` for dado) e devolve corpo e cabeçalhos.

    Parâmetros:
        url: endereço completo.
        data: corpo do POST; None faz um GET.
        headers: cabeçalhos extras (o User-Agent do radClock é sempre enviado).
        timeout: limite em segundos para conectar e para cada leitura.

    Retorno:
        (corpo da resposta em bytes, cabeçalhos da resposta).

    Exceções:
        RateLimited: o servidor respondeu 429.
        HttpStatus: o servidor respondeu outro código de erro.
        SourceError: falha de rede (DNS, conexão recusada, timeout...).
    """
    all_headers = {"User-Agent": USER_AGENT}
    if headers:
        all_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=_ssl_context) as response:
            return response.read(), response.headers
    except urllib.error.HTTPError as error:
        # HTTPError precisa vir antes de OSError, porque é subclasse dele
        if error.code == 429:
            raise RateLimited(_retry_after_seconds(error)) from error
        raise HttpStatus(error.code) from error
    except OSError as error:
        # URLError, timeout, conexão recusada; URLError guarda o motivo em .reason
        reason = getattr(error, "reason", error)
        raise SourceError(str(reason)) from error


def http_get(url: str, timeout: float = DEFAULT_HTTP_TIMEOUT_S) -> bytes:
    """GET simples; devolve só o corpo. Exceções: as de `http_request`."""
    body, _headers = http_request(url, timeout=timeout)
    return body


def http_json(url: str, timeout: float = DEFAULT_HTTP_TIMEOUT_S) -> dict:
    """
    GET de um JSON; devolve o documento decodificado.

    Exceções: as de `http_request`, e SourceError se o corpo não é JSON.
    """
    body = http_get(url, timeout)
    try:
        return json.loads(body)
    except ValueError as error:
        raise SourceError(f"JSON inválido: {error}") from error
