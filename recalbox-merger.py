#!/usr/bin/env python3
"""SHANWAN Android-mode merger - versao STDLIB PURA (sem python-evdev).

Equivalente ao merger.py (Debian/Fedora/Arch) mas usando apenas a
biblioteca padrao do Python (os/struct/fcntl/select). Feito para rodar
em sistemas minimos como Recalbox/Batocera (Buildroot), onde nao existe
apt/dnf/pacman nem o pacote python-evdev.

Como funciona (mesma logica do merger.py original):
  1. Descobre os nos evdev do ShanWan (20bc:5501) via /proc/bus/input/devices
  2. Abre joystick + consumer + keyboard com EVIOCGRAB (grab exclusivo)
  3. Cria joystick virtual via /dev/uinput com ioctls crus
  4. Traduz:
       KEY_VOLUMEUP/DOWN  -> BTN_START / BTN_SELECT
       KEY_UP/DOWN/LEFT/RIGHT -> ABS_HAT0X / ABS_HAT0Y (D-pad)
       BTN_C (RT fisico)  -> ABS_BRAKE  (indice 5 = RT na Steam/SDL)
       BTN_Z (LT fisico)  -> ABS_GAS    (indice 4 = LT na Steam/SDL)
  5. Encaminha o resto 1:1; sai com OSError (o wrapper reinicia)
"""
import os
import sys
import time
import struct
import fcntl
import select
import signal
import logging
import glob

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(process)d %(message)s")
log = logging.getLogger("shanwan-merger-std")

VID = 0x20BC
PID = 0x5501
VIRTUAL_NAME = "SHANWAN Android Gamepad (merged)"

# ---- constantes ioctl (fixas do kernel linux) -------------------------------
EVIOCGRAB     = 0x40044590
UI_SET_EVBIT  = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_ABS_SETUP  = 0x401C5504   # _IOW('U',4,struct uinput_abs_setup[28 bytes])
UI_DEV_SETUP  = 0x405C5503   # _IOW('U',3,struct uinput_setup[92 bytes])
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

# ---- constantes de evento ---------------------------------------------------
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
EV_MSC = 4

KEY_VOLUMEUP = 115
KEY_VOLUMEDOWN = 114
KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106

BTN_START = 315
BTN_SELECT = 314
BTN_C = 306   # RT fisico (jstest btn "2")
BTN_Z = 309   # LT fisico (jstest btn "5")

ABS_HAT0X = 16
ABS_HAT0Y = 17
ABS_GAS = 9    # indice 4 = LT na Steam
ABS_BRAKE = 10 # indice 5 = RT na Steam

TRIGGER_BTN_TO_AXIS = {BTN_C: ABS_BRAKE, BTN_Z: ABS_GAS}
ARROW_MAP = {KEY_UP: (ABS_HAT0Y, -1), KEY_DOWN: (ABS_HAT0Y, 1),
             KEY_LEFT: (ABS_HAT0X, -1), KEY_RIGHT: (ABS_HAT0X, 1)}

# absinfo reais do aparelho (capturados via evtest em 2026-08-16):
#   X/Y/Z/RZ/GAS/BRAKE: min=0 max=255 fuzz=0 flat=15
#   HAT0X/HAT0Y:        min=-1 max=1
ABS_DEFS = {
    0:  (0, 255, 0, 15),   # ABS_X
    1:  (0, 255, 0, 15),   # ABS_Y
    2:  (0, 255, 0, 15),   # ABS_Z  (dummy, nunca emite)
    5:  (0, 255, 0, 15),   # ABS_RZ (dummy, nunca emite)
    ABS_GAS:    (0, 255, 0, 15),
    ABS_BRAKE:  (0, 255, 0, 15),
    ABS_HAT0X:  (-1, 1, 0, 0),
    ABS_HAT0Y:  (-1, 1, 0, 0),
}

# ---- formato do struct input_event -------------------------------------------
# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# 64-bit: timeval = 2x long (8B)  -> 16+2+2+4 = 24 bytes
# 32-bit (arm): timeval = 2x int (4B) -> 8+2+2+4  = 16 bytes
EV_FMT = 'llHHi' if struct.calcsize('l') == 8 else 'iiHHi'
EV_SIZE = struct.calcsize(EV_FMT)


def parse_bits(tokens):
    """B: KEY=... / B: ABS=... -> lista de codigos (64-bit longs, MSW primeiro)."""
    bits = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        word_idx = n - 1 - i
        v = int(tok, 16)
        for b in range(64):
            if v >> b & 1:
                bits.append(word_idx * 64 + b)
    return bits


def discover():
    """Procura os nos do ShanWan via /proc/bus/input/devices.

    Retorna dicts: {path, fd, keys, abs_bits, name} para joystick,
    consumer e keyboard. Ignora System Control e o proprio virtual.
    """
    joystick = consumer = keyboard = None
    data = open('/proc/bus/input/devices').read()
    for block in data.split('\n\n'):
        if 'Vendor=%04x Product=%04x' % (VID, PID) not in block:
            continue
        name = ''
        ev_num = None
        keys = []
        abs_bits = []
        for line in block.splitlines():
            if line.startswith('N: Name='):
                name = line.split('=', 1)[1].strip().strip('"')
            elif line.startswith('H: Handlers='):
                for tok in line.split('=', 1)[1].split():
                    if tok.startswith('event'):
                        ev_num = int(tok[5:])
            elif line.startswith('B: KEY='):
                keys = parse_bits(line.split('=', 1)[1].split())
            elif line.startswith('B: ABS='):
                abs_bits = parse_bits(line.split('=', 1)[1].split())
        if ev_num is None or '(merged)' in name:
            continue
        path = '/dev/input/event%d' % ev_num
        if 'Consumer Control' in name:
            consumer = {'path': path, 'name': name}
        elif 'Keyboard' in name:
            keyboard = {'path': path, 'name': name}
        elif 'System Control' in name:
            continue
        elif abs_bits:  # joystick: tem eixos ABS e nao eh System/Consumer/Keyboard
            joystick = {'path': path, 'name': name,
                        'keys': keys, 'abs': abs_bits}
    return joystick, consumer, keyboard


def safe_grab(path, label):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, EVIOCGRAB, 1)
        log.info("grabbed %s exclusively (%s)", label, path)
        return fd
    except OSError as exc:
        log.warning("could not grab %s (%s): %s", label, path, exc)
        try:
            os.close(fd)
        except Exception:
            pass
        return None


def create_uinput(joystick_keys):
    """Cria o joystick virtual via /dev/uinput; devolve o fd."""
    ufd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(ufd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(ufd, UI_SET_EVBIT, EV_ABS)
    fcntl.ioctl(ufd, UI_SET_EVBIT, EV_MSC)

    keys = set(joystick_keys) | {BTN_START, BTN_SELECT}
    for k in sorted(keys):
        fcntl.ioctl(ufd, UI_SET_KEYBIT, k)

    for code, (mn, mx, fz, fl) in ABS_DEFS.items():
        fcntl.ioctl(ufd, UI_SET_ABSBIT, code)
        # struct uinput_abs_setup { u16 code; u16 filler; input_absinfo (6x i32) }
        fcntl.ioctl(ufd, UI_ABS_SETUP,
                    struct.pack('H2x6i', code, 0, mn, mx, fz, fl, 0))

    # struct uinput_setup { input_id id; char name[80]; u32 ff_effects_max; }
    setup = struct.pack('4H80sI', 0x03, VID, PID, 0x0111,
                        VIRTUAL_NAME.encode()[:79] + b'\0', 0)
    fcntl.ioctl(ufd, UI_DEV_SETUP, setup)
    fcntl.ioctl(ufd, UI_DEV_CREATE)
    log.info("virtual gamepad created via /dev/uinput: %s", VIRTUAL_NAME)
    return ufd


def write_event(ufd, ev_type, code, value):
    os.write(ufd, struct.pack(EV_FMT, 0, 0, ev_type, code, value))


def read_event(fd):
    """Le um input_event do fd; retorna (type, code, value) ou None.

    None tambem significa 'sem dados agora' (EAGAIN em fd nao-bloqueante)
    - o loop deve apenas voltar ao poll(). Um read de 0 bytes (EOF)
    indica device removido e tambem retorna None; o poll() repetira
    POLLHUP e o OSError de leitura seguinte encerrara o processo.
    """
    try:
        raw = os.read(fd, EV_SIZE)
    except BlockingIOError:
        return None
    if len(raw) != EV_SIZE:
        return None
    _, _, t, c, v = struct.unpack(EV_FMT, raw)
    return t, c, v


def main():
    log.info("scanning /proc/bus/input/devices for ShanWan %04x:%04x", VID, PID)
    joystick = consumer = keyboard = None
    for attempt in range(30):
        joystick, consumer, keyboard = discover()
        if joystick is not None:
            break
        time.sleep(1)
        if attempt % 5 == 4:
            log.info("still waiting for device (attempt %d)", attempt + 1)
    if joystick is None:
        log.error("joystick node of ShanWan not found")
        sys.exit(1)

    log.info("joystick node: %s", joystick['path'])
    if consumer:
        log.info("consumer node: %s", consumer['path'])
    if keyboard:
        log.info("keyboard node: %s", keyboard['path'])

    jfd = safe_grab(joystick['path'], "joystick")
    cfd = safe_grab(consumer['path'], "consumer") if consumer else None
    kfd = safe_grab(keyboard['path'], "keyboard") if keyboard else None
    if jfd is None:
        log.error("could not open/grab joystick - aborting")
        sys.exit(1)

    ufd = create_uinput(joystick['keys'])

    running = [True]

    def stop(*_):
        running[0] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    poll = select.poll()
    poll.register(jfd, select.POLLIN | select.POLLHUP)
    fds = {}
    if cfd:
        poll.register(cfd, select.POLLIN | select.POLLHUP)
        fds[cfd] = 'consumer'
    if kfd:
        poll.register(kfd, select.POLLIN | select.POLLHUP)
        fds[kfd] = 'keyboard'

    btn_state = {BTN_START: 0, BTN_SELECT: 0}

    try:
        while running[0]:
            for fd, ev_flags in poll.poll(1000):
                if ev_flags & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    log.warning("fd %d closed/removed (flags=%d) - exiting",
                                fd, ev_flags)
                    raise OSError("input device removed")
                if fd == jfd:
                    while True:
                        ev = read_event(jfd)
                        if ev is None:
                            break
                        t, c, v = ev
                        if t == EV_SYN:
                            write_event(ufd, EV_SYN, 0, 0)
                        elif t == EV_KEY and c in TRIGGER_BTN_TO_AXIS:
                            axis = TRIGGER_BTN_TO_AXIS[c]
                            val = 255 if v else 0
                            write_event(ufd, EV_ABS, axis, val)
                        else:
                            write_event(ufd, t, c, v)
                else:
                    label = fds.get(fd)
                    if label is None:
                        continue
                    while True:
                        ev = read_event(fd)
                        if ev is None:
                            break
                        t, c, v = ev
                        if t == EV_KEY:
                            if c == KEY_VOLUMEUP:
                                if btn_state[BTN_START] != v:
                                    write_event(ufd, EV_KEY, BTN_START, v)
                                    btn_state[BTN_START] = v
                            elif c == KEY_VOLUMEDOWN:
                                if btn_state[BTN_SELECT] != v:
                                    write_event(ufd, EV_KEY, BTN_SELECT, v)
                                    btn_state[BTN_SELECT] = v
                            elif c in ARROW_MAP:
                                axis, sign = ARROW_MAP[c]
                                write_event(ufd, EV_ABS, axis, sign if v else 0)
                        elif t == EV_SYN:
                            write_event(ufd, EV_SYN, 0, 0)
    except OSError as exc:
        log.warning("device disappeared: %s", exc)
        raise
    finally:
        if ufd is not None:
            try:
                fcntl.ioctl(ufd, UI_DEV_DESTROY)
            except Exception:
                pass
        for fd in (jfd, cfd, kfd, ufd):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
        log.info("shutdown complete")


if __name__ == "__main__":
    main()