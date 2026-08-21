#!/usr/bin/env python3
"""SHANWAN Android-mode merger — Emula um Xbox 360 Controller genuíno.

O dispositivo virtual criado usa VID/PID/versão e ordem exata de capacidades
do driver `xpad` (Microsoft Xbox 360 Controller: 045E:028E). Isso faz com que
SDL2, Steam e praticamente todo jogo/emulador Linux reconheçam o controle
NATIVAMENTE, sem depender de nenhuma entrada em GameControllerDB — validado
via SDL2 (SDL_GameControllerGetButton/GetAxis) com hardware real.

O mapeamento físico -> papel Xbox é carregado de `mapping.json` (mesma pasta).
Para remapear os botões sem editar código, use:

    sudo python3 remap.py            # remapeia tudo, na ordem A B X Y LB RB LT RT SELECT START MODE TURBO CLEAR
    sudo python3 remap.py Y RB       # remapeia só os papéis citados

Papéis suportados (ROLE_TARGETS) e efeito no dispositivo virtual:
  A, B, X, Y, LB, RB, SELECT, START, MODE -> botão digital (EV_KEY)
  LT, RT                                   -> eixo analógico puro (EV_ABS, 0-255)
  TURBO, CLEAR                             -> modificadores do motor de turbo (não viram botão)

Motor Turbo / Clear:
  - Segure TURBO + pressione um botão -> ativa repetição ~16 Hz (persistida em disco).
  - Segure CLEAR + pressione um botão -> desativa a repetição desse botão.
  - CLEAR longo (>1.5s) sem combinação -> limpa todos os turbos.
"""
import os
import sys
import json
import time
import select
import signal
import logging

import evdev
from evdev import InputDevice, UInput, ecodes, AbsInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s (PID %(process)d): %(message)s"
)
log = logging.getLogger("shanwan-merger")

VID = 0x20BC
PID = 0x5501
VIRTUAL_NAME = "Xbox 360 Controller"

VIRTUAL_VID     = 0x045E
VIRTUAL_PID     = 0x028E
VIRTUAL_VERSION = 0x0110

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE     = os.path.join(SCRIPT_DIR, "mapping.json")
TURBO_STATE_FILE = os.path.join(SCRIPT_DIR, "turbo_state.json")

# Papel -> ("KEY", código_virtual) | ("AXIS", eixo_virtual)
# LT/RT são só eixo analógico, sem botão digital — igual ao Xbox 360 real.
ROLE_TARGETS = {
    "A":      ("KEY",  ecodes.BTN_SOUTH),
    "B":      ("KEY",  ecodes.BTN_EAST),
    "X":      ("KEY",  ecodes.BTN_NORTH),
    "Y":      ("KEY",  ecodes.BTN_WEST),
    "LB":     ("KEY",  ecodes.BTN_TL),
    "RB":     ("KEY",  ecodes.BTN_TR),
    "LT":     ("AXIS", ecodes.ABS_Z),
    "RT":     ("AXIS", ecodes.ABS_RZ),
    "SELECT": ("KEY",  ecodes.BTN_SELECT),
    "START":  ("KEY",  ecodes.BTN_START),
    "MODE":   ("KEY",  ecodes.BTN_MODE),
}
SPECIAL_ROLES = {"TURBO", "CLEAR"}  # modificadores — não viram botão no virtual

# D-pad (nó Keyboard) — fixo, não faz parte do remapeamento de botões
ARROW_MAP = {
    ecodes.KEY_UP:    (ecodes.ABS_HAT0Y, -1),
    ecodes.KEY_DOWN:  (ecodes.ABS_HAT0Y,  1),
    ecodes.KEY_LEFT:  (ecodes.ABS_HAT0X, -1),
    ecodes.KEY_RIGHT: (ecodes.ABS_HAT0X,  1),
}

TRIGGER_PRESSED  = 255
TRIGGER_RELEASED = 0
TURBO_INTERVAL   = 0.035   # ~16 Hz


# ---------------------------------------------------------------------------
def load_mapping():
    """Lê mapping.json -> { (device_tag, code): role_name }."""
    with open(MAPPING_FILE) as f:
        raw = json.load(f)
    by_source = {}
    for role, entry in raw.items():
        if role.startswith("_"):
            continue
        by_source[(entry["device"], entry["code"])] = role
    log.info("mapping.json carregado: %d papéis", len(by_source))
    return by_source


def load_turbo_state():
    try:
        if os.path.exists(TURBO_STATE_FILE):
            with open(TURBO_STATE_FILE) as f:
                return set(json.load(f).get("turbo_roles", []))
    except Exception as e:
        log.warning("turbo_state load failed: %s", e)
    return set()


def save_turbo_state(s):
    try:
        with open(TURBO_STATE_FILE, "w") as f:
            json.dump({"turbo_roles": sorted(s)}, f, indent=2)
    except Exception as e:
        log.warning("turbo_state save failed: %s", e)


def looks_like_shanwan(dev):
    try:
        return dev.info.vendor == VID and dev.info.product == PID
    except Exception:
        return False


def is_virtual(dev):
    return dev.name == VIRTUAL_NAME


def categorize(caps, name, by_source):
    n = (name or "").lower()
    if "consumer control" in n:
        return "consumer"
    if "keyboard" in n or "kbd" in n:
        return "keyboard"
    if "system control" in n:
        return None
    keys = caps.get(ecodes.EV_KEY, [])
    if any(("consumer", k) in by_source for k in keys):
        return "consumer"
    if any(k in ARROW_MAP for k in keys):
        return "keyboard"
    return None


def discover(by_source):
    joystick = consumer = keyboard = None
    leftovers = []
    for path in evdev.list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        if not looks_like_shanwan(d) or is_virtual(d):
            d.close()
            continue
        caps = d.capabilities()
        has_abs = ecodes.EV_ABS in caps and caps[ecodes.EV_ABS]
        if has_abs and joystick is None:
            joystick = d
            continue
        cat = categorize(caps, d.name, by_source)
        if cat == "consumer" and consumer is None:
            consumer = d
        elif cat == "keyboard" and keyboard is None:
            keyboard = d
        else:
            leftovers.append(d)
    for d in leftovers:
        try:
            d.close()
        except Exception:
            pass
    return joystick, consumer, keyboard


def build_uinput():
    """Cria o dispositivo virtual com capacidades IDÊNTICAS ao Xbox 360 (xpad)."""
    keys = [
        ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST,
        ecodes.BTN_TL, ecodes.BTN_TR,
        ecodes.BTN_SELECT, ecodes.BTN_START, ecodes.BTN_MODE,
        ecodes.BTN_THUMBL, ecodes.BTN_THUMBR,
    ]
    axes = [
        (ecodes.ABS_X,     AbsInfo(0, -32768, 32767, 16, 128, 0)),
        (ecodes.ABS_Y,     AbsInfo(0, -32768, 32767, 16, 128, 0)),
        (ecodes.ABS_Z,     AbsInfo(0, 0, 255, 0, 0, 0)),
        (ecodes.ABS_RX,    AbsInfo(0, -32768, 32767, 16, 128, 0)),
        (ecodes.ABS_RY,    AbsInfo(0, -32768, 32767, 16, 128, 0)),
        (ecodes.ABS_RZ,    AbsInfo(0, 0, 255, 0, 0, 0)),
        (ecodes.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
        (ecodes.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
    ]
    return UInput(
        events={ecodes.EV_KEY: keys, ecodes.EV_ABS: axes},
        name=VIRTUAL_NAME, vendor=VIRTUAL_VID, product=VIRTUAL_PID,
        version=VIRTUAL_VERSION, bustype=ecodes.BUS_USB,
    )


def safe_grab(dev, label):
    try:
        dev.grab()
        log.info("grabbed %s (%s)", label, dev.path)
    except Exception as e:
        log.warning("could not grab %s: %s", label, e)


def emit(ui, role, state):
    entry = ROLE_TARGETS.get(role)
    if entry is None:
        return
    if entry[0] == "KEY":
        ui.write(ecodes.EV_KEY, entry[1], state)
    else:  # AXIS
        ui.write(ecodes.EV_ABS, entry[1], TRIGGER_PRESSED if state else TRIGGER_RELEASED)


def main():
    log.info("ShanWan merger iniciando — emulação Xbox 360 Controller (045E:028E)")
    by_source = load_mapping()
    turbo_roles = load_turbo_state()
    log.info("Turbos persistidos: %s", sorted(turbo_roles) or "nenhum")

    joystick = consumer = keyboard = None
    for attempt in range(30):
        joystick, consumer, keyboard = discover(by_source)
        if joystick:
            break
        time.sleep(1)
        if attempt % 5 == 4:
            log.info("aguardando dispositivo (tentativa %d)…", attempt + 1)
    if not joystick:
        log.error("nó joystick ShanWan não encontrado — abortando")
        sys.exit(1)

    log.info("joystick : %s (%s)", joystick.name, joystick.path)
    if consumer:
        log.info("consumer : %s (%s)", consumer.name, consumer.path)
    if keyboard:
        log.info("keyboard : %s (%s)", keyboard.name, keyboard.path)

    if consumer: safe_grab(consumer, "consumer")
    if keyboard: safe_grab(keyboard, "keyboard")
    safe_grab(joystick, "joystick")

    ui = build_uinput()
    log.info("virtual  : %s", getattr(ui, "device", "(uinput)"))

    turbo_mod   = False
    clear_mod   = False
    clear_t0    = 0.0
    held_turbo  = set()   # papéis mantidos pressionados COM turbo ativo
    turbo_phase = 0
    last_tick   = time.time()

    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    signal.signal(signal.SIGINT,  lambda *_: running.__setitem__(0, False))

    poll = select.epoll()
    poll.register(joystick.fd, select.EPOLLIN)
    fd_tag = {joystick.fd: "joystick"}
    fd_dev = {joystick.fd: joystick}
    if consumer:
        poll.register(consumer.fd, select.EPOLLIN)
        fd_tag[consumer.fd] = "consumer"
        fd_dev[consumer.fd] = consumer
    if keyboard:
        poll.register(keyboard.fd, select.EPOLLIN)
        fd_tag[keyboard.fd] = "keyboard"
        fd_dev[keyboard.fd] = keyboard

    def handle_role_press(role, v):
        if v == 1:
            if turbo_mod:
                turbo_roles.add(role)
                save_turbo_state(turbo_roles)
                log.info("🔥 TURBO ON  %s", role)
            elif clear_mod:
                turbo_roles.discard(role)
                held_turbo.discard(role)
                save_turbo_state(turbo_roles)
                log.info("❄️  TURBO OFF %s", role)
            elif role in turbo_roles:
                held_turbo.add(role)
            else:
                emit(ui, role, 1)
                ui.syn()
        else:
            held_turbo.discard(role)
            emit(ui, role, 0)
            ui.syn()

    try:
        while running[0]:
            evs = poll.poll(timeout=0.020)
            now = time.time()

            for fd, _ in evs:
                dev = fd_dev[fd]
                tag = fd_tag[fd]

                for ev in dev.read():
                    if ev.type == ecodes.EV_SYN:
                        ui.syn()
                        continue
                    if ev.type == ecodes.EV_ABS:
                        continue  # sem sticks reais neste aparelho

                    if ev.type != ecodes.EV_KEY:
                        continue
                    c, v = ev.code, ev.value

                    if tag == "keyboard" and c in ARROW_MAP:
                        axis, sign = ARROW_MAP[c]
                        ui.write(ecodes.EV_ABS, axis, sign if v else 0)
                        ui.syn()
                        continue

                    role = by_source.get((tag, c))
                    if role is None:
                        continue

                    if role == "TURBO":
                        turbo_mod = bool(v)
                        continue

                    if role == "CLEAR":
                        clear_mod = bool(v)
                        if v == 1:
                            clear_t0 = now
                        else:
                            if clear_t0 and now - clear_t0 >= 1.5 and turbo_roles:
                                turbo_roles.clear()
                                held_turbo.clear()
                                save_turbo_state(turbo_roles)
                                log.info("🧹 todos os turbos removidos (CLEAR longo)")
                            clear_t0 = 0.0
                        continue

                    handle_role_press(role, v)

            if held_turbo and now - last_tick >= TURBO_INTERVAL:
                turbo_phase ^= 1
                for role in list(held_turbo):
                    emit(ui, role, turbo_phase)
                ui.syn()
                last_tick = now

    except OSError as e:
        log.warning("dispositivo removido: %s", e)
        raise
    finally:
        for fd in fd_dev:
            try: poll.unregister(fd)
            except Exception: pass
        for dev in (consumer, keyboard):
            if dev is None: continue
            try: dev.ungrab()
            except Exception: pass
            try: dev.close()
            except Exception: pass
        try: ui.close()
        except Exception: pass
        try: joystick.close()
        except Exception: pass
        log.info("encerrado")


if __name__ == "__main__":
    main()
