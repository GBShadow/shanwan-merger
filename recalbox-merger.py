#!/usr/bin/env python3
"""SHANWAN Android-mode merger — STDLIB PURA, emula Xbox 360 Controller.

Equivalente ao merger.py mas usando apenas a biblioteca padrão do Python
(os, struct, fcntl, select, json). Para Recalbox/Batocera (Buildroot).

Carrega o mapeamento físico -> papel Xbox de `mapping.json` (mesma pasta,
compartilhado com merger.py e gerado/editado por remap.py). Para remapear:

    sudo python3 remap.py            # remapeia tudo
    sudo python3 remap.py Y RB       # remapeia só os papéis citados

VID/PID/versão do virtual emulam Xbox 360 real (driver xpad: 045E:028E) para
reconhecimento nativo por SDL2/EmulationStation/RetroArch, sem GameControllerDB
customizada — validado com hardware real via SDL2.
"""
import os
import sys
import json
import time
import struct
import fcntl
import select
import signal
import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s (PID %(process)d): %(message)s")
log = logging.getLogger("shanwan-recalbox")

VID = 0x20BC
PID = 0x5501
VIRTUAL_NAME = "Xbox 360 Controller"

VIRTUAL_VID     = 0x045E
VIRTUAL_PID     = 0x028E
VIRTUAL_VERSION = 0x0110

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE     = os.path.join(SCRIPT_DIR, "mapping.json")
TURBO_STATE_FILE = "/tmp/shanwan_turbo_state.json"

# ---- ioctl constants --------------------------------------------------------
EVIOCGRAB      = 0x40044590
UI_SET_EVBIT   = 0x40045564
UI_SET_KEYBIT  = 0x40045565
UI_SET_ABSBIT  = 0x40045567
UI_ABS_SETUP   = 0x401C5504
UI_DEV_SETUP   = 0x405C5503
UI_DEV_CREATE  = 0x5501
UI_DEV_DESTROY = 0x5502

# ---- event types ------------------------------------------------------------
EV_SYN = 0; EV_KEY = 1; EV_ABS = 3

# ---- button codes (padrão xpad / Xbox 360) ----------------------------------
BTN_SOUTH = 304; BTN_EAST = 305; BTN_NORTH = 307; BTN_WEST  = 308
BTN_TL    = 310; BTN_TR   = 311
BTN_SELECT= 314; BTN_START= 315; BTN_MODE  = 316
BTN_THUMBL= 317; BTN_THUMBR= 318

KEY_UP = 103; KEY_DOWN = 108; KEY_LEFT = 105; KEY_RIGHT = 106

ABS_X = 0; ABS_Y = 1; ABS_Z = 2; ABS_RX = 3; ABS_RY = 4; ABS_RZ = 5
ABS_HAT0X = 16; ABS_HAT0Y = 17

ARROW_MAP = {
    KEY_UP:    (ABS_HAT0Y, -1),
    KEY_DOWN:  (ABS_HAT0Y,  1),
    KEY_LEFT:  (ABS_HAT0X, -1),
    KEY_RIGHT: (ABS_HAT0X,  1),
}

# Papel -> ("KEY", código) | ("AXIS", eixo)
ROLE_TARGETS = {
    "A":      ("KEY",  BTN_SOUTH),
    "B":      ("KEY",  BTN_EAST),
    "X":      ("KEY",  BTN_NORTH),
    "Y":      ("KEY",  BTN_WEST),
    "LB":     ("KEY",  BTN_TL),
    "RB":     ("KEY",  BTN_TR),
    "LT":     ("AXIS", ABS_Z),
    "RT":     ("AXIS", ABS_RZ),
    "SELECT": ("KEY",  BTN_SELECT),
    "START":  ("KEY",  BTN_START),
    "MODE":   ("KEY",  BTN_MODE),
}

TRIGGER_PRESSED  = 255
TRIGGER_RELEASED = 0
TURBO_INTERVAL   = 0.035

EV_FMT  = 'llHHi' if struct.calcsize('l') == 8 else 'iiHHi'
EV_SIZE = struct.calcsize(EV_FMT)


def load_mapping():
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


def parse_bits(tokens):
    bits = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        v = int(tok, 16)
        wi = n - 1 - i
        for b in range(64):
            if v >> b & 1:
                bits.append(wi * 64 + b)
    return bits


def discover(by_source):
    joystick = consumer = keyboard = None
    data = open('/proc/bus/input/devices').read()
    for block in data.split('\n\n'):
        if 'Vendor=%04x Product=%04x' % (VID, PID) not in block:
            continue
        name = ev_num = None
        abs_bits = []
        for line in block.splitlines():
            if line.startswith('N: Name='):
                name = line.split('=',1)[1].strip().strip('"')
            elif line.startswith('H: Handlers='):
                for tok in line.split('=',1)[1].split():
                    if tok.startswith('event'):
                        ev_num = int(tok[5:])
            elif line.startswith('B: ABS='):
                abs_bits = parse_bits(line.split('=',1)[1].split())
        if ev_num is None or name == VIRTUAL_NAME:
            continue
        path = '/dev/input/event%d' % ev_num
        if 'Consumer Control' in name:
            consumer = {'path': path, 'name': name}
        elif 'Keyboard' in name:
            keyboard = {'path': path, 'name': name}
        elif 'System Control' in name:
            continue
        elif abs_bits:
            joystick = {'path': path, 'name': name}
    return joystick, consumer, keyboard


def safe_grab(path, label):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, EVIOCGRAB, 1)
        log.info("grabbed %s (%s)", label, path)
        return fd
    except OSError as e:
        log.warning("could not grab %s: %s", label, e)
        return None


def create_uinput():
    ufd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    for ev in (EV_KEY, EV_ABS):
        fcntl.ioctl(ufd, UI_SET_EVBIT, ev)
    for k in sorted([BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST,
                     BTN_TL, BTN_TR,
                     BTN_SELECT, BTN_START, BTN_MODE,
                     BTN_THUMBL, BTN_THUMBR]):
        fcntl.ioctl(ufd, UI_SET_KEYBIT, k)

    abs_defs = {
        ABS_X:  (-32768, 32767, 16, 128),
        ABS_Y:  (-32768, 32767, 16, 128),
        ABS_Z:  (0, 255, 0, 0),
        ABS_RX: (-32768, 32767, 16, 128),
        ABS_RY: (-32768, 32767, 16, 128),
        ABS_RZ: (0, 255, 0, 0),
        ABS_HAT0X: (-1, 1, 0, 0),
        ABS_HAT0Y: (-1, 1, 0, 0),
    }
    for code, (mn, mx, fz, fl) in abs_defs.items():
        fcntl.ioctl(ufd, UI_SET_ABSBIT, code)
        fcntl.ioctl(ufd, UI_ABS_SETUP,
                    struct.pack('H2x6i', code, 0, mn, mx, fz, fl, 0))

    setup = struct.pack('4H80sI', 0x03, VIRTUAL_VID, VIRTUAL_PID, VIRTUAL_VERSION,
                        VIRTUAL_NAME.encode()[:79] + b'\0', 0)
    fcntl.ioctl(ufd, UI_DEV_SETUP, setup)
    fcntl.ioctl(ufd, UI_DEV_CREATE)
    log.info("virtual gamepad created (Xbox 360 emulado): %s", VIRTUAL_NAME)
    return ufd


def write_ev(ufd, t, c, v):
    os.write(ufd, struct.pack(EV_FMT, 0, 0, t, c, v))


def read_ev(fd):
    try:
        raw = os.read(fd, EV_SIZE)
    except BlockingIOError:
        return None
    if len(raw) != EV_SIZE:
        return None
    _, _, t, c, v = struct.unpack(EV_FMT, raw)
    return t, c, v


def emit(ufd, role, state):
    entry = ROLE_TARGETS.get(role)
    if entry is None:
        return
    if entry[0] == "KEY":
        write_ev(ufd, EV_KEY, entry[1], state)
    else:
        write_ev(ufd, EV_ABS, entry[1], TRIGGER_PRESSED if state else TRIGGER_RELEASED)


def main():
    log.info("ShanWan Recalbox merger — emulação Xbox 360 Controller (045E:028E)")
    by_source = load_mapping()
    turbo_roles = load_turbo_state()
    log.info("turbos persistidos: %s", sorted(turbo_roles) or "nenhum")

    joystick = consumer = keyboard = None
    for attempt in range(30):
        joystick, consumer, keyboard = discover(by_source)
        if joystick: break
        time.sleep(1)
        if attempt % 5 == 4:
            log.info("aguardando dispositivo (tentativa %d)…", attempt + 1)
    if not joystick:
        log.error("nó joystick não encontrado — abortando")
        sys.exit(1)

    log.info("joystick : %s", joystick['path'])
    if consumer: log.info("consumer : %s", consumer['path'])
    if keyboard: log.info("keyboard : %s", keyboard['path'])

    jfd = safe_grab(joystick['path'], "joystick")
    cfd = safe_grab(consumer['path'], "consumer") if consumer else None
    kfd = safe_grab(keyboard['path'], "keyboard") if keyboard else None
    if jfd is None:
        log.error("falha ao abrir joystick — abortando")
        sys.exit(1)

    ufd = create_uinput()

    turbo_mod = clear_mod = False
    clear_t0  = 0.0
    held_turbo  = set()
    turbo_phase = 0
    last_tick   = time.time()

    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    signal.signal(signal.SIGINT,  lambda *_: running.__setitem__(0, False))

    poll = select.poll()
    poll.register(jfd, select.POLLIN | select.POLLHUP)
    fd_tag = {jfd: 'joystick'}
    if cfd: poll.register(cfd, select.POLLIN | select.POLLHUP); fd_tag[cfd] = 'consumer'
    if kfd: poll.register(kfd, select.POLLIN | select.POLLHUP); fd_tag[kfd] = 'keyboard'

    def handle_role_press(role, v):
        nonlocal turbo_mod, clear_mod
        if v == 1:
            if turbo_mod:
                turbo_roles.add(role); save_turbo_state(turbo_roles)
                log.info("🔥 TURBO ON  %s", role)
            elif clear_mod:
                turbo_roles.discard(role); held_turbo.discard(role)
                save_turbo_state(turbo_roles)
                log.info("❄️  TURBO OFF %s", role)
            elif role in turbo_roles:
                held_turbo.add(role)
            else:
                emit(ufd, role, 1); write_ev(ufd, EV_SYN, 0, 0)
        else:
            held_turbo.discard(role)
            emit(ufd, role, 0); write_ev(ufd, EV_SYN, 0, 0)

    try:
        while running[0]:
            now = time.time()
            for fd, flags in poll.poll(20):
                if flags & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    raise OSError("device removed (fd=%d)" % fd)

                tag = fd_tag[fd]
                while True:
                    ev = read_ev(fd)
                    if ev is None: break
                    t, c, v = ev
                    if t == EV_SYN:
                        write_ev(ufd, EV_SYN, 0, 0)
                        continue
                    if t == EV_ABS:
                        continue  # sem sticks reais neste aparelho
                    if t != EV_KEY:
                        continue

                    if tag == 'keyboard' and c in ARROW_MAP:
                        axis, sign = ARROW_MAP[c]
                        write_ev(ufd, EV_ABS, axis, sign if v else 0)
                        write_ev(ufd, EV_SYN, 0, 0)
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
                                turbo_roles.clear(); held_turbo.clear()
                                save_turbo_state(turbo_roles)
                                log.info("🧹 todos turbos removidos")
                            clear_t0 = 0.0
                        continue

                    handle_role_press(role, v)

            if held_turbo and now - last_tick >= TURBO_INTERVAL:
                turbo_phase ^= 1
                for role in list(held_turbo):
                    emit(ufd, role, turbo_phase)
                write_ev(ufd, EV_SYN, 0, 0)
                last_tick = now

    except OSError as e:
        log.warning("device removed: %s", e)
        raise
    finally:
        try: fcntl.ioctl(ufd, UI_DEV_DESTROY)
        except Exception: pass
        for fd in (jfd, cfd, kfd, ufd):
            if fd is not None:
                try: os.close(fd)
                except Exception: pass
        log.info("shutdown complete")


if __name__ == "__main__":
    main()
