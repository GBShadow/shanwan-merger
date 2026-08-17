#!/usr/bin/env bash
# =============================================================================
# setup.sh - Configuracao automatica do ShanWan Android Gamepad (20bc:5501)
# no Linux (Debian/Ubuntu/MiniOS), via merger userspace.
#
# Uso:
#   sudo ./setup.sh            # instala tudo (idempotente - pode reexecutar)
#   sudo ./setup.sh --uninstall  # remove tudo (rollback)
#
# O que este script faz:
#   1. Instala a dependencia python3-evdev
#   2. Cria a unit systemd usbcore-shanwan-quirk.service
#      (usbcore.quirks=2563:0575:r - evita o fallback de config descriptor)
#   3. Cria a unit systemd shanwan-merger.service
#      (daemon merger.py que une os evdev em joystick virtual)
#   4. Cria a regra udev 98-shanwan-hide-physical.rules
#      (esconde o js1 fisico do SDL/Steam - auxiliar; o grab no merger
#       e o que resolve de fato a duplicacao)
#   5. Ativa e inicia tudo
# =============================================================================
set -euo pipefail

# --- caminhos ---------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER_PY="$SCRIPT_DIR/merger.py"
SYSTEMD_DIR="/etc/systemd/system"
UDEV_DIR="/etc/udev/rules.d"
MERGER_UNIT="$SYSTEMD_DIR/shanwan-merger.service"
QUIRK_UNIT="$SYSTEMD_DIR/usbcore-shanwan-quirk.service"
UDEV_RULE="$UDEV_DIR/98-shanwan-hide-physical.rules"

VID="20bc"
PID="5501"

# --- helpers ----------------------------------------------------------------
say()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[setup]\033[0m ERRO: %s\n' "$*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "execute com sudo: sudo $0"
    fi
}

require_merger_py() {
    [[ -f "$MERGER_PY" ]] || die "nao encontrei merger.py em $MERGER_PY (rode o script da raiz do projeto)"
}

# --- deteccao de distro / gerenciador de pacotes ---------------------------------
# Suportadas: Debian/Ubuntu/MiniOS (apt), Fedora (dnf), Arch/Manjaro (pacman).
PKG_MGR=""
EVDEV_PKG=""

detect_distro() {
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MGR="apt"
        EVDEV_PKG="python3-evdev"
        say "distro detectada: Debian/Ubuntu (apt-get)"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MGR="dnf"
        EVDEV_PKG="python3-evdev"
        say "distro detectada: Fedora (dnf)"
    elif command -v pacman >/dev/null 2>&1; then
        PKG_MGR="pacman"
        EVDEV_PKG="python-evdev"
        say "distro detectada: Arch/Manjaro (pacman)"
    else
        die "gerenciador de pacotes nao suportado (apt/dnf/pacman). Instale manualmente o pacote 'python-evdev' e rode de novo."
    fi
}

install_evdev() {
    case "$PKG_MGR" in
        apt)
            apt-get update -y >/dev/null
            DEBIAN_FRONTEND=noninteractive apt-get install -y "$EVDEV_PKG" >/dev/null
            ;;
        dnf)
            dnf install -y "$EVDEV_PKG" >/dev/null
            ;;
        pacman)
            pacman -Sy --noconfirm "$EVDEV_PKG" >/dev/null
            ;;
    esac
}

check_system() {
    # verifica que o kernel tem uinput
    if [[ ! -e /dev/uinput ]] && ! modprobe uinput 2>/dev/null; then
        warn "/dev/uinput indisponivel - merger nao conseguira criar o joystick virtual"
    fi
    # python3 + evdev
    if ! command -v python3 >/dev/null 2>&1; then
        die "python3 nao instalado. Instale primeiro: apt/dnf/pacman install python3"
    fi
    if ! python3 -c "import evdev" 2>/dev/null; then
        say "instalando $EVDEV_PKG (dependencia do merger)..."
        install_evdev
        python3 -c "import evdev" || die "falhou em instalar $EVDEV_PKG via $PKG_MGR - instale manualmente e rode de novo"
    fi
}

# --- arquivos ---------------------------------------------------------------
write_quirk_unit() {
    cat > "$QUIRK_UNIT" <<EOF
[Unit]
Description=Apply usbcore quirk for ShanWan 2563:0575 gamepad (avoid config-desc fallback)
DefaultDependencies=no
Before=usb.target
After=systemd-modules-load.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo "2563:0575:r" > /sys/module/usbcore/parameters/quirks'
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
EOF
    say "criada $QUIRK_UNIT"
}

write_merger_unit() {
    cat > "$MERGER_UNIT" <<EOF
[Unit]
Description=ShanWan Android-mode event merger (unified uinput gamepad)
DefaultDependencies=no
After=systemd-udev-trigger.service usbcore-shanwan-quirk.service
Wants=usbcore-shanwan-quirk.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$MERGER_PY"
Restart=always
RestartSec=2
TimeoutStopSec=3
KillSignal=SIGTERM

[Install]
WantedBy=default.target
EOF
    say "criada $MERGER_UNIT (executa $MERGER_PY)"
}

write_udev_rule() {
    cat > "$UDEV_RULE" <<EOF
# Esconde o joystick FISICO do ShanWan (js1) do SDL/Steam.
# Nota: so isso NAO basta - a Steam le via evdev direto; o grab exclusivo
# feito pelo merger.py no no evdev fisico e o que resolve a duplicacao.
ACTION=="remove", GOTO="shanwan_hide_end"
SUBSYSTEM=="input", KERNEL=="event*|js*", \
    SUBSYSTEMS=="usb", ATTRS{idVendor}=="$VID", ATTRS{idProduct}=="$PID", \
    ENV{ID_INPUT_JOYSTICK}="", \
    ENV{ID_INPUT_KEY}="", \
    ENV{ID_INPUT_KEYBOARD}=""
LABEL="shanwan_hide_end"
EOF
    say "criada $UDEV_RULE"
}

# --- acoes ------------------------------------------------------------------
do_install() {
    require_root
    require_merger_py
    detect_distro
    check_system

    say "=============================================="
    say " Instalando configuracao ShanWan (20bc:$PID)"
    say "=============================================="

    write_quirk_unit
    write_merger_unit
    write_udev_rule

    # quirk imediatamente (nao espera reboot)
    say "aplicando quirk usbcore agora..."
    echo "2563:0575:r" > /sys/module/usbcore/parameters/quirks
    say "quirk atual: $(cat /sys/module/usbcore/parameters/quirks)"

    # recarregar regras udev e re-aplicar nos dispositivos ja plugados
    say "recarregando regras udev..."
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=input

    # habilitar e iniciar
    systemctl daemon-reload
    systemctl enable --now usbcore-shanwan-quirk.service >/dev/null 2>&1
    systemctl enable --now shanwan-merger.service >/dev/null 2>&1
    systemctl restart shanwan-merger.service >/dev/null 2>&1

    # pequena espera para o daemon subir
    sleep 3

    say "=============================================="
    say " VERIFICACAO"
    say "=============================================="
    systemctl is-active usbcore-shanwan-quirk.service || warn "quirk unit inativa"
    systemctl is-active shanwan-merger.service || warn "merger unit inativa"
    echo
    if pgrep -f "merger.py" >/dev/null; then
        say "merger rodando (PID $(pgrep -f merger.py | head -1))"
        echo
        say "Nos criados:"
        for p in /sys/class/input/js*/device; do
            n="$(basename "$(dirname "$p")")"
            nm="$(cat "$p/name" 2>/dev/null)"
            [[ "$nm" == SHANWAN* ]] && echo "   /dev/input/$n -> $nm"
        done
        echo
        say "Para testar:  jstest-gtk  (selecione /dev/input/js2, o 'merged')"
        say "               sudo evtest <node-merged>"
        say "Reabra a Steam (feche 100%, incl. tray) para re-enumerar."
    else
        warn "merger NAO esta rodando. Veja o log:"
        warn "   journalctl -u shanwan-merger.service -b -e"
        warn "Possiveis causas: controle nao conectado, python3-evdev faltando."
    fi
    echo
    say "Concluido. Documentacao completa: $SCRIPT_DIR/README.md"
}

do_uninstall() {
    require_root
    say "Removendo configuracao ShanWan..."

    systemctl disable --now shanwan-merger.service 2>/dev/null || true
    systemctl disable --now usbcore-shanwan-quirk.service 2>/dev/null || true
    rm -f "$MERGER_UNIT" "$QUIRK_UNIT" "$UDEV_RULE"
    systemctl daemon-reload

    # limpar quirk (opcional - volta ao comportamento original)
    if [[ -f /sys/module/usbcore/parameters/quirks ]]; then
        # remove apenas a entrada 2563:0575:r (o resto fica intacto)
        current="$(cat /sys/module/usbcore/parameters/quirks)"
        cleaned="$(echo "$current" | tr ',' '\n' | grep -v '^2563:0575:r$' | paste -sd, -)"
        echo "${cleaned%,}" > /sys/module/usbcore/parameters/quirks 2>/dev/null || true
        say "quirk usbcore restaurado para: $(cat /sys/module/usbcore/parameters/quirks)"
    fi

    udevadm control --reload-rules
    say "Removido. Reinicie a Steam se estiver aberta."
}

# --- main -------------------------------------------------------------------
case "${1:-install}" in
    install|-i)       do_install ;;
    uninstall|-u|-r)  do_uninstall ;;
    help|-h|--help)   sed -n '2,25p' "$0" ;;
    *) die "argumento desconhecido: $1 (use install|uninstall|help)" ;;
esac