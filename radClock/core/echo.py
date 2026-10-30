"""
Ecos de chuva da RainViewer, reprojetados para o escopo do radar.

O problema: os tiles da RainViewer estão em Web Mercator (a projeção dos
mapas da web), mas o escopo é azimutal equidistante (ver projection.py).
Esticar o mosaico erraria alguns km na borda de 300 km. Em vez disso, cada
pixel do escopo é levado de volta a lat/lon pela projeção inversa e o pixel
correspondente do mosaico é copiado (vizinho mais próximo), tudo vetorizado
em numpy.

Regras da API gratuita desde 2026-01-01 (NÃO afrouxar):
  - zoom máximo 7 (o zoom 8 devolve uma imagem de aviso);
  - só o esquema de cor 2;
  - 100 requisições/min por IP (aqui o limite é 80/min, rajada de 20);
  - um quadro novo a cada 10 min; aqui no máximo 16 tiles por quadro;
  - atribuição: o renderizador escreve "RainViewer" no escopo.

Animação: os últimos `frames` quadros (até 13 = 2 h) são reprojetados para o
alcance atual e a tela passa por eles em loop. No Pi 3 B+ o que pesa é
baixar e reprojetar, por isso:
  - a projeção inversa (trigonometria em cada pixel) só depende do alcance e
    do retângulo de tiles: é calculada uma vez (Reprojector) e cada quadro
    vira só uma cópia indexada de pixels ("gather" do numpy);
  - os tiles ficam em cache como PNG (~30 KB, contra 1 MB decodificado) até
    o quadro sair da janela, e trocar o alcance não rebaixa o que já veio;
  - os tiles de todos os quadros são pedidos de uma vez a 4 downloads em
    paralelo (a latência, ~0,7 s por requisição, dominava), do quadro mais
    novo para o mais velho, sob o limitador de requisições por minuto;
  - o quadro mais novo é montado primeiro: o eco atual aparece logo e a
    animação se completa depois;
  - trocar o alcance interrompe a montagem entre um quadro e outro.

Threads: a tela chama `request()`, `frames()` e `error`; todo o resto roda
na thread EchoFetcher (e os downloads no pool dela).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from radClock.core.config import EchoCfg
from radClock.core.net import SourceError, http_get, http_json
from radClock.core.projection import R_EARTH_KM
from radClock.core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

TILE = 512                      # lado do tile, px
MAX_ZOOM = 7                    # limite da API gratuita
MAX_TILES = 16                  # tiles por quadro, no máximo
MERC_LAT_MAX = 85.05112878      # Web Mercator não vai além disso
RETRY_S = 60.0                  # espera depois de um erro
REQ_PER_MIN = 80                # a RainViewer aceita 100/min por IP; folga para o metadado
REQ_BURST = 20
DOWNLOAD_WORKERS = 4
TILE_CACHE_BYTES = 48 << 20     # 48 MB de PNGs; acima disso só os do retângulo atual ficam
TILE_BOX_SAMPLES = 9            # grade de amostras usada para achar o retângulo de tiles
MIN_SLEEP_S = 1.0
ECHO_OLD_S = 30 * 60            # quadro mais novo mais velho que isso: RainViewer ou rede parou

# chave de um tile no cache: (path do quadro, zoom, x do tile, y do tile)
TileKey = tuple[str, int, int, int]


# ------------------------------------------------------------- geometria
def inverse_azeq(e: np.ndarray, n: np.ndarray, lat0: float, lon0: float
                 ) -> tuple[np.ndarray, np.ndarray]:
    """
    Inversa de projection.AzimuthalEquidistant: km da estação -> (lat, lon).

    Funciona em arrays inteiros de uma vez (numpy). Fórmulas de Snyder:
    rho = distância ao centro; c = rho / R é o ângulo correspondente no
    centro da Terra; daí saem lat e lon.

    A longitude NÃO é reduzida a [-180, 180): perto do antimeridiano ela
    pode passar de 180, de propósito, para o mosaico de tiles continuar
    contínuo (ver to_world_px).

    Parâmetros:
        e, n: arrays de km a leste / ao norte da estação.
        lat0, lon0: estação, em graus.
    Retorno:
        (lat, lon) em graus, arrays do mesmo formato.
    """
    rho = np.hypot(e, n)
    angle = rho / R_EARTH_KM
    sin_angle = np.sin(angle)
    cos_angle = np.cos(angle)
    center_lat_rad = math.radians(lat0)
    sin_center = math.sin(center_lat_rad)
    cos_center = math.cos(center_lat_rad)
    # n*sin(c)/rho, com 0 no próprio centro (rho = 0) em vez de divisão por zero
    north_ratio = np.divide(n * sin_angle, rho, out=np.zeros_like(rho), where=rho > 0)
    sin_lat = np.clip(cos_angle * sin_center + north_ratio * cos_center, -1, 1)
    lat = np.degrees(np.arcsin(sin_lat))
    lon = lon0 + np.degrees(np.arctan2(e * sin_angle,
                                       rho * cos_center * cos_angle - n * sin_center * sin_angle))
    return lat, lon


def to_world_px(lat, lon, z: int):
    """
    lat/lon -> pixel no "mundo" Web Mercator do zoom z (tiles de 512 px).

    No zoom z o mundo tem 2^z x 2^z tiles. A longitude não é reduzida a
    [-180, 180): perto do antimeridiano os índices de tile saem do intervalo
    e só são reduzidos na hora de baixar (EchoFetcher._download).

    Retorno:
        (x, y) em px, mesmo formato da entrada.
    """
    world_size_px = TILE * (1 << z)
    lat = np.clip(lat, -MERC_LAT_MAX, MERC_LAT_MAX)
    x = (np.asarray(lon) + 180.0) / 360.0 * world_size_px
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0 * world_size_px
    return x, y


@dataclass(frozen=True)
class TileBox:
    """
    Retângulo de tiles no zoom z: colunas x0..x1 e linhas y0..y1 (inclusive).
    """
    z: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def count(self) -> int:
        """Número de tiles do retângulo."""
        return (self.x1 - self.x0 + 1) * (self.y1 - self.y0 + 1)


def tiles_for(lat0: float, lon0: float, range_km: float) -> TileBox:
    """
    Retângulo de tiles que cobre o escopo, no maior zoom possível.

    Fluxo: amostra uma grade de pontos sobre o quadrado do escopo, converte
    para lat/lon e tenta do zoom 7 para baixo até o retângulo caber em
    MAX_TILES tiles.

    Retorno:
        o TileBox escolhido (no pior caso, o do zoom 0).
    """
    samples_km = np.linspace(-range_km, range_km, TILE_BOX_SAMPLES)
    e, n = np.meshgrid(samples_km, samples_km)
    lat, lon = inverse_azeq(e, n, lat0, lon0)
    box = None
    for z in range(MAX_ZOOM, -1, -1):
        x, y = to_world_px(lat, lon, z)
        last_row = (1 << z) - 1
        box = TileBox(z,
                      int(x.min() // TILE),
                      max(int(y.min() // TILE), 0),
                      int(x.max() // TILE),
                      min(int(y.max() // TILE), last_row))
        if box.count <= MAX_TILES:
            return box
    return box


class Reprojector:
    """
    Mapeamento "pixel do escopo -> pixel do mosaico" para um alcance e retângulo.

    A parte cara (projeção inversa + Mercator em side x side pixels) é feita
    uma vez no construtor e vale para todos os quadros da animação; `apply`
    é só um "gather" do numpy (copiar pixels por índice). Não volte a
    reprojetar do zero a cada quadro: no Pi isso custa segundos.

    Parâmetros do construtor:
        box: retângulo de tiles do mosaico.
        lat0, lon0: estação.
        range_km: alcance do escopo (raio).
        side: lado da imagem do escopo, px.
        r_px: raio do escopo, px.
    """

    def __init__(self, box: TileBox, lat0: float, lon0: float, range_km: float,
                 side: int, r_px: int):
        self.box = box
        self.side = side
        mosaic_height = (box.y1 - box.y0 + 1) * TILE
        mosaic_width = (box.x1 - box.x0 + 1) * TILE
        self.shape = (mosaic_height, mosaic_width)

        # km de cada coluna/linha do escopo, medidos do centro do pixel
        km_per_px = range_km / r_px
        offsets_km = (np.arange(side) - side / 2 + 0.5) * km_per_px
        e, n = np.meshgrid(offsets_km, -offsets_km)     # linha 0 = norte
        lat, lon = inverse_azeq(e, n, lat0, lon0)
        x, y = to_world_px(lat, lon, box.z)
        mosaic_x = np.floor(x - box.x0 * TILE).astype(np.int64)
        mosaic_y = np.floor(y - box.y0 * TILE).astype(np.int64)

        inside_mosaic = ((mosaic_x >= 0) & (mosaic_x < mosaic_width)
                         & (mosaic_y >= 0) & (mosaic_y < mosaic_height))
        inside_circle = e * e + n * n <= range_km ** 2
        # índices "achatados" (1D): destino no escopo, origem no mosaico
        self.dst = np.flatnonzero(inside_mosaic & inside_circle)
        self.src = (mosaic_y * mosaic_width + mosaic_x).ravel()[self.dst]

    def apply(self, mosaic: np.ndarray, opacity: float) -> np.ndarray:
        """Monta a imagem do escopo a partir de um mosaico.

        Parâmetros:
            mosaic: RGBA (linhas, colunas, 4) no formato `self.shape`.
            opacity: 0..1, multiplica o alfa.
        Retorno:
            RGBA (side, side, 4), transparente onde não há eco.
        """
        output = np.zeros((self.side * self.side, 4), np.uint8)
        output[self.dst] = mosaic.reshape(-1, 4)[self.src]
        alpha = output[:, 3].astype(np.float32) * opacity
        output[:, 3] = alpha.astype(np.uint8)
        return output.reshape(self.side, self.side, 4)


def reproject(mosaic: np.ndarray, box: TileBox, lat0: float, lon0: float,
              range_km: float, side: int, r_px: int, opacity: float) -> np.ndarray:
    """Atalho de uma vez só (usado nos testes): Reprojector(...).apply(...)."""
    return Reprojector(box, lat0, lon0, range_km, side, r_px).apply(mosaic, opacity)


def pick_frame(frames: list[EchoFrame], now: float, step_s: float,
               hold_s: float) -> EchoFrame | None:
    """Quadro da animação a mostrar agora.

    A animação vai do mais velho ao mais novo, `step_s` segundos por quadro,
    e para `hold_s` segundos no mais novo (o "agora") antes de recomeçar.

    Parâmetros:
        frames: quadros do mais velho ao mais novo.
        now: instante atual (s).
    Retorno:
        o quadro, ou None se não há quadros.
    """
    if not frames:
        return None
    count = len(frames)
    if count == 1 or step_s <= 0:
        return frames[-1]
    cycle_s = (count - 1) * step_s + hold_s
    time_in_cycle = now % cycle_s
    index = min(int(time_in_cycle / step_s), count - 1)
    return frames[index]


def frame_age_s(frame_time: int, wall_now: float) -> float | None:
    """Idade de um quadro de eco, em segundos, pelo relógio de parede.

    Parâmetros:
        frame_time: epoch (s) do quadro, informado pela RainViewer.
        wall_now: time.time() atual.

    Retorno:
        A idade, ou None se o relógio local está antes do quadro: acontece no
        Pi sem RTC até o NTP acertar a hora, e aí a idade não tem sentido.
    """
    age_s = wall_now - frame_time
    if age_s < 0:
        return None
    return age_s


def age_text(age_s: float) -> str:
    """Idade curta para a tela: "há 14 min", a partir de 1 h "há 1h05" e a
    partir de um dia "há >1 dia" (o texto tem de caber no botão CHUVA)."""
    minutes = int(age_s // 60)
    if minutes < 60:
        return f"há {minutes} min"
    if minutes < 24 * 60:
        return f"há {minutes // 60}h{minutes % 60:02d}"
    return "há >1 dia"


# ------------------------------------------------------------------ rede
# Decodificador de PNG: bytes -> array RGBA (altura, largura, 4); SourceError
# se inválido. O núcleo não importa pygame, então quem cria o EchoFetcher
# fornece um (a tela usa `radClock.ui.echoview.decode_png`).
PngDecoder = Callable[[bytes], np.ndarray]


def _clock_text(epoch: int) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


@dataclass(frozen=True)
class EchoFrame:
    """Um quadro de eco pronto para a tela.

    image: array RGBA (side, side, 4), transparente fora da chuva. A tela o
        converte em Surface uma vez por quadro (ver ui/echoview.py).
    range_km: alcance para o qual foi reprojetado (outro alcance = não usar).
    time: epoch (s) do quadro de radar.
    version: muda a cada quadro publicado (chave de cache da tela).
    """
    image: np.ndarray
    range_km: float
    time: int
    version: int


class EchoFetcher(threading.Thread):
    """Thread que baixa e reprojeta os quadros de chuva.

    Uso pela tela: `request(alcance)` a cada quadro (None = pausar), e
    `frames()` / `error` para desenhar.

    Ciclo da thread (run):
      1. dorme até `request()` mudar o alcance ou vencer `refresh_s`;
      2. renova a lista de quadros da RainViewer (metadados);
      3. monta os quadros que faltam para o alcance pedido;
      4. em erro, registra e tenta de novo em RETRY_S.

    Parâmetros do construtor:
        cfg: seção [echo].
        lat0, lon0: estação.
        side, r_px: lado da imagem e raio do escopo, px.
        decode_png: bytes PNG -> array RGBA (ver PngDecoder).
        get, get_json: funções HTTP (injetáveis para testes).
    """

    def __init__(self, cfg: EchoCfg, lat0: float, lon0: float, side: int, r_px: int,
                 decode_png: PngDecoder, get=http_get, get_json=http_json):
        super().__init__(name="echo", daemon=True)
        self.cfg = cfg
        self.lat0 = lat0
        self.lon0 = lon0
        self.side = side
        self.r_px = r_px
        self._decode_png = decode_png
        self._http_get = get
        self._http_get_json = get_json

        # compartilhado com a thread da tela
        self._lock = threading.Lock()
        self._frames: list[EchoFrame] = []
        self._error = ""
        self._requested_range_km: float | None = None     # None = pausado
        self._wake = threading.Event()
        self._stop_event = threading.Event()

        # estado da thread (o cache de PNG também é usado pelos downloads)
        self._tile_host = ""
        self._frame_paths: list[tuple[int, str]] = []       # (epoch, path), mais novo primeiro
        self._metadata_at = -math.inf
        self._tile_png_cache: dict[TileKey, bytes] = {}
        self._tile_cache_lock = threading.Lock()
        self._pending_downloads: dict[TileKey, Future] = {}
        self._built_frames: dict[str, EchoFrame] = {}       # path -> quadro de _built_range_km
        self._built_range_km: float | None = None
        self._reprojector: Reprojector | None = None
        self._version = 0
        self._limiter = RateLimiter(REQ_PER_MIN, REQ_BURST)
        self._pool = ThreadPoolExecutor(DOWNLOAD_WORKERS, thread_name_prefix="echo-dl")

    # ---------------------------------------------- chamado pela thread da tela
    def request(self, range_km: float | None) -> None:
        """Pede quadros para `range_km`; None pausa os downloads (eco desligado)."""
        if range_km == self._requested_range_km:
            return
        self._requested_range_km = range_km
        self._wake.set()

    def frames(self) -> list[EchoFrame]:
        """Quadros prontos, do mais velho ao mais novo (podem ser de outro alcance)."""
        with self._lock:
            return self._frames

    @property
    def error(self) -> str:
        """Último erro (vazio = tudo certo)."""
        with self._lock:
            return self._error

    def stop(self) -> None:
        """Encerra a thread e cancela downloads pendentes."""
        self._stop_event.set()
        self._wake.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------- metadados
    def _refresh_metadata(self) -> None:
        """Baixa a lista de quadros da RainViewer e guarda os `cfg.frames` mais novos.

        Também descarta do cache os tiles de quadros que saíram da janela.
        """
        if not self._limiter.acquire(self._stop_event):
            return
        document = self._http_get_json(self.cfg.url)
        past = (document.get("radar") or {}).get("past") or []
        past = [item for item in past if isinstance(item, dict) and item.get("path")]
        if not document.get("host") or not past:
            raise SourceError("metadados da RainViewer sem quadros")
        self._metadata_at = time.monotonic()

        newest_last = [(int(item.get("time") or 0), item["path"])
                       for item in past[-self.cfg.frames:]]
        paths = list(reversed(newest_last))                 # mais novo primeiro
        if paths == self._frame_paths:
            return
        if not self._frame_paths or paths[0] != self._frame_paths[0]:
            log.info("RainViewer: quadro novo %s", _clock_text(paths[0][0]))
        self._tile_host = document["host"]
        self._frame_paths = paths
        current = {path for _, path in paths}
        with self._tile_cache_lock:
            self._tile_png_cache = {key: png for key, png in self._tile_png_cache.items()
                                    if key[0] in current}

    # ------------------------------------------------------------- tiles
    def _download(self, key: TileKey) -> None:
        """Baixa um tile (respeitando o limitador) e guarda no cache. Roda no pool."""
        path, z, tile_x, tile_y = key
        if not self._limiter.acquire(self._stop_event):
            raise SourceError("parado")
        # o x só é reduzido ao mundo [0, 2^z) aqui (antimeridiano)
        wrapped_x = tile_x % (1 << z)
        url = (f"{self._tile_host}{path}/{TILE}/{z}/{wrapped_x}/{tile_y}/"
               f"{self.cfg.color_scheme}/1_1.png")
        data = self._http_get(url)
        with self._tile_cache_lock:
            self._tile_png_cache[key] = data

    def _has_tile(self, key: TileKey) -> bool:
        with self._tile_cache_lock:
            return key in self._tile_png_cache

    def _trim_cache(self, box: TileBox) -> None:
        """Se o cache passou de TILE_CACHE_BYTES, mantém só os tiles do retângulo atual."""
        with self._tile_cache_lock:
            total_bytes = sum(len(png) for png in self._tile_png_cache.values())
            if total_bytes <= TILE_CACHE_BYTES:
                return
            self._tile_png_cache = {key: png for key, png in self._tile_png_cache.items()
                                    if _tile_in_box(key, box)}

    @staticmethod
    def _keys(path: str, box: TileBox) -> list[TileKey]:
        """Chaves de todos os tiles de um quadro no retângulo, linha por linha."""
        keys = []
        for tile_y in range(box.y0, box.y1 + 1):
            for tile_x in range(box.x0, box.x1 + 1):
                keys.append((path, box.z, tile_x, tile_y))
        return keys

    def _prefetch(self, paths: list[str], box: TileBox) -> None:
        """Pede ao pool todos os tiles que faltam, na ordem de `paths` (mais novo primeiro)."""
        self._pending_downloads = {key: future for key, future in self._pending_downloads.items()
                                   if not future.done()}
        for path in paths:
            for key in self._keys(path, box):
                if self._has_tile(key) or key in self._pending_downloads:
                    continue
                self._pending_downloads[key] = self._pool.submit(self._download, key)

    def _wait_for_tiles(self, keys: list[TileKey]) -> None:
        """Garante que os tiles estão no cache; repassa o erro de um download."""
        for key in keys:
            if self._has_tile(key):
                continue
            future = self._pending_downloads.pop(key, None)
            if future is None:
                future = self._pool.submit(self._download, key)
            future.result()

    def _build_mosaic(self, path: str, box: TileBox) -> np.ndarray:
        """Junta os tiles de um quadro numa imagem RGBA única (o mosaico)."""
        keys = self._keys(path, box)
        self._wait_for_tiles(keys)
        mosaic = np.zeros((*self._reprojector.shape, 4), np.uint8)
        for key in keys:
            _, z, tile_x, tile_y = key
            with self._tile_cache_lock:
                png = self._tile_png_cache[key]
            tile = self._decode_png(png)
            if tile.shape[:2] != (TILE, TILE):
                raise SourceError(f"tile {z}/{tile_x}/{tile_y} com tamanho "
                                  f"{tile.shape[1]}x{tile.shape[0]}")
            top = (tile_y - box.y0) * TILE
            left = (tile_x - box.x0) * TILE
            mosaic[top:top + TILE, left:left + TILE] = tile
        return mosaic

    # ------------------------------------------------------------ quadros
    def _publish(self) -> None:
        """Entrega à tela os quadros montados, do mais velho ao mais novo."""
        frames = sorted(self._built_frames.values(), key=lambda frame: frame.time)
        with self._lock:
            self._frames = frames

    def _start_range(self, range_km: float, box: TileBox) -> None:
        """Alcance novo: descarta os quadros montados e refaz o Reprojector."""
        self._built_frames = {}
        self._built_range_km = range_km
        self._reprojector = Reprojector(box, self.lat0, self.lon0, range_km,
                                        self.side, self.r_px)

    def _drop_old_frames(self) -> None:
        """Remove os quadros que saíram da janela da RainViewer."""
        current = {path for _, path in self._frame_paths}
        if all(path in current for path in self._built_frames):
            return
        self._built_frames = {path: frame for path, frame in self._built_frames.items()
                              if path in current}
        self._publish()

    def _build_frames(self, range_km: float) -> None:
        """Monta os quadros que faltam para `range_km`, do mais novo ao mais velho.

        Volta no meio (sem erro) se o alcance pedido mudar ou a thread parar:
        o laço de `run` recomeça com o alcance novo.
        """
        box = tiles_for(self.lat0, self.lon0, range_km)
        if self._built_range_km != range_km:
            self._start_range(range_km, box)
        self._drop_old_frames()

        missing = [path for _, path in self._frame_paths if path not in self._built_frames]
        self._prefetch(missing, box)
        for epoch, path in self._frame_paths:
            if path in self._built_frames:
                continue
            if self._requested_range_km != range_km or self._stop_event.is_set():
                return
            self._build_one_frame(epoch, path, range_km, box)

    def _build_one_frame(self, epoch: int, path: str, range_km: float, box: TileBox) -> None:
        """Mosaico -> reprojeção -> imagem RGBA; publica e poda o cache."""
        started = time.monotonic()
        image = self._reprojector.apply(self._build_mosaic(path, box), self.cfg.opacity)
        self._version += 1
        self._built_frames[path] = EchoFrame(image, range_km, epoch, self._version)
        self._publish()
        self._trim_cache(box)
        log.debug("eco: quadro %s z%d %d tiles, %.0f km, %.2f s", _clock_text(epoch),
                  box.z, box.count, range_km, time.monotonic() - started)

    # -------------------------------------------------------------- laço
    def _set_error(self, text: str) -> None:
        with self._lock:
            self._error = text

    def _work_once(self, range_km: float) -> float:
        """Uma volta de trabalho. Retorno: quantos segundos dormir depois."""
        try:
            if time.monotonic() - self._metadata_at >= self.cfg.refresh_s:
                self._refresh_metadata()
            self._build_frames(range_km)
        except SourceError as e:
            log.warning("eco: %s", e)
            self._set_error(str(e))
            return RETRY_S
        except Exception:               # noqa: BLE001 -- a thread não pode morrer
            log.exception("eco: erro interno")
            self._set_error("erro interno")
            return RETRY_S
        self._set_error("")
        until_refresh_s = self.cfg.refresh_s - (time.monotonic() - self._metadata_at)
        return max(MIN_SLEEP_S, until_refresh_s)

    def run(self) -> None:
        """Laço da thread: trabalha uma vez e dorme até o próximo ciclo ou um pedido novo."""
        while not self._stop_event.is_set():
            self._wake.clear()
            range_km = self._requested_range_km
            if range_km is None:
                self._wake.wait()               # pausado: dorme até request()
            else:
                self._wake.wait(self._work_once(range_km))


def _tile_in_box(key: TileKey, box: TileBox) -> bool:
    _, z, tile_x, tile_y = key
    return z == box.z and box.x0 <= tile_x <= box.x1 and box.y0 <= tile_y <= box.y1
