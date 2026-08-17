#!/usr/bin/env python3
"""SHANWAN Android-mode merger.

Une os varios evdev do gamepad ShanWan (20bc:5501) em um joystick
virtual unico via uinput.

Layout dos nos deste aparelho em modo Android (todos vid/pid 20bc:5501):
  joystick  (event20, iface 1.0)  faces/axes/ABS_HAT em caps (mas o
                                   dpad real NAO emite aqui)
  consumer  (event22, iface 1.1)  "Consumer Control" -> Start/Clear chegam
                                   como KEY_VOLUMEUP/KEY_VOLUMEDOWN
  keyboard  (event23, iface 1.1)  "Keyboard" -> D-pad/alavanca chegam como
                                   KEY_UP/KEY_DOWN/KEY_LEFT/KEY_RIGHT
                                   (alem de volume keys duplicados)

Traducao aplicada no virtual (uinput) "SHANWAN Android Gamepad (merged)":
  KEY_VOLUMEUP   -> BTN_START
  KEY_VOLUMEDOWN -> BTN_SELECT
  KEY_UP/DOWN    -> ABS_HAT0Y (-1 / +1)
  KEY_LEFT/RIGHT -> ABS_HAT0X (-1 / +1)
Eventos do no joystick sao encaminhados 1:1 (faces, ABS_X/Y, ABS_Z/RZ,
ABS_GAS/BRAKE).

Os nos consumer e keyboard sao abertos com EVIOCGRAB (grab exclusivo)
para que suas teclas (volume Setas) parem de chegar ao TTY/XSession,
evitando setas fantasma no terminal e OSD de volume.
"""
import os
import sys
import time
import select
import signal
import logging

import evdev
from evdev import InputDevice, UInput, ecodes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(process)d %(message)s")
log = logging.getLogger("shanwan-merger")

VID = 0x20BC
PID = 0x5501
VIRT_NAME_TAG = "(merged)"
VIRTUAL_NAME = "SHANWAN Android Gamepad (merged)"

START_SRC = ecodes.KEY_VOLUMEUP
SELECT_SRC = ecodes.KEY_VOLUMEDOWN

ARROW_MAP = {
    ecodes.KEY_UP:    (ecodes.ABS_HAT0Y, -1),
    ecodes.KEY_DOWN:  (ecodes.ABS_HAT0Y,  1),
    ecodes.KEY_LEFT:  (ecodes.ABS_HAT0X, -1),
    ecodes.KEY_RIGHT: (ecodes.ABS_HAT0X,  1),
}

# Traducao de gatilhos em forma de botao para forma de eixo analógico.
# Muitos gamepads arcade (este ShanWan inclusive) nao tem eixo analogico
# real nos gatilhos - o firmware os reporta como botões.
# CAPTURA CONFIRMADA via evtest: neste aparelho, os gatilhos fisicos
# LT e RT chegam ao nó joystick (event4) como os codigos
#   RT fisico -> BTN_C (code 306) -> aparece como botão "2" no jstest-gtk
#   LT fisico -> BTN_Z (code 309) -> aparece como botão "5" no jstest-gtk
#
# IMPORTANTE - como a Steam/SDL enumeram eixos POR POSICAO (indice),
# e nao por nome, o virtual device precisa declarar os eixos na ordem
# que a Steam Input espera no layout generico:
#   indice 0 = ABS_X   (left stick X)
#   indice 1 = ABS_Y   (left stick Y)
#   indice 2 = ABS_Z   (right stick X - dummy, nunca emite)
#   indice 3 = ABS_RZ  (right stick Y - dummy, nunca emite)
#   indice 4 = ABS_GAS   (LT - USAR para o gatilho esquerdo)
#   indice 5 = ABS_BRAKE (RT - USAR para o gatilho direito)
# Por isso usamos GAS/BRAKE em vez de Z/RZ: ABS_Z/ABS_RZ caem nos
# indices 2/3, que a Steam le como "alavanca direita" (bug observado).
# Traduzimos: pressionar = 255, soltar = 0.
# Importante: SUPRIMIMOS o evento do botao original para a Steam nao
# enxergar dois inputs conflitantes para o mesmo gatilho (BTN_C/BTN_Z
# seriam enxergados como botoes de face extras caso nao suprimissemos).
TRIGGER_BTN_TO_AXIS = {
    ecodes.BTN_C: ecodes.ABS_BRAKE,  # jstest btn "2" (RT fisico) -> ABS_BRAKE (indice 5 = RT)
    ecodes.BTN_Z: ecodes.ABS_GAS,    # jstest btn "5" (LT fisico) -> ABS_GAS (indice 4 = LT)
}
TRIGGER_PRESSED_VAL = 255
TRIGGER_RELEASED_VAL = 0

EXTRA_BTNS = [ecodes.BTN_START, ecodes.BTN_SELECT]
POLL_TIMEOUT_MS = 1000


def looks_like_shanwan(dev):
    try:
        return dev.info.vendor == VID and dev.info.product == PID
    except Exception:
        return False


def is_virtual(dev):
    return VIRT_NAME_TAG in (dev.name or "")


def is_joystick_node(caps):
    return ecodes.EV_ABS in caps and bool(caps[ecodes.EV_ABS])


def categorize(caps, name):
    """Retorna 'joystick', 'consumer', 'keyboard' ou None."""
    n = (name or "").lower()
    if "consumer control" in n:
        return "consumer"
    if "keyboard" in n or "kbd" in n:
        return "keyboard"
    if "system control" in n:
        return None
    keys = caps.get(ecodes.EV_KEY, [])
    if START_SRC in keys or SELECT_SRC in keys:
        return "consumer"
    if any(k in ARROW_MAP for k in keys):
        return "keyboard"
    return None


def discover():
    joystick = None
    consumer = None
    keyboard = None
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
        if is_joystick_node(caps) and joystick is None:
            joystick = d
            continue
        cat = categorize(caps, d.name)
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


def build_uinput(joystick):
    caps = joystick.capabilities(absinfo=True)
    safe = {t: c for t, c in caps.items()
            if t in (ecodes.EV_KEY, ecodes.EV_ABS, ecodes.EV_MSC)}
    keys = list(safe.setdefault(ecodes.EV_KEY, []))
    for b in EXTRA_BTNS:
        if b not in keys:
            keys.append(b)
    safe[ecodes.EV_KEY] = keys
    abslist = list(safe.setdefault(ecodes.EV_ABS, []))
    abs_codes = {a[0] if isinstance(a, tuple) else a for a in abslist}
    from evdev import AbsInfo
    for code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
        if code not in abs_codes:
            abslist.append((code, AbsInfo(value=0, min=-1, max=1,
                                          fuzz=0, flat=0, resolution=0)))
    safe[ecodes.EV_ABS] = abslist
    ui = UInput(events=safe,
                name=VIRTUAL_NAME,
                vendor=VID,
                product=PID,
                version=0x0111,
                bustype=ecodes.BUS_USB)
    return ui


def safe_grab(dev, label):
    try:
        dev.grab()
        log.info("grabbed %s exclusively (%s)", label, dev.path)
    except Exception as exc:
        log.warning("could not grab %s (%s): %s; "
                    "setas/keys podem chegar ao terminal", label, dev.path, exc)


def handle_key(ui, ev, btn_state):
    if ev.code == START_SRC:
        if btn_state.get(ecodes.BTN_START, 0) != ev.value:
            ui.write(ecodes.EV_KEY, ecodes.BTN_START, ev.value)
            btn_state[ecodes.BTN_START] = ev.value
        return
    if ev.code == SELECT_SRC:
        if btn_state.get(ecodes.BTN_SELECT, 0) != ev.value:
            ui.write(ecodes.EV_KEY, ecodes.BTN_SELECT, ev.value)
            btn_state[ecodes.BTN_SELECT] = ev.value
        return
    if ev.code in ARROW_MAP:
        axis, sign = ARROW_MAP[ev.code]
        val = sign if ev.value else 0
        ui.write(ecodes.EV_ABS, axis, val)


def main():
    log.info("scanning evdev for ShanWan 20bc:5501 nodes")
    joystick = None
    consumer = None
    keyboard = None
    for attempt in range(30):
        joystick, consumer, keyboard = discover()
        if joystick is not None:
            break
        time.sleep(1)
        if attempt % 5 == 4:
            log.info("still waiting for device (attempt %d)", attempt + 1)
    if joystick is None:
        log.error("joystick node (EV_ABS) of ShanWan not found")
        sys.exit(1)
    log.info("joystick node: %s (%s) caps=%s",
             joystick.name, joystick.path,
             sorted(joystick.capabilities().keys()))
    if consumer:
        log.info("consumer node: %s (%s)", consumer.name, consumer.path)
    else:
        log.warning("no consumer-control node; Start/Clear via volume unavailable")
    if keyboard:
        log.info("keyboard node: %s (%s)", keyboard.name, keyboard.path)
    else:
        log.warning("no keyboard node; D-pad arrows unavailable")

    if consumer:
        safe_grab(consumer, "consumer")
    if keyboard:
        safe_grab(keyboard, "keyboard")
    # Grab exclusivo TAMBEM no nó joystick físico: a Steam/SDL leem via
    # evdev (/dev/input/event*) e ignoram a regra udev (que só afeta o
    # joydev /dev/input/js1). Com o grab, nenhum outro processo consegue
    # ler eventos do aparelho original - a Steam so enxerga o merged.
    safe_grab(joystick, "joystick")

    ui = build_uinput(joystick)
    try:
        vpath = ui.device
    except Exception:
        vpath = "?"
    log.info("virtual gamepad created: %s", vpath)

    btn_state = {ecodes.BTN_START: 0, ecodes.BTN_SELECT: 0}

    running = [True]

    def stop(*_):
        running[0] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    poll = select.epoll()
    poll.register(joystick.fd, select.EPOLLIN)
    fd_to_dev = {}
    if consumer:
        poll.register(consumer.fd, select.EPOLLIN)
        fd_to_dev[consumer.fd] = consumer
    if keyboard:
        poll.register(keyboard.fd, select.EPOLLIN)
        fd_to_dev[keyboard.fd] = keyboard

    try:
        while running[0]:
            for fd, _ in poll.poll(timeout=POLL_TIMEOUT_MS):
                if fd == joystick.fd:
                    for ev in joystick.read():
                        if ev.type == ecodes.EV_SYN:
                            ui.syn()
                        elif ev.type == ecodes.EV_KEY and ev.code in TRIGGER_BTN_TO_AXIS:
                            axis = TRIGGER_BTN_TO_AXIS[ev.code]
                            val = TRIGGER_PRESSED_VAL if ev.value else TRIGGER_RELEASED_VAL
                            ui.write(ecodes.EV_ABS, axis, val)
                        else:
                            ui.write(ev.type, ev.code, ev.value)
                else:
                    dev = fd_to_dev.get(fd)
                    if dev is None:
                        continue
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY:
                            handle_key(ui, ev, btn_state)
                    ui.syn()
    except OSError as exc:
        log.warning("device disappeared: %s", exc)
        raise
    finally:
        for fd in [joystick.fd] + list(fd_to_dev.keys()):
            try:
                poll.unregister(fd)
            except Exception:
                pass
        for dev in fd_to_dev.values():
            try:
                dev.ungrab()
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass
        try:
            ui.close()
        except Exception:
            pass
        try:
            joystick.close()
        except Exception:
            pass
        log.info("shutdown complete")


if __name__ == "__main__":
    main()