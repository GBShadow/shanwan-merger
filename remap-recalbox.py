#!/usr/bin/env python3
"""Ferramenta interativa de remapeamento do ShanWan Gamepad — Recalbox/Batocera.

Versão STDLIB PURA (sem python-evdev) de `remap.py`, para rodar direto no
Recalbox via SSH, sem precisar de outra máquina Linux.

Uso (via SSH, como root):
    python3 remap-recalbox.py                # remapeia TODOS os papéis
    python3 remap-recalbox.py Y RB LT         # remapeia só os papéis citados
    python3 remap-recalbox.py --list          # mostra o mapeamento atual

Papéis válidos: A B X Y LB RB LT RT SELECT START MODE TURBO CLEAR

O script:
  1. Mata qualquer `recalbox-merger.py` em execução (best-effort — o loop
     de reinício em `custom.sh` vai tentar respawnar em ~2s, mas como este
     script já segura o EVIOCGRAB exclusivo dos nós físicos, o respawn
     falha silenciosamente em grab e fica inofensivo até este script sair);
  2. Abre e trava (grab exclusivo) os 3 nós físicos do ShanWan;
  3. Para cada papel pedido, aguarda você pressionar o botão físico
     correspondente;
  4. Atualiza `mapping.json` (mesma pasta deste script) incrementalmente;
  5. Libera os nós — o loop de `custom.sh` respawna o merger sozinho em
     até 2s, já lendo o `mapping.json` atualizado.

Não precisa reiniciar o Recalbox nem o serviço manualmente.
"""
import os
import sys
import json
import time
import struct
import fcntl
import select
import subprocess

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(SCRIPT_DIR, "mapping.json")

VID = 0x20BC
PID = 0x5501

EVIOCGRAB = 0x40044590
EV_SYN = 0
EV_KEY = 1

KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106
ARROW_CODES = {KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT}

EV_FMT  = 'llHHi' if struct.calcsize('l') == 8 else 'iiHHi'
EV_SIZE = struct.calcsize(EV_FMT)

DEFAULT_ORDER = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT",
                  "SELECT", "START", "MODE", "TURBO", "CLEAR"]
VALID_ROLES = set(DEFAULT_ORDER)

ROLE_EMOJI = {
    "A": "[A]", "B": "[B]", "X": "[X]", "Y": "[Y]",
    "LB": "[LB]", "RB": "[RB]", "LT": "[LT]", "RT": "[RT]",
    "SELECT": "[SELECT]", "START": "[START]", "MODE": "[MODE]",
    "TURBO": "[TURBO]", "CLEAR": "[CLEAR]",
}


def say(msg):
    print(f"[remap] {msg}")


def warn(msg):
    print(f"[remap] AVISO: {msg}")


def die(msg):
    print(f"[remap] ERRO: {msg}", file=sys.stderr)
    sys.exit(1)


def load_mapping():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            return json.load(f)
    return {}


def save_mapping(mapping):
    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=2)


def print_current(mapping):
    say("Mapeamento atual:")
    for role in DEFAULT_ORDER:
        e = mapping.get(role)
        if e:
            print(f"  {ROLE_EMOJI.get(role,''):10} {role:8} -> device={e['device']:9} code={e['code']}")
        else:
            print(f"  {ROLE_EMOJI.get(role,''):10} {role:8} -> (nao definido)")


def kill_running_merger():
    try:
        subprocess.run(["pkill", "-f", "recalbox-merger.py"])
        say("processo(s) recalbox-merger.py existente(s) finalizado(s)")
        time.sleep(0.5)
    except Exception as e:
        warn(f"nao consegui rodar pkill: {e}")


def find_devices():
    """Descobre os 3 nós físicos do ShanWan via /proc/bus/input/devices."""
    joystick = consumer = keyboard = None
    data = open('/proc/bus/input/devices').read()
    for block in data.split('\n\n'):
        if 'Vendor=%04x Product=%04x' % (VID, PID) not in block:
            continue
        name = ev_num = None
        has_abs = False
        for line in block.splitlines():
            if line.startswith('N: Name='):
                name = line.split('=', 1)[1].strip().strip('"')
            elif line.startswith('H: Handlers='):
                for tok in line.split('=', 1)[1].split():
                    if tok.startswith('event'):
                        ev_num = int(tok[5:])
            elif line.startswith('B: ABS='):
                has_abs = True
        if ev_num is None or name == "Xbox 360 Controller":
            continue
        path = '/dev/input/event%d' % ev_num
        if 'Consumer Control' in name:
            consumer = path
        elif 'Keyboard' in name:
            keyboard = path
        elif 'System Control' in name:
            continue
        elif has_abs:
            joystick = path
    return joystick, consumer, keyboard


def grab(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, EVIOCGRAB, 1)
    return fd


def read_ev(fd):
    try:
        raw = os.read(fd, EV_SIZE)
    except BlockingIOError:
        return None
    if len(raw) != EV_SIZE:
        return None
    _, _, t, c, v = struct.unpack(EV_FMT, raw)
    return t, c, v


def capture_role(role, fd_tag):
    say(f"{ROLE_EMOJI.get(role,'')} Pressione o botao fisico para: {role}")
    poll = select.poll()
    for fd in fd_tag:
        poll.register(fd, select.POLLIN)

    while True:
        events = poll.poll(60000)
        if not events:
            warn("nada pressionado em 60s - repetindo prompt...")
            continue
        for fd, _ in events:
            tag = fd_tag[fd]
            while True:
                ev = read_ev(fd)
                if ev is None:
                    break
                t, c, v = ev
                if t == EV_KEY and v == 1:
                    if c in ARROW_CODES:
                        warn("D-pad nao pode ser remapeado por aqui - pressione outro botao")
                        continue
                    say(f"   -> capturado: device={tag} code={c}")
                    return tag, c


def main():
    args = sys.argv[1:]

    if os.geteuid() != 0:
        die("execute como root (via SSH, ja e root no Recalbox por padrao)")

    mapping = load_mapping()

    if "--list" in args or "-l" in args:
        print_current(mapping)
        return

    if args:
        roles = [a.upper() for a in args]
        invalid = [r for r in roles if r not in VALID_ROLES]
        if invalid:
            die(f"papeis invalidos: {invalid}. Validos: {sorted(VALID_ROLES)}")
    else:
        roles = DEFAULT_ORDER

    say(f"Papeis a remapear ({len(roles)}): {', '.join(roles)}")
    kill_running_merger()

    joystick = consumer = keyboard = None
    for attempt in range(10):
        joystick, consumer, keyboard = find_devices()
        if joystick:
            break
        time.sleep(0.5)
    if not joystick:
        die("controle ShanWan nao encontrado - verifique se esta conectado")

    fds = {}
    fd_tag = {}
    try:
        fd = grab(joystick); fds["joystick"] = fd; fd_tag[fd] = "joystick"
        if consumer:
            fd = grab(consumer); fds["consumer"] = fd; fd_tag[fd] = "consumer"
        if keyboard:
            fd = grab(keyboard); fds["keyboard"] = fd; fd_tag[fd] = "keyboard"
    except OSError as e:
        die(f"falha ao travar dispositivo: {e}")

    try:
        say("=" * 60)
        say("Pressione cada botao UMA VEZ, na ordem pedida. Ctrl+C cancela.")
        say("=" * 60)

        for role in roles:
            tag, code = capture_role(role, fd_tag)
            mapping[role] = {"device": tag, "code": code}
            save_mapping(mapping)  # salva incrementalmente

        say("Todos os papeis remapeados com sucesso!")

    except KeyboardInterrupt:
        warn("cancelado pelo usuario - mapeamento parcial ja foi salvo")
    finally:
        for fd in fds.values():
            try:
                os.close(fd)
            except Exception:
                pass

    print()
    print_current(mapping)
    say("O loop do custom.sh vai respawnar o recalbox-merger.py em ate 2s")
    say("com o novo mapeamento. Nao e preciso reiniciar o Recalbox.")


if __name__ == "__main__":
    main()
