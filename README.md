# ShanWan Android Gamepad (20bc:5501) — configuração no Linux

Correção completa para o arcade ShanWan que, em modo Android, não
funciona direito no Linux: Start/Clear viram teclas de volume, o D-pad
não é lido como direcional de joystick e os gatilhos LT/RT são
reportados como botões (não como eixos) — a Steam não reconhecia
nada disso e ainda duplicava o controle.

Este projeto resolve tudo com um **merger userspace** (Python + evdev)
que lê os vários nós evdev do aparelho, traduz os eventos e cria um
**joystick virtual único** (`uinput`) que a Steam/SDL reconhecem
corretamente.

## Como funciona

O aparelho em modo Android expõe **3 nós evdev** (todos `20bc:5501`):

| Nó físico            | O que carrega                              |
|----------------------|--------------------------------------------|
| joystick             | faces, alavanca analógica, **LT/RT como botões** |
| Consumer Control     | Start/Clear como `KEY_VOLUMEUP/DOWN`       |
| Keyboard             | D-pad como `KEY_UP/DOWN/LEFT/RIGHT`        |

O `merger.py`:
1. Abre os 3 nós com **grab exclusivo** (`EVIOCGRAB`) — nenhum outro
   processo (Steam incluída) consegue ler o aparelho original;
2. Traduz:
   - `KEY_VOLUMEUP/DOWN` → `BTN_START` / `BTN_SELECT`
   - `KEY_UP/DOWN/LEFT/RIGHT` → `ABS_HAT0X/HAT0Y` (D-pad)
   - `BTN_C` (RT físico) → `ABS_BRAKE` (posição de gatilho direito)
   - `BTN_Z` (LT físico) → `ABS_GAS` (posição de gatilho esquerdo)
3. Cria o joystick virtual `SHANWAN Android Gamepad (merged)`.

> **Por que GAS/BRAKE e não Z/RZ?** A Steam/SDL numeram os eixos **por
> posição (índice)**, não por nome: índice 2/3 = alavanca direita,
> índice 4/5 = gatilhos. `ABS_Z`/`ABS_RZ` caem nos índices 2/3 (a Steam
> via LT/RT como "alavanca direita" — bug já observado). `ABS_GAS`/
> `ABS_BRAKE` caem nos índices 4/5 = LT/RT corretos.

## Requisitos

- Linux com `systemd` (Debian/Ubuntu/MiniOS, **Fedora**, **Arch/Manjaro**)
- `python3` + `python-evdev` (instalado automaticamente pelo setup.sh)
- Kernel com `uinput` (padrão na maioria das distros)
- O controle conectado via USB

> **Compatibilidade do setup.sh**: detecta o gerenciador de pacotes
> automaticamente — `apt-get` (Debian/Ubuntu/MiniOS), `dnf` (Fedora)
> ou `pacman` (Arch/Manjaro) — e instala o pacote evdev correto
> (`python3-evdev` vs `python-evdev`).
>
> **Não suportado**: Recalbox/Batocera e outros sistemas baseados em
> Buildroot — sem apt/dnf/pacman, filesystem read-only e init próprio
> (sem systemd). Para esses, rode o `merger.py` manualmente ou monte
> o boot do próprio sistema.

## Instalação automática (recomendado)

```bash
git clone <este-repo> ~/projects/shanwan-merger   # ou copie a pasta
cd ~/projects/shanwan-merger
sudo ./setup.sh
```

O script (idempotente — pode reexecutar à vontade):
1. Instala `python3-evdev`;
2. Cria a unit `usbcore-shanwan-quirk.service`
   (`usbcore.quirks=2563:0575:r` — segura o aparelho no modo
   Android em vez de piscar para `2563:0575`);
3. Cria a unit `shanwan-merger.service` (daemon do merger,
   `Restart=always`);
4. Cria a regra udev `98-shanwan-hide-physical.rules` (auxiliar);
5. Ativa tudo e mostra a verificação.

Para remover tudo depois:

```bash
sudo ./setup.sh --uninstall
```

## Instalação manual (passo a passo)

```bash
# 1. dependência
sudo apt install -y python3-evdev

# 2. quirk usbcore (unit systemd)
sudo tee /etc/systemd/system/usbcore-shanwan-quirk.service >/dev/null <<'EOF'
[Unit]
Description=Apply usbcore quirk for ShanWan 2563:0575 gamepad
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
echo "2563:0575:r" | sudo tee /sys/module/usbcore/parameters/quirks

# 3. merger (unit systemd) — ajuste o caminho do merger.py
sudo tee /etc/systemd/system/shanwan-merger.service >/dev/null <<EOF
[Unit]
Description=ShanWan Android-mode event merger
DefaultDependencies=no
After=systemd-udev-trigger.service usbcore-shanwan-quirk.service
Wants=usbcore-shanwan-quirk.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/gbshadow/projects/shanwan-merger/merger.py
Restart=always
RestartSec=2
TimeoutStopSec=3
KillSignal=SIGTERM

[Install]
WantedBy=default.target
EOF

# 4. regra udev (auxiliar)
sudo tee /etc/udev/rules.d/98-shanwan-hide-physical.rules >/dev/null <<'EOF'
ACTION=="remove", GOTO="shanwan_hide_end"
SUBSYSTEM=="input", KERNEL=="event*|js*", \
    SUBSYSTEMS=="usb", ATTRS{idVendor}=="20bc", ATTRS{idProduct}=="5501", \
    ENV{ID_INPUT_JOYSTICK}="", \
    ENV{ID_INPUT_KEY}="", \
    ENV{ID_INPUT_KEYBOARD}=""
LABEL="shanwan_hide_end"
EOF

# 5. ativar tudo
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input
sudo systemctl daemon-reload
sudo systemctl enable --now usbcore-shanwan-quirk.service
sudo systemctl enable --now shanwan-merger.service
```

## Testando

```bash
# veja o joystick virtual
for p in /sys/class/input/js*/device; do
  echo "$(basename "$(dirname "$p")") -> $(cat "$p/name")"
done

# teste interativo (selecione o nó "merged"!)
sudo evtest /dev/input/js2        # ou o event* correspondente

# ou com GUI
jstest-gtk                        # IMPORTANTE: escolha /dev/input/js2
```

Resultado esperado no `jstest-gtk` (js2):
- A/B/X/Y (botões 0/1/3/4), LB/RB (6/7), Turbo (12)
- **LT** → eixo **4 (Gas)** varia 0→255
- **RT** → eixo **5 (Brake)** varia 0→255
- D-pad → eixos Hat0X/Hat0Y (-1/0/+1)
- Start/Select respondem

## Steam

1. **Feche a Steam completamente** (menu Steam → Exit; ou `steam -shutdown`)
   e reabra — ela re-enumera os controles e o físico ficará invisível
   (grab do merger);
2. Configurações → Controle: deve aparecer **apenas um** dispositivo
   ("SHANWAN Android Gamepad (merged)");
3. Para configurar bindings, **não use o Wizard** (o botão B/Escape
   cancela a Wizard por design). Use **Browse Layouts → Generic
   X-Input** (ou "Arcade Stick") e edite os binds individualmente;
4. LT/RT aparecem como gatilhos analógicos em jogos.

## Diagnóstico rápido

```bash
# serviço ativo?
systemctl is-active shanwan-merger.service usbcore-shanwan-quirk.service

# log do merger
journalctl -u shanwan-merger.service -b -e

# quem lê o nó físico? (só o merger deve aparecer em /dev/input/eventN do joystick)
sudo fuser -v /dev/input/eventN

# quirk ativo?
cat /sys/module/usbcore/parameters/quirks     # deve conter 2563:0575:r
```

## Limitações conhecidas

- A alavanca analógica é **digital** neste firmware (4 setas, mesmo
  canal do D-pad) — não há eixo analógico real;
- Sem rumble/force feedback;
- Driver de kernel definitivo (`hid-shanwan.c`) foi **compilado mas
  não carrega** neste MiniOS por mismatch de ABI (kernel
  `6.12.57+deb13-amd64` vs headers `mos`); requer instalar o kernel
  mos e rebootar. O merger é a solução que funciona hoje.

## Recalbox / Batocera (Buildroot — sem apt/systemd)

O Recalbox não tem apt/dnf/pacman nem systemd, e o `python-evdev` não
existe no image dele. Para esses sistemas existe o
**`recalbox-merger.py`**: versão do merger usando **apenas a biblioteca
padrão do Python** (os/struct/fcntl/select — sem python-evdev).
Funciona em qualquer Recalbox/Batocera (arquitetura 64-bit ou 32-bit).

**Testado** (2026-08-16): criou o uinput com as mesmas caps, e captura
de LT/RT no virtual confirmou `ABS_GAS`/`ABS_BRAKE` (8x cada).

### Instalação no Recalbox

```bash
# do seu PC, via SSH (recalbox vem com ssh habilitado):
scp recalbox-merger.py install-recalbox.sh root@<ip-do-recalbox>:/tmp/
ssh root@<ip-do-recalbox> 'sh /tmp/install-recalbox.sh'
```

O `install-recalbox.sh`:
1. Copia o merger para `/recalbox/share/system/shanwan/`;
2. Cria/atualiza `/recalbox/share/system/custom.sh` (executado no boot
   pelo `S99custom`) com o merger em loop de reinício automático —
   equivalente ao `Restart=always` do systemd (reinicia sozinho se o
   controle for replugado);
3. Verifica o quirk `usbcore.quirks=2563:0575:r` no cmdline do kernel.

Depois: reinicie o Recalbox (ou `sh /etc/init.d/S99custom start`).
Log: `/recalbox/share/system/logs/shanwan-merger.log`

> **Quirk sem systemd**: o quirk vai na linha de comando do kernel.
> No Raspberry Pi: edite `/boot/cmdline.txt` e acrescente
> `usbcore.quirks=2563:0575:r` ao final da linha única existente.
> Em x86: `/boot/recalbox-cmdline.txt` se existir.

### Rollback no Recalbox

```bash
ssh root@<ip-do-recalbox> 'rm -rf /recalbox/share/system/shanwan && \
  sed -i "/# --- ShanWan merger/,/fi/d" /recalbox/share/system/custom.sh'
```

## Arquivos

| Arquivo                                   | Papel                                  |
|-------------------------------------------|----------------------------------------|
| `merger.py`                               | daemon (une nós evdev → uinput)        |
| `recalbox-merger.py`                      | versão stdlib pura (Recalbox/Batocera) |
| `setup.sh`                                | instala/remove tudo automaticamente    |
| `install-recalbox.sh`                     | instala no Recalbox via custom.sh      |
| `shanwan-merger.service`                  | unit systemd do daemon                 |
| `STATUS.md`                               | histórico completo do projeto          |

## Rollback manual

```bash
sudo systemctl disable --now shanwan-merger.service usbcore-shanwan-quirk.service
sudo rm /etc/systemd/system/shanwan-merger.service \
        /etc/systemd/system/usbcore-shanwan-quirk.service \
        /etc/udev/rules.d/98-shanwan-hide-physical.rules
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
# remova a entrada 2563:0575:r do quirk se desejar:
sudo sh -c 'echo "" > /sys/module/usbcore/parameters/quirks'
```