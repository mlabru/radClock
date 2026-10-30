#!/usr/bin/env bash
# Orquestração do radClock: desenvolvimento no PC, dados, instalação e
# operação no Raspberry Pi. Rode sem argumentos para ver os comandos.
#
# No PC usa o .venv do projeto (ou python3) com PYTHONPATH na raiz, então
# funciona mesmo sem `pip install -e .`. No Pi, `install` copia o projeto para
# /opt/radClock, cria o venv de lá e instala o serviço systemd.
#
# DRY_RUN=1 mostra os comandos de install/deploy sem executar nada.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/radClock}"            # instalação no Pi
ETC="${ETC:-/etc/radClock}"
SERVICE=radclock                             # nome da unidade systemd
REMOTE_SRC=/tmp/radClock-src                 # cópia temporária no Pi (deploy)
FAKE_JSON=/tmp/radClock/aircraft.json        # saída do tools/fake_readsb.py

# o que não vai para o Pi: venv local, caches de geração, git, bytecode
RSYNC_EXCLUDES=(--exclude .venv --exclude .git --exclude tools/cache
                --exclude __pycache__ --exclude '*.pyc' --exclude .pytest_cache
                --exclude config.toml --exclude build --exclude '*.egg-info')

# ---------------------------------------------------------------- utilitários
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }

run() {                                     # executa, ou só mostra com DRY_RUN=1
    if [[ "${DRY_RUN:-0}" == 1 ]]; then
        printf '   [dry-run] %q' "$1"; shift; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

pybin() {                                   # python do projeto: .venv se houver
    if [[ -x "$ROOT/.venv/bin/python" ]]; then echo "$ROOT/.venv/bin/python"; else echo python3; fi
}

py() {                                      # PYTHONPATH na raiz: acha o pacote sem pip install
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$(pybin)" "$@"
}

need_cmd() {                                # Pi OS Lite recém-instalado pode não ter
    command -v "$1" >/dev/null 2>&1 || die "falta '$1': sudo apt install ${2:-$1}"
}

need_root() { [[ $EUID -eq 0 || "${DRY_RUN:-0}" == 1 ]] || die "'$1' precisa de root: sudo $0 $1"; }

usage() {
    cat <<EOF
uso: $0 <comando> [opções]

Desenvolvimento (PC)
  setup                 cria/atualiza o .venv e instala dependências (dev + Pillow)
  test [args pytest]    roda os testes
  dev                   tráfego simulado (fake_readsb) + app em janela (config.dev.toml)
  api                   tráfego real (adsb.lol) + chuva, sem receptor (config.api.toml)
  smoke [segundos] [config]
                        sobe o app sem tela com tráfego simulado (padrão 8 s,
                        config.dev.toml) e falha se ele cair ou registrar traceback

Dados
  data [-c config] [--no-db] [--no-maps]
                        gera data/aircraft.db (silhuetas SVG 512 px) e maps/ para a
                        estação e o maior alcance do config (padrão config.dev.toml)

Raspberry Pi
  install [--24x7]      (no Pi, root) instala/atualiza em $PREFIX, o config em
                        $ETC (só se não existir) e o serviço; --24x7 instala
                        também o watchdog de hardware e o limite do log
  deploy <ssh-host> [--24x7]
                        copia o projeto para o Pi por rsync e roda o install lá
  status [ssh-host]     estado do serviço (local ou no Pi)
  logs [ssh-host]       acompanha o log do serviço (local ou no Pi)
  restart [ssh-host]    reinicia o serviço

Variáveis: DRY_RUN=1 (só mostra install/deploy), PREFIX, ETC.
EOF
}

# --------------------------------------------------------------- desenvolvimento
cmd_setup() {
    if [[ ! -e "$ROOT/.venv" ]]; then
        say "criando .venv"
        python3 -m venv "$ROOT/.venv"
    fi
    say "instalando radClock (editável) + pytest + Pillow no .venv"
    "$ROOT/.venv/bin/pip" install -e "$ROOT[dev]" pillow
}

cmd_test() {
    say "pytest $*"
    (cd "$ROOT" && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy py -m pytest -q "$@")
}

FAKE_PID=""
start_fake() {
    say "tráfego simulado -> $FAKE_JSON"
    # exec (e não a função py): com py & o $! é o PID de um subshell e o
    # stop_fake mataria só ele, deixando o fake_readsb órfão
    (export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
     exec "$(pybin)" "$ROOT/tools/fake_readsb.py") >/dev/null 2>&1 &
    FAKE_PID=$!
    trap 'stop_fake' EXIT
    for _ in $(seq 50); do                  # até 5 s pelo primeiro arquivo
        [[ -s "$FAKE_JSON" ]] && return 0
        kill -0 "$FAKE_PID" 2>/dev/null || die "fake_readsb terminou na partida"
        sleep 0.1
    done
    die "fake_readsb não gravou $FAKE_JSON"
}
stop_fake() {
    [[ -n "$FAKE_PID" ]] && kill "$FAKE_PID" 2>/dev/null || true
    FAKE_PID=""
}

cmd_dev() {
    start_fake
    say "radClock -c config.dev.toml -w ($*)"
    (cd "$ROOT" && py -m radClock -c config.dev.toml -w "$@")
}

cmd_api() {
    say "radClock -c config.api.toml -w ($*)"
    (cd "$ROOT" && py -m radClock -c config.api.toml -w "$@")
}

cmd_smoke() {
    local secs="${1:-8}" cfg="${2:-$ROOT/config.dev.toml}" log tmp rc=0
    [[ "$secs" =~ ^[0-9]+$ ]] || die "smoke: segundos inválidos: $secs"
    [[ -f "$cfg" ]] || die "smoke: config não encontrado: $cfg"
    cfg="$(realpath "$cfg")"
    tmp="$(mktemp -d)"
    log="$tmp/smoke.log"
    start_fake
    say "app sem tela por ${secs}s (SDL dummy, ajustes em $tmp)"
    # SIGTERM no fim: o app sai limpo (SystemExit); 124 = o timeout é que parou,
    # ou seja, o app ainda estava de pé
    (cd "$ROOT" && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy XDG_STATE_HOME="$tmp" \
        PYTHONPATH="$ROOT" timeout -s TERM "$secs" "$(pybin)" -m radClock -c "$cfg" -v \
        >"$log" 2>&1) || rc=$?
    if [[ $rc -ne 124 && $rc -ne 0 ]]; then
        tail -20 "$log" >&2
        die "o app saiu antes do tempo (código $rc); log em $log"
    fi
    if grep -q "Traceback" "$log"; then
        grep -A15 "Traceback" "$log" >&2
        die "traceback no log: $log"
    fi
    grep -E "mapa |base de aeronaves|quadro:" "$log" | sed 's/^/   /' || true
    say "smoke ok ($(grep -c . "$log") linhas de log, sem erro)"
    rm -rf "$tmp"
}

# ------------------------------------------------------------------------ dados
cmd_data() {
    local cfg="$ROOT/config.dev.toml" db=1 maps=1
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c) cfg="$(realpath "$2")"; shift 2 ;;
            --no-db) db=0; shift ;;
            --no-maps) maps=0; shift ;;
            *) die "data: opção desconhecida $1" ;;
        esac
    done
    [[ -f "$cfg" ]] || die "config não encontrado: $cfg"
    # estação, maior alcance e regra de TLS saem do próprio config (validado pelo app)
    local lat lon radius strict tls=()
    read -r lat lon radius strict < <(py -c "
import sys
from pathlib import Path
from radClock.core import config
c = config.load(Path(sys.argv[1]))
print(c.station.lat, c.station.lon, max(c.radar.ranges_km), int(c.network.strict_tls))" "$cfg")
    [[ "$strict" == 0 ]] && tls=(--no-strict-tls)       # [network] strict_tls = false
    if [[ $db == 1 ]]; then
        py -c "import PIL" 2>/dev/null || die "Pillow ausente: $0 setup (ou pip install pillow)"
        say "silhuetas próprias (747, 767, ERJ-145, Brasília, Bandeirante)"
        py "$ROOT/tools/make_local_silhouettes.py"
        say "data/aircraft.db (SVG do pw-silhouettes a 512 px; precisa de internet)"
        py "$ROOT/tools/make_aircraft_db.py" --sqlite --svg --size 512 --out "$ROOT/data/aircraft.db" \
            "${tls[@]}" | grep -v "cache hit" || die "make_aircraft_db falhou"
    fi
    if [[ $maps == 1 ]]; then
        say "maps/ para $lat,$lon, raio ${radius} km (Natural Earth + OurAirports)"
        (cd "$ROOT" && py tools/make_map.py --lat "$lat" --lon "$lon" --radius "$radius" \
            --states --rivers --geojson-dir maps "${tls[@]}" | grep -v "cache hit")
        warn "confira maps/layers.toml e copie os [[map.layers]] para o seu config"
    fi
}

# -------------------------------------------------------------- Raspberry Pi
cmd_install() {
    need_root install
    local full=0
    [[ "${1:-}" == "--24x7" ]] && full=1
    [[ -f "$ROOT/pyproject.toml" ]] || die "rode a partir da cópia do projeto"
    [[ -f "$ROOT/data/aircraft.db" ]] || warn "sem aircraft.db: a ficha fica sem tipo/silhueta (gere no PC com '$0 data')"
    need_cmd rsync
    need_cmd python3
    python3 -c "import ensurepip" 2>/dev/null || die "falta o módulo venv: sudo apt install python3-venv"

    # o serviço roda como um usuário que já existe no Pi; os grupos de
    # vídeo/toque/som vêm do SupplementaryGroups da unidade
    # (o User= da unidade é a única fonte do nome; troque lá se precisar)
    local app_user
    app_user="$(sed -n 's/^User=//p' "$ROOT/deploy/radclock.service")"
    id "$app_user" >/dev/null 2>&1 || die "usuário $app_user (User= em deploy/radclock.service) não existe no Pi"

    say "código em $PREFIX (o .venv de lá é preservado)"
    run mkdir -p "$PREFIX" "$ETC"
    run rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$PREFIX/"

    say "venv e dependências"
    [[ -x "$PREFIX/.venv/bin/python" ]] || run python3 -m venv "$PREFIX/.venv"
    run "$PREFIX/.venv/bin/pip" install --quiet --upgrade "$PREFIX"

    if [[ ! -f "$ETC/config.toml" ]]; then
        say "config inicial em $ETC/config.toml (ajuste [station] e [display])"
        run install -m644 "$ROOT/config.example.toml" "$ETC/config.toml"
    else
        say "config existente mantido: $ETC/config.toml"
    fi
    # config inválido faria o serviço reiniciar em loop: confere antes
    if [[ "${DRY_RUN:-0}" != 1 ]]; then
        (cd "$PREFIX" && "$PREFIX/.venv/bin/python" -c \
            "from pathlib import Path; from radClock.core import config; config.load(Path('$ETC/config.toml'))") \
            || die "$ETC/config.toml inválido; corrija e rode de novo"
    fi

    say "serviço systemd"
    run install -m644 "$ROOT/deploy/radclock.service" "/etc/systemd/system/$SERVICE.service"
    if [[ $full == 1 ]]; then
        say "24/7: watchdog de hardware e limite do log (vale após reiniciar o Pi)"
        run install -D -m644 "$ROOT/deploy/watchdog.conf" /etc/systemd/system.conf.d/watchdog.conf
        run install -D -m644 "$ROOT/deploy/journald.conf" /etc/systemd/journald.conf.d/radClock.conf
    fi
    run systemctl daemon-reload
    run systemctl enable "$SERVICE"
    run systemctl restart "$SERVICE"
    [[ "${DRY_RUN:-0}" == 1 ]] || { sleep 3; systemctl --no-pager status "$SERVICE" | head -12 || true; }
    [[ $full == 1 ]] && warn "reinicie o Pi (sudo reboot) para o watchdog de hardware valer"
    say "instalado. Log: $0 logs"
}

cmd_deploy() {
    local host="${1:-}"
    [[ -n "$host" ]] || die "deploy: informe o host ssh, ex.: $0 deploy pi@radClock.local"
    shift
    need_cmd rsync
    say "copiando para $host:$REMOTE_SRC (o Pi também precisa de rsync)"
    run rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$host:$REMOTE_SRC/"
    say "instalando no Pi"
    run ssh -t "$host" "sudo bash $REMOTE_SRC/radClock.sh install $*"
}

on_host() {                                 # comando local ou no Pi por ssh
    local host="$1"; shift
    if [[ -n "$host" ]]; then ssh -t "$host" "$*"; else bash -c "$*"; fi
}
cmd_status()  { on_host "${1:-}" "systemctl --no-pager status $SERVICE"; }
cmd_logs()    { on_host "${1:-}" "journalctl -u $SERVICE -f -n 50"; }
cmd_restart() { on_host "${1:-}" "sudo systemctl restart $SERVICE && systemctl --no-pager status $SERVICE | head -5"; }

# ----------------------------------------------------------------------- main
main() {
    local cmd="${1:-}"
    [[ $# -gt 0 ]] && shift
    case "$cmd" in
        setup)   cmd_setup "$@" ;;
        test)    cmd_test "$@" ;;
        dev)     cmd_dev "$@" ;;
        api)     cmd_api "$@" ;;
        smoke)   cmd_smoke "$@" ;;
        data)    cmd_data "$@" ;;
        install) cmd_install "$@" ;;
        deploy)  cmd_deploy "$@" ;;
        status)  cmd_status "$@" ;;
        logs)    cmd_logs "$@" ;;
        restart) cmd_restart "$@" ;;
        ""|-h|--help|help) usage ;;
        *) usage >&2; die "comando desconhecido: $cmd" ;;
    esac
}

main "$@"
