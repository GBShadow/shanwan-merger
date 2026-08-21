# ShanWan Android Gamepad (20bc:5501) — Emulação Xbox 360 no Linux

Correção completa para o arcade ShanWan que, em modo Android, não
funciona direito no Linux: Start/Clear viram teclas de volume, o D-pad
vem como setas de teclado (nó separado), e os gatilhos LT/RT são
reportados como botões de face — Steam e emuladores não reconheciam
nada disso, viam múltiplos dispositivos duplicados, e ainda cruzavam
os nomes dos botões.

Este projeto resolve tudo com um **merger userspace** (Python + evdev)
que lê os 3 nós físicos do aparelho, traduz os eventos segundo um
mapeamento configurável, e cria um **joystick virtual único** (`uinput`)
que se identifica como um **Xbox 360 Controller genuíno** — reconhecido
nativamente por SDL2, Steam, RetroArch e qualquer emulador/jogo Linux,
sem precisar de nenhum arquivo de mapeamento externo (GameControllerDB).

## Índice

- [Especificações do Hardware](#especificações-do-hardware)
- [Como funciona](#como-funciona)
- [Requisitos](#requisitos)
- [Instalação automática (recomendado)](#instalação-automática-recomendado)
- [Remapeando os botões (`remap.py`)](#remapeando-os-botões-remappy)
- [Turbo e Clear (repetição automática de botão)](#turbo-e-clear-repetição-automática-de-botão)
- [Testando](#testando)
- [Steam, AntiMicroX e SDL2](#steam-antimicrox-e-sdl2)
- [Diagnóstico rápido](#diagnóstico-rápido)
- [Limitações conhecidas](#limitações-conhecidas)
- [Recalbox e Batocera (sistemas Buildroot, sem apt ou systemd)](#recalbox-e-batocera-sistemas-buildroot-sem-apt-ou-systemd)
  - [Instalação no Recalbox](#instalação-no-recalbox)
  - [Instalação direto no cartão/HD do Recalbox (offline)](#instalação-direto-no-cartãohd-do-recalbox-offline)
  - [Remapeando direto no Recalbox (`remap-recalbox.py`)](#remapeando-direto-no-recalbox-remap-recalboxpy)
  - [Rollback no Recalbox](#rollback-no-recalbox)
- [Arquivos](#arquivos)
- [Rollback manual (systemd)](#rollback-manual-systemd)

## Especificações do Hardware

### Identificação USB

| Propriedade          | Valor                                                |
|-----------------------|-------------------------------------------------------|
| Fabricante             | ShenZhen ShanWan Technology Co., Ltd.                 |
| Nome USB               | `Android Gamepad`                                     |
| VID:PID (modo Android, **efetivo**) | `20bc:5501`                              |
| VID:PID (modo "real", **enumera e cai**) | `2563:0575` ("PS3/PC Gamepad")      |
| Barramento             | USB 2.0 (`Bus=0003` nos nós evdev)                    |
| `lsusb`                | `Bus 002 Device 0XX: ID 20bc:5501 ShenZhen ShanWan Technology Co., Ltd. Android Gamepad` |

**Por que existe o quirk `usbcore.quirks=2563:0575:r`?** Ao plugar, o
firmware primeiro enumera como `2563:0575` ("modo real", com descritor
HID correto), mas o kernel Linux rejeita o pedido de descritor de
configuração (pede só 9 bytes; o firmware devolve o descritor inteiro)
e o aparelho **cai** ~126 ms depois para o fallback `20bc:5501` ("modo
Android", com Start/Clear/D-pad quebrados). A flag `r`
(`USB_QUIRK_WINDOWS_CONFIG_REQ_SIZE`) faz o Linux pedir o descritor com
255 bytes, como o Windows faz — isso evita a queda inicial, mas o
firmware ainda entrega um descritor HID malformado em `2563:0575`, então
o `hid-generic` acaba rejeitando de qualquer forma e o aparelho recai em
`20bc:5501`. **A quirk continua sendo aplicada** (inofensiva, e mantém a
porta aberta para uma solução futura via driver de kernel dedicado), mas
quem resolve o problema **hoje** é o merger userspace, operando
inteiramente em modo Android (`20bc:5501`).

### Os 3 nós evdev do aparelho (modo Android)

| Nó (nome evdev)                              | Handlers típicos | Conteúdo                                                  |
|------------------------------------------------|-------------------|------------------------------------------------------------|
| `SHANWAN Android Gamepad`                       | `eventN js0`      | botões de face/ombro/gatilho/Select/Mode/Turbo (`EV_KEY`), eixos declarados mas inertes (`EV_ABS`) |
| `SHANWAN Android Gamepad Consumer Control`      | `kbd eventN`       | Start/Clear como `KEY_VOLUMEUP`/`KEY_VOLUMEDOWN`           |
| `SHANWAN Android Gamepad Keyboard`              | `sysrq kbd leds eventN` | D-pad como `KEY_UP`/`KEY_DOWN`/`KEY_LEFT`/`KEY_RIGHT`  |
| `SHANWAN Android Gamepad System Control`        | `kbd eventN`       | não usado pelo merger (ignorado)                           |

### Tabela completa de mapeamento físico (papel Xbox 360)

Valores confirmados por captura direta de hardware (evento por evento).
Esta é a configuração padrão de fábrica em `mapping.json` — se você
remapear com `remap.py`/`remap-recalbox.py`, os códigos podem mudar,
mas a estrutura (papel → alvo Xbox) permanece a mesma.

| Papel (Xbox 360) | Botão físico no arcade | Nó de origem | Código evdev | Nome no kernel        | Alvo no virtual                  |
|-------------------|--------------------------|---------------|----------------|--------------------------|-------------------------------------|
| `A`                 | X (face)                   | joystick      | `308`           | `BTN_WEST` / `BTN_Y`      | `BTN_SOUTH`                          |
| `B`                 | Bola/Círculo (face)        | joystick      | `309`           | `BTN_Z`                   | `BTN_EAST`                           |
| `X`                 | Quadrado (face)            | joystick      | `305`           | `BTN_B` / `BTN_EAST`      | `BTN_NORTH`                          |
| `Y`                 | Triângulo (face)           | joystick      | `306`           | `BTN_C`                   | `BTN_WEST`                           |
| `LB`                | L1 (ombro esquerdo)        | joystick      | `304`           | `BTN_A` / `BTN_SOUTH`     | `BTN_TL`                             |
| `RB`                | R1 (ombro direito)         | joystick      | `311`           | `BTN_TR`                  | `BTN_TR`                             |
| `LT`                | L2 (gatilho esquerdo)      | joystick      | `307`           | `BTN_NORTH` / `BTN_X`     | `ABS_Z` (eixo 0–255, sem botão digital) |
| `RT`                | R2 (gatilho direito)       | joystick      | `310`           | `BTN_TL`                  | `ABS_RZ` (eixo 0–255, sem botão digital) |
| `SELECT`            | Select                     | joystick      | `314`           | `BTN_SELECT`              | `BTN_SELECT`                         |
| `START`             | Start                      | **consumer**  | `115`           | `KEY_VOLUMEUP`            | `BTN_START`                          |
| `MODE`              | Mode                       | joystick      | `315`           | `BTN_START` *(alias de kernel — não confundir com o papel `START`)* | `BTN_MODE` (Guide/Home) |
| `TURBO`             | Turbo                      | joystick      | `316`           | `BTN_MODE` *(alias de kernel)* | modificador — não vira botão   |
| `CLEAR`             | Clear                      | **consumer**  | `114`           | `KEY_VOLUMEDOWN`          | modificador — não vira botão         |
| D-pad ↑/↓/←/→       | Direcional (cruz)          | **keyboard**  | `103/108/105/106` | `KEY_UP/DOWN/LEFT/RIGHT` | `ABS_HAT0Y`(∓1)/`ABS_HAT0X`(∓1) *(fixo, fora do remapeamento)* |

> **Nota sobre aliases confusos:** os nomes de constante do kernel Linux
> para os botões de face são só *aliases* posicionais (`BTN_A`==`BTN_SOUTH`==`BTN_GAMEPAD`,
> `BTN_Y`==`BTN_WEST`, etc.) — não correspondem à letra serigrafada no
> botão físico. Por isso a tabela acima lista **três** colunas: o nome
> impresso no arcade, o código numérico real, e o(s) nome(s) de
> constante do kernel — só o código numérico importa para o mapeamento.

## Como funciona

O aparelho em modo Android expõe **3 nós evdev** (todos `20bc:5501`),
detalhados na seção [Especificações do Hardware](#especificações-do-hardware)
acima.

O `merger.py`:

1. Abre os 3 nós com **grab exclusivo** (`EVIOCGRAB`) — nenhum outro
   processo (Steam incluída) consegue ler o aparelho físico original;
2. Carrega `mapping.json` (código físico → papel Xbox: `A B X Y LB RB
   LT RT SELECT START MODE TURBO CLEAR`);
3. Traduz cada papel para o código correto no dispositivo virtual:
   - botões de face/ombro/select/start/mode → `EV_KEY` padrão Xbox
     (`BTN_SOUTH/EAST/NORTH/WEST/TL/TR/SELECT/START/MODE`)
   - **LT/RT → eixos analógicos puros** (`ABS_Z`/`ABS_RZ`, 0–255) —
     igual ao Xbox 360 real, sem botão digital duplicado
   - D-pad → `ABS_HAT0X`/`ABS_HAT0Y`
4. Cria o joystick virtual **`Xbox 360 Controller`** com VID/PID
   `045E:028E` (Microsoft) e a mesma ordem de capacidades do driver
   `xpad` real — é isso que faz o SDL2 reconhecer tudo nativamente.

> **Por que emular um Xbox 360 e não manter o VID/PID do ShanWan?**
> SDL2 identifica controles por GUID (derivado de VID/PID/versão +, em
> versões recentes, um CRC do nome do dispositivo) e mapeia botões por
> **índice posicional**, não por nome de código evdev. Reaproveitar o
> VID/PID do ShanWan físico fazia o SDL aplicar entradas antigas da
> GameControllerDB com ordem de botões incompatível — resultado:
> LB acendia como Y, LT acendia LB+RT juntos, etc. Emular o Xbox 360
> real (VID/PID + ordem de capacidades idênticos ao driver `xpad`)
> ativa o reconhecimento **nativo embutido** do SDL2, sem depender de
> nenhum arquivo de banco de dados externo. Validado com hardware real
> via chamadas diretas à libSDL2 (`SDL_GameControllerGetButton/GetAxis`).

## Requisitos

- Linux com `systemd` (Debian/Ubuntu/MiniOS, **Fedora**, **Arch/Manjaro**)
- `python3` + `python-evdev` (instalado automaticamente pelo `setup.sh`)
- Kernel com `uinput` (padrão na maioria das distros)
- O controle conectado via USB

> **Compatibilidade do setup.sh**: detecta o gerenciador de pacotes
> automaticamente — `apt-get` (Debian/Ubuntu/MiniOS), `dnf` (Fedora)
> ou `pacman` (Arch/Manjaro) — e instala o pacote evdev correto
> (`python3-evdev` vs `python-evdev`).
>
> **Não suportado pelo setup.sh**: Recalbox/Batocera e outros sistemas
> Buildroot — sem apt/dnf/pacman e sem systemd. Use o
> `install-recalbox.sh` (seção dedicada abaixo).

## Instalação automática (recomendado)

```bash
git clone https://github.com/GBShadow/shanwan-merger.git
cd shanwan-merger
sudo ./setup.sh
```

O script (idempotente — pode reexecutar à vontade):
1. Instala `python3-evdev`;
2. Cria a unit `usbcore-shanwan-quirk.service`
   (`usbcore.quirks=2563:0575:r` — ver explicação em
   [Especificações do Hardware](#especificações-do-hardware));
3. Cria a unit `shanwan-merger.service` (daemon do merger,
   `Restart=always`), apontando para `merger.py` **na pasta onde você
   clonou o repositório**;
4. Cria a regra udev `98-shanwan-hide-physical.rules` (auxiliar);
5. Ativa tudo e mostra a verificação.

Para remover tudo depois:

```bash
sudo ./setup.sh --uninstall
```

## Remapeando os botões (`remap.py`)

Se o layout físico não bater com o esperado (ex.: LB soando como outro
botão), **não é preciso editar código**. Use a ferramenta interativa:

```bash
# Remapear TODOS os 13 papéis, um de cada vez, na ordem:
# A B X Y LB RB LT RT SELECT START MODE TURBO CLEAR
sudo python3 remap.py

# Remapear só alguns papéis específicos, nessa ordem
sudo python3 remap.py Y RB LT

# Ver o mapeamento atual sem alterar nada
sudo python3 remap.py --list
```

O que a ferramenta faz:

1. Para o `shanwan-merger.service` (evita conflito de grab exclusivo);
2. Abre e trava os 3 nós físicos do controle;
3. Para cada papel pedido, mostra o nome e espera você **pressionar
   uma vez** o botão físico correspondente;
4. Salva `mapping.json` **incrementalmente** — se você cancelar com
   `Ctrl+C` no meio, o progresso já feito não se perde;
5. Reinicia o serviço automaticamente ao terminar.

O arquivo gerado (`mapping.json`) fica na raiz do projeto e é lido por
`merger.py`/`recalbox-merger.py` a cada início do serviço:

```json
{
  "A":      {"device": "joystick", "code": 308},
  "B":      {"device": "joystick", "code": 309},
  "X":      {"device": "joystick", "code": 305},
  "Y":      {"device": "joystick", "code": 306},
  "LB":     {"device": "joystick", "code": 304},
  "RB":     {"device": "joystick", "code": 311},
  "LT":     {"device": "joystick", "code": 307},
  "RT":     {"device": "joystick", "code": 310},
  "SELECT": {"device": "joystick", "code": 314},
  "START":  {"device": "consumer", "code": 115},
  "MODE":   {"device": "joystick", "code": 315},
  "TURBO":  {"device": "joystick", "code": 316},
  "CLEAR":  {"device": "consumer", "code": 114}
}
```

> O D-pad (nó "Keyboard", `KEY_UP/DOWN/LEFT/RIGHT`) **não** faz parte
> do remapeamento — é estrutural e permanece fixo.

## Turbo e Clear (repetição automática de botão)

O controle tem dois botões físicos dedicados a esse recurso (mapeados
nos papéis `TURBO` e `CLEAR` em `mapping.json`):

- **Ativar turbo:** segure **Turbo** e, ao mesmo tempo, pressione o
  botão que você quer que repita sozinho (ex.: X). A partir daí,
  segurar esse botão no jogo dispara cliques automáticos a **~16 Hz**
  (a cada ~35 ms) enquanto estiver pressionado.
- **Desativar turbo de um botão:** segure **Clear** e pressione o
  botão que tem turbo ativo.
- **Limpar todos os turbos de uma vez:** segure **Clear sozinho** por
  mais de **1,5 segundo**.

O estado é persistido em `turbo_state.json` (na pasta do projeto) e
sobrevive a reinícios do serviço/PC.

## Testando

```bash
# veja o joystick virtual (agora aparece como "Xbox 360 Controller")
for p in /sys/class/input/js*/device; do
  echo "$(basename "$(dirname "$p")") -> $(cat "$p/name")"
done

# teste interativo — escolha o node cujo nome é "Xbox 360 Controller"
sudo evtest /dev/input/jsN

# ou com GUI
jstest-gtk                        # escolha o "Xbox 360 Controller"
```

Resultado esperado (layout Xbox 360 padrão):
- A/B/X/Y, LB/RB, Back/Start, Guide (mode) — todos como botões digitais
- **LT/RT** → eixos analógicos puros (`lefttrigger`/`righttrigger`),
  0 solto → máximo pressionado, **sem** botão duplicado
- D-pad → hat (Hat0X/Hat0Y, -1/0/+1)

## Steam, AntiMicroX e SDL2

Como o dispositivo virtual se identifica como um Xbox 360 Controller
genuíno, **nenhuma configuração adicional é necessária**:

1. Feche o app completamente e reabra (ele precisa reenumerar
   controles) — o físico fica invisível (grab exclusivo do merger),
   só o virtual aparece;
2. Deve aparecer como **"Xbox 360 Controller"**, reconhecido
   automaticamente com o layout correto (Steam Input, AntiMicroX,
   qualquer jogo/emulador via SDL2).

## Diagnóstico rápido

```bash
# serviço ativo?
systemctl is-active shanwan-merger.service usbcore-shanwan-quirk.service

# log do merger
journalctl -u shanwan-merger.service -b -e

# ver o mapeamento carregado
sudo python3 remap.py --list

# quirk ativo?
cat /sys/module/usbcore/parameters/quirks     # deve conter 2563:0575:r
```

## Limitações conhecidas

- A alavanca analógica é **digital** neste firmware (vem pelo mesmo
  canal do D-pad) — não há eixo analógico real de stick;
- Sem rumble/force feedback (Xbox 360 real tem; o virtual não emite);
- Driver de kernel definitivo (`hid-shanwan.c`) foi tentado na máquina
  original (MiniOS) mas bloqueado por mismatch de ABI — ver
  `STATUS.md` §3.6 para detalhes. O merger userspace é a solução
  usada em produção.

## Recalbox e Batocera (sistemas Buildroot, sem apt ou systemd)

O Recalbox não tem apt/dnf/pacman nem systemd, e o `python-evdev` não
existe na imagem dele. Para esses sistemas existe o
**`recalbox-merger.py`**: versão do merger usando **apenas a biblioteca
padrão do Python** (os/struct/fcntl/select/json — sem python-evdev).
Usa o **mesmo `mapping.json`** e a mesma emulação de Xbox 360 Controller
do `merger.py`. Funciona em qualquer Recalbox/Batocera (64-bit ou 32-bit).

### Instalação no Recalbox

Com o Recalbox **rodando** (via SSH — vem com SSH habilitado por padrão):

```bash
# do seu PC:
scp recalbox-merger.py mapping.json remap-recalbox.py install-recalbox.sh root@<ip-do-recalbox>:/tmp/
ssh root@<ip-do-recalbox> 'sh /tmp/install-recalbox.sh'
```

O `install-recalbox.sh`:
1. Copia `recalbox-merger.py`, `mapping.json` **e `remap-recalbox.py`**
   para `/recalbox/share/system/shanwan/`;
2. Cria/atualiza `/recalbox/share/system/custom.sh` (executado no boot
   pelo `S99custom`) com o merger em loop de reinício automático —
   equivalente ao `Restart=always` do systemd;
3. Verifica o quirk `usbcore.quirks=2563:0575:r` no cmdline do kernel.

Depois: reinicie o Recalbox (ou `sh /etc/init.d/S99custom start`).
Log: `/recalbox/share/system/logs/shanwan-merger.log`

### Instalação direto no cartão/HD do Recalbox (offline)

Se o Recalbox está numa mídia removível (cartão SD ou HD/SSD USB) que
você pode montar em outro PC Linux, dá para instalar **sem precisar
ligar o Recalbox nem usar SSH**:

1. Conecte a mídia — o Linux vai montar automaticamente algo como
   `/run/media/<user>/RECALBOX` (partição de boot, FAT32) e
   `/run/media/<user>/SHARE` (partição de dados, exFAT/ext4).
2. Copie os arquivos do merger direto para dentro da partição `SHARE`:
   ```bash
   SHARE=/run/media/$USER/SHARE
   mkdir -p "$SHARE/system/shanwan"
   cp recalbox-merger.py mapping.json remap-recalbox.py "$SHARE/system/shanwan/"
   chmod +x "$SHARE/system/shanwan/recalbox-merger.py" "$SHARE/system/shanwan/remap-recalbox.py"
   ```
3. Crie (ou acrescente ao já existente) `$SHARE/system/custom.sh`:
   ```bash
   cat >> "$SHARE/system/custom.sh" <<'EOF'
   # --- ShanWan merger (instalado manualmente) ---
   if [ -x /recalbox/share/system/shanwan/recalbox-merger.py ]; then
       ( while true; do
           /usr/bin/python3 /recalbox/share/system/shanwan/recalbox-merger.py \
               >> /recalbox/share/system/logs/shanwan-merger.log 2>&1
           sleep 2
       done ) &
   fi
   EOF
   chmod +x "$SHARE/system/custom.sh"
   ```
4. Adicione o quirk USB à linha de kernel na partição de **boot**
   (`RECALBOX`). Em instalações **x86/PC** (BIOS legado *e* UEFI usam
   GRUB), edite **os dois** arquivos, acrescentando
   `usbcore.quirks=2563:0575:r` ao final de cada linha `linux ...`:
   ```bash
   BOOT=/run/media/$USER/RECALBOX
   sed -i 's/\(linux .*\)$/\1 usbcore.quirks=2563:0575:r/' \
       "$BOOT/boot/grub/grub.cfg" "$BOOT/EFI/BOOT/grub.cfg"
   ```
   Em **Raspberry Pi**, o cmdline fica em `$BOOT/cmdline.txt` (uma
   única linha) — acrescente `usbcore.quirks=2563:0575:r` ao final dela.
5. `sync` e desmonte/ejete a mídia com segurança antes de remover.

> `boot.md5` (checksums de integridade do boot) só é verificado durante
> **atualizações OTA** do Recalbox, não no boot normal — a edição acima
> não é revertida ao ligar. Porém, se você atualizar o sistema depois,
> reaplique o quirk nos `grub.cfg` (a atualização sobrescreve o boot).

### Remapeando direto no Recalbox (`remap-recalbox.py`)

Não é preciso outra máquina Linux — o `remap-recalbox.py` é a versão
**stdlib pura** do `remap.py` (mesma interface, sem depender de
`python-evdev`) e roda direto no Recalbox via SSH:

```bash
ssh root@<ip-do-recalbox>
cd /recalbox/share/system/shanwan

# remapear TODOS os 13 papéis, um de cada vez
python3 remap-recalbox.py

# remapear só alguns papéis específicos
python3 remap-recalbox.py Y RB LT

# ver o mapeamento atual
python3 remap-recalbox.py --list
```

O script mata o `recalbox-merger.py` em execução, trava os 3 nós físicos,
captura os botões pedidos e salva em `mapping.json`. Não precisa reiniciar
o Recalbox nem o serviço manualmente — o loop do `custom.sh` respawna o
merger sozinho em até 2 segundos, já com o mapeamento atualizado.

### Rollback no Recalbox

```bash
ssh root@<ip-do-recalbox> 'rm -rf /recalbox/share/system/shanwan && \
  sed -i "/# --- ShanWan merger/,/fi/d" /recalbox/share/system/custom.sh'
```

## Arquivos

| Arquivo                  | Papel                                              |
|---------------------------|----------------------------------------------------|
| `merger.py`                | daemon principal (evdev), emula Xbox 360 Controller, lê `mapping.json` |
| `recalbox-merger.py`       | mesma lógica em stdlib puro (Recalbox/Batocera)     |
| `mapping.json`             | mapeamento físico→papel; editável via `remap.py` (Fedora/Debian/Arch) ou `remap-recalbox.py` (Recalbox) |
| `remap.py`                 | ferramenta interativa de remapeamento (requer python-evdev) |
| `remap-recalbox.py`        | mesma ferramenta em stdlib puro (Recalbox/Batocera) |
| `turbo_state.json`         | estado dos botões com turbo ativo (gerado em runtime, git-ignorado) |
| `setup.sh`                 | instala/remove tudo automaticamente (systemd)       |
| `install-recalbox.sh`      | instala no Recalbox via `custom.sh`                 |
| `shanwan-merger.service`   | unit systemd do daemon                              |
| `STATUS.md`                | histórico técnico completo do projeto (diagnóstico, decisões) |

## Rollback manual (systemd)

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
