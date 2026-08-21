#!/usr/bin/env python3
"""Ferramenta interativa de remapeamento do ShanWan Gamepad.

Uso:
    sudo python3 remap.py                  # remapeia TODOS os papéis, na ordem padrão
    sudo python3 remap.py Y RB LT          # remapeia só os papéis citados (nessa ordem)
    sudo python3 remap.py --list           # mostra o mapeamento atual sem alterar nada

Papéis válidos: A B X Y LB RB LT RT SELECT START MODE TURBO CLEAR

O script:
  1. Para o serviço shanwan-merger.service (evita conflito de grab exclusivo)
  2. Abre os 3 nós físicos do ShanWan (joystick, consumer, keyboard) com grab exclusivo
  3. Para cada papel pedido, aguarda você pressionar o botão físico correspondente
  4. Atualiza mapping.json com os novos códigos
  5. Reinicia o serviço automaticamente

Precisa rodar como root (sudo) — abrir/travar /dev/input/event* exige privilégio.
"""
import os
import sys
import json
import time
import select
import subprocess

import evdev
from evdev import InputDevice, ecodes

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(SCRIPT_DIR, "mapping.json")
SERVICE_NAME = "shanwan-merger.service"

VID = 0x20BC
PID = 0x5501

DEFAULT_ORDER = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT",
                  "SELECT", "START", "MODE", "TURBO", "CLEAR"]
VALID_ROLES = set(DEFAULT_ORDER)

# Códigos do D-pad — nunca podem ser capturados como papel (ficam fixos)
ARROW_CODES = {ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT}

ROLE_EMOJI = {
    "A": "🟢", "B": "🔴", "X": "🔵", "Y": "🟡",
    "LB": "🔘", "RB": "🔘", "LT": "🎯", "RT": "🎯",
    "SELECT": "⏸️", "START": "▶️", "MODE": "🏠",
    "TURBO": "⚡", "CLEAR": "🧹",
}


def say(msg):
    print(f"\033[1;32m[remap]\033[0m {msg}")


def warn(msg):
    print(f"\033[1;33m[remap]\033[0m {msg}")


def die(msg):
    print(f"\033[1;31m[remap]\033[0m ERRO: {msg}", file=sys.stderr)
    sys.exit(1)


def load_mapping():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            return json.load(f)
    return {}


def save_mapping(mapping):
    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)


def print_current(mapping):
    say("Mapeamento atual:")
    for role in DEFAULT_ORDER:
        e = mapping.get(role)
        if e:
            print(f"  {ROLE_EMOJI.get(role,'')} {role:8} -> device={e['device']:9} code={e['code']}")
        else:
            print(f"  {ROLE_EMOJI.get(role,'')} {role:8} -> (não definido)")


def find_devices():
    """Abre os 3 nós físicos do ShanWan: joystick, consumer, keyboard."""
    joystick = consumer = keyboard = None
    for path in evdev.list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        if d.info.vendor != VID or d.info.product != PID or "(merged)" in (d.name or ""):
            continue
        if d.name == "Xbox 360 Controller":
            continue
        caps = d.capabilities()
        name_lower = (d.name or "").lower()
        if "consumer control" in name_lower:
            consumer = d
        elif "keyboard" in name_lower:
            keyboard = d
        elif "system control" in name_lower:
            continue
        elif ecodes.EV_ABS in caps and caps[ecodes.EV_ABS]:
            joystick = d
    return joystick, consumer, keyboard


def stop_service():
    r = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE_NAME])
    if r.returncode == 0:
        say(f"parando {SERVICE_NAME}…")
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=True)
        return True
    return False


def start_service():
    say(f"reiniciando {SERVICE_NAME}…")
    subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)


def capture_role(role, devices, tag_by_fd):
    """Bloqueia até o usuário pressionar um botão físico; retorna (device_tag, code)."""
    emoji = ROLE_EMOJI.get(role, "")
    say(f"{emoji} Pressione o botão físico para: \033[1m{role}\033[0m")

    poll = select.epoll()
    for fd in tag_by_fd:
        poll.register(fd, select.EPOLLIN)

    try:
        while True:
            events = poll.poll(timeout=60)
            if not events:
                warn("nada pressionado em 60s — repetindo prompt…")
                continue
            for fd, _ in events:
                tag = tag_by_fd[fd]
                dev = devices[tag]
                for ev in dev.read():
                    if ev.type == ecodes.EV_KEY and ev.value == 1:
                        if ev.code in ARROW_CODES:
                            warn("D-pad não pode ser remapeado por aqui — ignorando, pressione outro botão")
                            continue
                        say(f"   -> capturado: device={tag} code={ev.code} "
                            f"({ecodes.KEY.get(ev.code) or ecodes.BTN.get(ev.code) or ev.code})")
                        return tag, ev.code
    finally:
        poll.close()


def main():
    args = sys.argv[1:]

    if os.geteuid() != 0:
        die("execute como root: sudo python3 remap.py")

    mapping = load_mapping()

    if "--list" in args or "-l" in args:
        print_current(mapping)
        return

    if args:
        roles = [a.upper() for a in args]
        invalid = [r for r in roles if r not in VALID_ROLES]
        if invalid:
            die(f"papéis inválidos: {invalid}. Válidos: {sorted(VALID_ROLES)}")
    else:
        roles = DEFAULT_ORDER

    say(f"Papéis a remapear ({len(roles)}): {', '.join(roles)}")
    was_running = stop_service()

    joystick, consumer, keyboard = None, None, None
    for attempt in range(10):
        joystick, consumer, keyboard = find_devices()
        if joystick:
            break
        time.sleep(0.5)
    if not joystick:
        die("controle ShanWan não encontrado — verifique se está conectado")

    devices = {"joystick": joystick}
    if consumer: devices["consumer"] = consumer
    if keyboard: devices["keyboard"] = keyboard

    for tag, dev in devices.items():
        try:
            dev.grab()
        except Exception as e:
            warn(f"não consegui travar {tag}: {e}")

    tag_by_fd = {dev.fd: tag for tag, dev in devices.items()}

    try:
        say("=" * 60)
        say("Pressione cada botão UMA VEZ, na ordem pedida. Ctrl+C cancela.")
        say("=" * 60)

        for role in roles:
            tag, code = capture_role(role, devices, tag_by_fd)
            mapping[role] = {"device": tag, "code": code}
            save_mapping(mapping)  # salva incrementalmente — seguro se cancelar no meio

        say("Todos os papéis remapeados com sucesso!")

    except KeyboardInterrupt:
        warn("cancelado pelo usuário — mapeamento parcial já foi salvo")
    finally:
        for dev in devices.values():
            try: dev.ungrab()
            except Exception: pass
            try: dev.close()
            except Exception: pass

    print()
    print_current(mapping)

    if was_running:
        start_service()
        say("Serviço reiniciado com o novo mapeamento. Pode testar!")
    else:
        warn(f"Serviço estava parado antes — inicie manualmente: sudo systemctl start {SERVICE_NAME}")


if __name__ == "__main__":
    main()
