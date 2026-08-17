# Projeto ShanWan 20bc:5501 — Status Final e Continuidade

Controle arcade USB **ShenZhen ShanWan Technology Co., Ltd. Android Gamepad**
(`lsusb`: `20bc:5501`). No Linux ficava preso no modo "Android": os botões
**Start** e **Clear/Select** eram reportados como `KEY_VOLUMEUP` /
`KEY_VOLUMEDOWN` por um nó evdev separado ("Consumer Control"), enquanto o
joystick (faces/dpad/axes) vivia em outro nó. Emuladores e jogos não
conseguiam usar Start/Select. O D-pad virava setas de teclado em outro nó
("Keyboard"), o que fazia o cursor pular no terminal e nem aparecia
no `jstest-gtk`.

**Status atual:** `usbcore.quirks=2563:0575:r` (persistida via systemd)
+ daemon Python `shanwan-merger.service` (active + enabled) fundendo os 3
nós (joystick + consumer + keyboard) em `/dev/input/event24` / `js2` com
`BTN_START/BTN_SELECT` e `ABS_HAT0X/HAT0Y` corretos. O controle **está
usável hoje** via `js2`. Driver de kernel `hid-shanwan.c` foi tentado mas
bloqueado por mismatch ABI do MiniOS (ver §3.6).

Sistema: **MiniOS live** (Debian 13 trixie), kernel **6.12.57+deb13-amd64**,
usuario `gbshadow`, maquina fisica HP PresarioCQ18.

---

## 1. Diagnóstico (confirmado)

- `dmesg` mostrou que ao plugar o controle:
  1. enumera como `2563:0575` "PS3/PC Gamepad" (modo "real"),
  2. ~126 ms depois **se desconecta e reenumera como `20bc:5501` "Android
     Gamepad"** (modo fallback).
- Causa-raiz: bug clássico de gamepads multi-modo ShanWan (documentado na
  Arch Wiki §Gamepad#ShanWan). O firmware retorna o descritor de
  configuração USB inteiro quando o host pede só 9 bytes → host Linux
  rejeita → firmware cai no fallback.
- No modo Android a interface 1.1 do HID expõe 3 sub-devs (System Control,
  **Consumer Control**, Keyboard). Start/Clear viram volumee keys no nó
  Consumer Control (`event22`), **separado** do nó joystick (`event20`).
- `xpad` (kernel stock) **não** tem alias para `20bc:5501` (só BETOP
  `5134/514a` num modo XInput FF/5D diferente) — descartado.
- `hid-sony` (kernel stock) tem o rdesc sixaxis correto para "SHANWAN PS3
  GamePad", mas a `sony_devices[]` **não** tem vendor `0x2563` e
  `sony_probe` só ativa o quirk `SHANWAN_GAMEPAD` se `hdev->name ==
  "SHANWAN PS3 GamePad"`; o seu reporta `"PS3/PC Gamepad"` → não roteia.
  Mesmo se forçasse o bind, `sony_report_fixup` só patcheia bytes, não
  substitui o rdesc com "unbalanced collection" (erro -22).
- Conclusão: só config/quirk no kernel stock **não** resolve. É preciso
  either merger em userspace (feito) ou driver de kernel custom (futuro).

---

## 2. O que já está feito e funcionando (Estágios 0, 1 e 3)

### 2.1 Quirk USB `usbcore.quirks=2563:0575:r`

- **O que faz:** `r` = `USB_QUIRK_WINDOWS_CONFIG_REQ_SIZE` faz o Linux pedir
  o descritor de config com 255 bytes (como o Windows), evitando o erro
  que derrubava o `2563:0575`. Resultado: o modo "real" `2563:0575` agora
  **fica em pé** ao plugar (antes caía por falha de config-descriptor).
  Nota: o firmware deste aparelho ainda entrega um descritor HID
  malformado em `2563:0575`, então o `hid-generic` ainda rejeita e ele
  eventualmente cai em `20bc:5501` — mas a quirk permanece útil e
  inofensiva, e essencial para qualquer solução futura de driver.
- **Persistência:** systemd unit
  `/etc/systemd/system/usbcore-shanwan-quirk.service` (enabled, active).
  Aplica `echo "2563:0575:r" > /sys/module/usbcore/parameters/quirks` a
  cada boot, antes de qualquer gamepad ser plugado.
- **Estado atual:** `cat /sys/module/usbcore/parameters/quirks` →
  `2563:0575:r`. Service: `active` + `enabled`.

### 2.2 Remoção da regra udev quebrada

- `/etc/udev/rules.d/99-njp308.rules` (tinha typo `ATTRS{idVendor}=="20b"`
  faltando `c`) foi renomeada para `.bak`. Mesmo corrigida ela só mudava
  classificação do udev, não remapeava códigos — não resolvia Start/Clear.

### 2.3 Merger userspace (joystick virtual unificado) — FUNCIONANDO

Projeto: `/home/gbshadow/projects/shanwan-merger/`

Arquivos:
- `merger.py` — script Python (~200 linhas) com `evdev` + `uinput`.
- `shanwan-merger.service` — unit systemd copiada para
  `/etc/systemd/system/shanwan-merger.service` (enabled, active).

Lógica (versão atual, pós-correção do D-pad):
1. Acha o nó evdev com `EV_ABS` do `20bc:5501` → nó "joystick"
   (atualmente `event20`).
2. Acha o nó cujo nome contém "Consumer Control" do mesmo VID/PID
   (atualmente `event22`) — fonte de Start/Clear.
3. Acha o nó cujo nome contém "Keyboard" do mesmo VID/PID
   (atualmente `event23`) — **fonte do D-pad/alavanca** (ver seção 2.5).
4. Cria um `UInput` virtual "SHANWAN Android Gamepad (merged)" clonando
   caps `EV_KEY` + `EV_ABS` + `EV_MSC` do joystick, adicionando
   `BTN_START` + `BTN_SELECT` (filtra `EV_SYN` extra que causava
   `EINVAL`) e garantindo `ABS_HAT0X` + `ABS_HAT0Y` declarados
   (`AbsInfo min=-1 max=1`).
5. Abre os nós **consumer** e **keyboard** com `EVIOCGRAB` (grab
   exclusivo) — impede que os eventos desses nós cheguem ao
   TTY/XSession (previne setas fantasmas no terminal e OSD de volume).
6. Loop `epoll`:
   - do nó joystick: encaminha 1:1 todos eventos (faces, analogicos,
     gatilhos), com `ui.syn()` em cada EV_SYN;
   - do nó consumer e keyboard: traduz as teclas (ver mapeamento abaixo)
     e faz `ui.syn()` no final do report;
   - estado de botão deduplicado (nao reemite mesmo valor).
7. `Restart=on-failure` — se o cabo desplugar/replugar o processo cai
   (fd vira inválido) e o systemd reinicia em 2s, reencontrando os nós
   (que podem mudar de número).

Traducoes aplicadas no virtual pelo merger:
| No origem        | Codigo HID                 | Saida no virtual            |
|------------------|----------------------------|-----------------------------|
| `event22` (cons) | `KEY_VOLUMEUP`             | `EV_KEY/BTN_START`          |
| `event22` (cons) | `KEY_VOLUMEDOWN`           | `EV_KEY/BTN_SELECT`         |
| `event23` (kbd)  | `KEY_UP` / `KEY_DOWN`      | `EV_ABS/ABS_HAT0Y` (-1/+1)  |
| `event23` (kbd)  | `KEY_LEFT` / `KEY_RIGHT`   | `EV_ABS/ABS_HAT0X` (-1/+1)  |
| `event20` (joy)  | (todos `BTN_*` e `ABS_*`)  | encaminhado 1:1             |

Saída atual (`/proc/bus/input/devices`):
```
I: Bus=0003 Vendor=20bc Product=5501 Version=0111
N: Name="SHANWAN Android Gamepad (merged)"
H: Handlers=event24 js2
B: EV=20001b
B: KEY=7fff000000000000 0 0 0 0
B: ABS=30627
```

### 2.4 Problema residual descoberto: D-pad não reconhecido pelo jstest-gtk

**Sintoma reportado:** o direcional (D-pad em cruz) e/ou a alavanca
analógica não apareciam como eixos no `jstest-gtk`. Pior: no terminal
do opencode, mexer no direcional fazia o cursor pular como se fossem
as **setas direcionais do teclado** (`KEY_UP/KEY_DOWN/KEY_LEFT/KEY_RIGHT`).

**Diagnóstico (confirmado via `evtest` + dump de caps):** o D-pad deste
aparelho em modo Android **NÃO é emitido no nó joystick** (o que tem
`EV_ABS`, ex.: `event19`/`event20`) — esse nó só declara `BTN_*` +
`ABS_X/Y/Z/RZ/GAS/BRAKE/ABS_HAT0X/Y` em caps mas na prática não
produz eventos para o hat. Em vez disso, o firmware reporta o D-pad
como **teclas de seta** no nó "Keyboard" (interface USB 1.1, ex.:
`event22`/`event23`), que declara `KEY_UP/KEY_DOWN/KEY_LEFT/
KEY_RIGHT` (junto com `KEY_VOLUMEUP/DOWN` duplicados e todo o mapa de
teclado) no bitmap de caps.

Por isso:
- `jstest-gtk` não enxergava: ele só lê `EV_ABS/BTN_*` do nó joystick
  selecionado; as setas chegam em outro nó evdev que ele nem abre.
- O terminal recebia as setas: o `event23` estava sem `EVIOCGRAB`, então
  o Xorg/libinput/tratava como teclado normal → cursor pulava.

A alavanca analógica é um caso à parte: ainda não capturamos eventos
dela em nenhum dos 4 nós durante os testes rápidos. Há duas hipóteses a
validar na fase final (ver §3.1.1):
  (a) ela é emitida no `event20` como `ABS_X/Y` (eventos que chegam ao
      virtual mas o `jstest-gtk` estava apontando para o `js1` físico
      antigo, não o `js2` merged) — confirmar rodando o `jstest-gtk`
      apontado para `/dev/input/js2`;
  (b) ela também virou teclas de seta no `event23` (firmware barato
      digitaliza a alavanca em 4 direções) — confirmar capturando o
      `event23` enquanto se move só a alavanca.

**Correção aplicada (no merger, versão atual):**

1. O merger agora **também abre o nó "Keyboard"** (interface 1.1, ex.:
   `event22`/`event23`, identificado por `name =~ "Keyboard"`) (não
   mais o ignora) e faz `EVIOCGRAB` exclusivo nele + no nó "Consumer
   Control" (ex.: `event21`/`event22`, identificado por `name =~
   "Consumer Control"`). Com o grab, o terminal/X paran de receber
   as setas e os volume keys → sem efeitos colaterais.
2. Tradução de teclas aplicada no loop do merger:
   - `KEY_UP    → ABS_HAT0Y = -1`
   - `KEY_DOWN  → ABS_HAT0Y = +1`
   - `KEY_LEFT  → ABS_HAT0X = -1`
   - `KEY_RIGHT → ABS_HAT0X = +1`
   (valor `0` quando a tecla é liberada, para volver o hat ao centro).
3. O `UInput` virtual agora declara explicitamente `ABS_HAT0X` e
   `ABS_HAT0Y` (`AbsInfo min=-1 max=1`) caso o nó joystick físico não
   tivesse (no seu caso já tinha, mas ficou defendido a futuras
   revisões de firmware).
4. Estado de botões deduplicado (`btn_state`) para não reemitir o mesmo
   valor — evita flapping.

**Verificação feita (snapshot pós-replug natural, números atuais):**
- Nó joystick: `event19`; nó consumer: `event21` (grabbed, errno 16);
  nó keyboard: `event22` (grabbed, errno 16); virtual: `event24`/`js2`.
- Tentar `dev.grab()` num segundo processo contra os nós consumer e
  keyboard retorna `errno 16 EBUSY` — confirma que o merger detém o
  grab exclusivo (setas/volume não chegam mais ao TTY).
- `evtest /dev/input/event24` confirma caps com `ABS_HAT0X/ABS_HAT0Y` +
  `BTN_START/BTN_SELECT` + `ABS_X/Y/RZ`.
- `grep -B1 -A6 "merged" /proc/bus/input/devices` mostra
  `Handlers=event24 js2`.

> **Nota sobre rebind/replug:** se o cabo USB for desplugar/replugar,
> os números `eventNN` dos nós físicos mudam (já observado: `event20`
> virou `event19`, `event22` virou `event21`, etc.). O merger reinicia
> (`Restart=on-failure`) e reencontra os nós pelo **nome** + VID/PID,
> não pelo número, então é robusto. O virtual merger tende a aparecer
> como `event24`/`js2` mas o número dele também depende de ordem de
> registro — sempre confirme via `grep "merged" /proc/bus/input/devices`.

**Pendência de validação (usuário):** rodar `jstest-gtk` apontando para
**`/dev/input/js2`** (o virtual merged, **não** `js1` que é o físico
antigo) e confirmar que ao mexer no D-pad os `Axes 16/17 (HAT0X/HAT0Y)`
se movem. Para a alavanca analógica, ver §3.1.1.

O controle **usável hoje** em jogos/emuladores via `/dev/input/event24`
(ou `js2`). Start/Clear funcionam como botões normais.

### 2.5 AntiMicroX (rede parcial, opcional) — INSTALADO

- AppImage `AntiMicroX-x86_64.AppImage` **3.6.1** (40592576 bytes)
  baixado e validado em `/home/gbshadow/AppImages/`. Comando:
  ```bash
  ~/AppImages/AntiMicroX-x86_64.AppImage --version
  # ⇒ antimicrox 3.6.1
  ```
- **Atenção:** antimicrox mapeia gamepad→teclado/mouse; **não** gera
  joystick virtual. Logo **não** substitui o merger. Útil só se quiser
  bind de botão para teclas atalho em apps que não leem joystick.
- Para executar (dependências Qt5/SDL2 já presentes):
  ```bash
  ~/AppImages/AntiMicroX-x86_64.AppImage
  ```

### 2.6 Melhorias para compatibilidade com Steam (Estágio 3.5)

Sintomas reportados ao usar o controle na Steam:
1. A Steam enxergava **dois** controles ao mesmo tempo (`js1` físico
   + `js2` virtual) → entradas duplicadas.
2. Os gatilhos "2 e 5" (no jstest-gtk) não respondiam no teste de input
   da Steam.
3. Ao apertar B durante a configuração de inputs da Steam, saía da
   tela de binding — isso é **comportamento esperado** da Steam: B
   cancela a Wizard.

#### 2.6.1 Mapeamento físico confirmado por captura com timestamp
Captura via `evtest /dev/input/event19` com janelas cronometradas
(`001_long_phys.log` etc). Mapeamento definitivo:

| Botão físico        | evdev code    | jstest idx | Código symlinks                |
|---------------------|---------------|------------|--------------------------------|
| A (face)            | 304 (BTN_SOUTH) | 0         | BTN_GAMEPAD / BTN_A            |
| B (face)            | 305 (BTN_EAST)  | 1         | BTN_B                          |
| X (face)            | 307 (BTN_NORTH) | 3         | BTN_X                          |
| Y (face)            | 308 (BTN_WEST)  | 4         | BTN_Y                          |
| RB (ombro dir)      | 311 (BTN_TR)    | 7         | —                              |
| LB (ombro esq)      | 310 (BTN_TL)    | 6         | —                              |
| **RT (gatilho dir)**| **306 (BTN_C)** | **2**     | (jstest mostra "2")            |
| **LT (gatilho esq)**| **309 (BTN_Z)** | **5**     | (jstest mostra "5")           |
| Turbo               | 316 (BTN_MODE)  | 12        | (vira BTN_MODE no evdev)       |
| Select              | 314 (BTN_SELECT)| 10        | (no nó físico)                 |
| Start               | 315 (BTN_START) | 11         | (no nó físico)                 |
| Mode                | (não confirmado)| ?         | veio junto/no event22          |
| Clear               | (não confirmado)| ?         | ver via event22                |
| D-pad (4 direções)  | (via event22)   | -         | KEY_UP/DOWN/LEFT/RIGHT         |

**Importante**: o firmware mapeia os **gatilhos LT/RT como botões**
(BTN_Z=LT, BTN_C=RT), **não** como eixos analógicos. `ABS_GAS/BRAKE`
são declarados em caps mas o firmware nunca os preenche (valor ficou
em 0 em todos os testes).

#### 2.6.2 Tradução BTN→Eixo no merger (correção FINAL, revisão 2)
**Descoberta crítica**: a Steam/SDL enumeram os eixos **POR POSIÇÃO
(índice)**, não por nome. O layout genérico que a Steam Input usa é:
- índice 0 = `ABS_X` → left stick X
- índice 1 = `ABS_Y` → left stick Y
- índice 2 = `ABS_Z` → right stick X  (dummy, nunca emite)
- índice 3 = `ABS_RZ` → right stick Y  (dummy, nunca emite)
- índice 4 = `ABS_GAS` → **LT** (gatilho esquerdo)
- índice 5 = `ABS_BRAKE` → **RT** (gatilho direito)

Como o aparelho não tem nada analógico nesses gatilhos, traduzimos o
botão digital para eixo com valor 255 (pressionado) e 0 (soltado):

```python
# Código atual no merger.py (correção final da inversão e da posição)
TRIGGER_BTN_TO_AXIS = {
    ecodes.BTN_C: ecodes.ABS_BRAKE,  # jstest btn "2" (RT fisico) -> ABS_BRAKE (indice 5 = RT)
    ecodes.BTN_Z: ecodes.ABS_GAS,    # jstest btn "5" (LT fisico) -> ABS_GAS (indice 4 = LT)
}
TRIGGER_PRESSED_VAL  = 255
TRIGGER_RELEASED_VAL = 0
```
No loop forward do nó joystick:
```python
elif ev.type == ecodes.EV_KEY and ev.code in TRIGGER_BTN_TO_AXIS:
    axis = TRIGGER_BTN_TO_AXIS[ev.code]
    val = TRIGGER_PRESSED_VAL if ev.value else TRIGGER_RELEASED_VAL
    ui.write(ecodes.EV_ABS, axis, val)
    # SUPRIME o BTN_C/BTN_Z original (nao vai para o virtual)
```
Resultado: o virtual só emite `ABS_GAS`/`ABS_BRAKE` (sem `BTN_C`/
`BTN_Z`), exatamente nas posições 4/5 que a Steam lê como LT/RT.
`ABS_Z`/`ABS_RZ` permanecem em caps mas nunca emitem (dummy, mantém o
índice alinhado). O antigo `TRIGGER_AXIS_MAP` (ABS_GAS→ABS_Z) foi
**removido** — traduzir para Z/RZ fazia os gatilhos caírem nas
posições 2/3 = "alavanca direita" na Steam.

> **Histórico de bugs corrigidos**:
> 1. Revisão 1: `BTN_C → ABS_Z` (LT) e `BTN_Z → ABS_RZ` (RT),
>    invertendo LT/RT. A captura cronometrada revelou de facto
>    **RT = BTN_C** e **LT = BTN_Z**. Invertido em 2026-08-16 20:25.
> 2. Revisão 1.1 (ABS_Z/ABS_RZ): embora o jstest-gtk mostrasse os
>    eixos 2/3 (Z/Rz) respondendo, a Steam os lia como "alavanca
>    direita" (posições 2/3). Corrigido usando ABS_GAS/ABS_BRAKE
>    (posições 4/5) em 2026-08-16 21:05 — confirmado pela Steam.

#### 2.6.3 Validação confirmada (final)
Captura via `evtest /dev/input/event22` (virtual) durante ~4min,
usuário pressionando LT e RT alternados várias vezes:
```
$ grep -oE "code [0-9]+ \(ABS_[A-Z_]+\)" /tmp/lt_rt_bg.log | sort | uniq -c
    42 code 10 (ABS_BRAKE)   <- RT fisico -> ABS_BRAKE (indice 5 = RT)
    37 code 9  (ABS_GAS)     <- LT fisico -> ABS_GAS (indice 4 = LT)
```
`BTN_C`/`BTN_Z` no virtual: **ausentes** (suprimidos corretamente).
**Usuário confirmou na Steam em 2026-08-16: LT/RT reconhecidos
corretamente como gatilhos (funcionou).**

Portanto no `jstest-gtk` em `/dev/input/js2`:
- pressionar LT físico → axis **4 (Gas)** muda de 0 → máximo
- pressionar RT físico → axis **5 (Brake)** muda de 0 → máximo

#### 2.6.4 Duplicação na Steam — resolvida (grab do joystick físico)
**Sintoma**: mesmo com a regra udev escondendo `js1`, a Steam ainda
mostrava o controle original + o merged.

**Causa**: a Steam Input lê os dispositivos via **evdev**
(`/dev/input/event*`) e hidraw, não via joydev (`/dev/input/js*`).
A regra udev só afeta o joydev, então a Steam continuava abrindo o
nó evdev físico (`event4`).

**Correção**: o merger agora faz `EVIOCGRAB` (grab exclusivo) TAMBÉM
no nó joystick físico — nenhum outro processo consegue ler eventos
dele; a Steam só enxerga o virtual merged:
```python
# merger.py — após abrir os nós:
safe_grab(joystick, "joystick")   # <-- adicionado (era só consumer/keyboard)
```
Log de confirmação:
```
grabbed joystick exclusively (/dev/input/event4)
```
E `udevadm trigger --subsystem-match=input` + reinício da Steam
(fechar 100%, incl. tray) para re-enumerar.

**Validado**: `fuser -v /dev/input/event4` mostra apenas `python3`
(merger); a Steam (PID steam) aparece somente em `event22` (virtual).
Usuário confirmou: **apenas um controle aparece na Steam agora.**

#### 2.6.5 Regra udev escondendo `js1` físico (auxiliar)
Criado `/etc/udev/rules.d/98-shanwan-hide-physical.rules`:
```
ACTION=="remove", GOTO="shanwan_hide_end"
SUBSYSTEM=="input", KERNEL=="event*|js*", \
    SUBSYSTEMS=="usb", ATTRS{idVendor}=="20bc", ATTRS{idProduct}=="5501", \
    ENV{ID_INPUT_JOYSTICK}="", \
    ENV{ID_INPUT_KEY}="", \
    ENV{ID_INPUT_KEYBOARD}=""
LABEL="shanwan_hide_end"
```
Validado:
```
$ udevadm info /dev/input/js1 | grep ID_INPUT_JOYSTICK
$                          # ^ vazio: Steam ignora js1
$ udevadm info /dev/input/js2 | grep ID_INPUT_JOYSTICK
E: ID_INPUT_JOYSTICK=1    # ^ Steam vê só js2 (virtual merged)
```
Nota: sozinha ela NÃO resolveu a duplicação (a Steam lê evdev
direto) — o grab do joystick (§2.6.4) é o que resolve de fato.

#### 2.6.6 Observouções sobre o "B sai da config"
Steam Input: durante a Wizard de binding, B/Back/Escape **sempre**
cancela — é design-choice. Para configurar sem usar o Wizard:
1. Steam → Settings → Controller → clique no ...merged...
2. Clique em **"Browse Layouts"** (NÃO em "Begin Setup"/Wizard)
3. Escolha um template "Generic X-Input" ou "Arcade Stick"
4. Edite bindings individualmente clicando neles — esse modo aceita B

#### 2.6.7 Bug de restart (já corrigido)
Antes, `Restart=on-failure` na unit não reiniciava o merger quando o
dispositivo era desplugar-replugar (o script saía com code 0). Trocado
para `Restart=always` e `raise` no `except OSError`. Hoje o merger
reinicia sozinho em ~2s após desplugar/replugar.
```ini
# /etc/systemd/system/shanwan-merger.service (linhas relevantes)
Restart=always
RestartSec=2
```

---

## 3. Pendências e próximas etapas (Estágio 2 / driver de kernel)

Ordem recomendada para a **Fase 2** (driver de kernel, fix definitivo
que substitui o merger):

### 3.1 Validar o merger em uso real — CONCLUÍDO em 2026-08-16
✅ jstest-gtk em `js2`: faces, dpad, Start/Clear, LB/RB e alavanca OK.
✅ LT/RT: captura evtest confirmou `ABS_GAS`/`ABS_BRAKE` (42x/37x);
   **usuário confirmou na Steam que os gatilhos funcionam.**
✅ Duplicação: resolvida com grab do joystick (§2.6.4) — apenas um
   controle aparece na Steam.
✅ Mapeamento de botões completo validado por captura cronometrada
   (ver §2.6.1).

#### 3.1.1 Validar especificamente a alavanca analógica
Nos testes rápidos de capture (~10s) ainda não registramos eventos de
movimento da alavanca analógica em nenhum dos 4 nós. Há duas hipóteses:
- **(a) Ela é emitida em `event20` como `ABS_X`/`ABS_Y`** mas o
  `jstest-gtk` estava apontando para `js1` (físico) em vez de `js2`
  (virtual). Confirmar rodando `jstest-gtk` ou `evtest` em
  `/dev/input/js2` / `/dev/input/event24` e mexer só a alavanca — se
  os `Axis 0 (ABS_X)` / `Axis 1 (ABS_Y)` variarem, está OK e só era
  desvio do `js1`.
- **(b) O firmware digitaliza a alavanca em 4 setas** e reclama no
  `event23` (`KEY_UP/DOWN/LEFT/RIGHT`) — o mesmo canal do D-pad. Nesse
  caso a alavanca também fica "sempre parada ou pulando entre -1/0/+1"
  no virtual. Se for isso, a alavanca é efetivamente só digital neste
  firmware (não há eixo analógico real); registrar como limitação ou
  pegar o rdesc do modo `2563:0575` para liberar analógico real via
  driver de kernel.

Procedimento:
```bash
# MEXA só na alavanca analógica (esquerda) por ~6s durante o comando:
timeout 6 sudo evtest /dev/input/event24 | grep -E "ABS_X|ABS_Y"
# Se nada variar, capturar o event23 enquanto mexe só na alavanca:
timeout 6 sudo evtest /dev/input/event23 | grep -E "KEY_UP|KEY_DOWN|KEY_LEFT|KEY_RIGHT"
```

### 3.2 Capturar o descritor HID real do `2563:0575`
Importante para escrever o `report_fixup` do driver. Caminhos:
- `sudo modprobe usbmon` + capture com `usbmon` no barramento do gamepad
  durante a janela curta em que `2563:0575` está em pé (antes de cair em
  fallback). Arquiva o descritor HID.
- OU rodar `usbhid-dump -d 2563:0575` imediatamente após plugar.
- Comparar com o `sixaxis_rdesc` que já existe no `hid-sony.c` (kernel
  tem template pronto).

### 3.3 Escolher a via do driver de kernel

**Opção A (preferida, upstreamável): patchear `hid-sony.c`**
- Adicionar entrada `{ HID_USB_DEVICE(USB_VENDOR_ID_SHANWAN,
  USB_DEVICE_ID_SHANWAN_0575) }` na `sony_devices[]` (define novos IDs
  em `hid-ids.h`).
- Ampliar a checagem em `sony_probe` para também reconhecer o nome
  `"PS3/PC Gamepad"` (além de `"SHANWAN PS3 GamePad"`).
- Possivelmente estender `sony_report_fixup` para substituir o rdesc
  inteiro quando `SHANWAN_GAMEPAD` (como já faz para `motion`/`ps3remote`).
- Vantagem: zero daemon, funciona em todas as camadas, upstream aceita.

**Opção B (autônoma): driver fora-da-árvore `hid-shanwan.c` + DKMS**
- `drivers/hid/`-style: `hid-shanwan.c` declara
  `HID_USB_DEVICE(0x20bc, 0x5501)` e `{0x2563, 0x0575}` (tratar os dois
  IDs); `report_fixup` substitui o rdesc sixaxis; `input_mapping`
  roteia usages para `BTN_START/BTN_SELECT/...` (modelado em `hid-dr.c`).
- Compilar via DKMS contra `linux-headers-$(uname -r)`. Install no boot.
- Vantagem: independente de upstream aceitar patch.

**Comparar com base:** estudar `drivers/hid/hid-dr.c` (DragonRise) e
`hid-sony.c` `motion_fixup`/`ps3remote_fixup` como templates para
`report_fixup` que troca o rdesc por inteiro.

### 3.4 Persistência extra (opcional, MiniOS)
- Se houver camada de persistência ativa (cmdline mostra
  `perchdir=resume`), confirmar se `/etc/systemd/system/*.service`
  sobrevivem a reboot e se o DKMS (futuro) também.
- Adicionar `usbcore.quirks=2563:0575:r` à cmdline do boot do MiniOS
  como redundância caso a unit systemd não dispare cedo o suficiente
  (procurar o bootloader/syslinux.cfg/grub.cfg do live).

### 3.5 Rumble / force feedback (desejável)
- No modo Android o firmware **não** expõe rumble — só ficará disponível
  no modo "real" (`2563:0575`) quando o driver de kernel conseguir
  mantê-lo em pé via `report_fixup`. Templates: `hid-betopff.c` (mesmo
  vendor `0x20bc`) e o `SONY_FF` do `hid-sony.c`.

### 3.6 Estágio 2 — TENTADO e BLOQUEADO por mismatch ABI do ambiente

Foi implementado o esqueleto `hid-shanwan.c` (probe-only com dump do
rdesc em dmesg) em `/home/gbshadow/projects/hid-shanwan/`:

Arquivos:
- `hid-shanwan.c` — driver minimal que casa `HID_USB_DEVICE(0x2563, 0x0575)`,
  usa `report_fixup` para `print_hex_dump()` do rdesc bruto no kernel log
  (diagnóstico) e `probe` retorna `-ENODEV` para device cair no fallback.
- `Makefile` — Kbuild out-of-tree; alvo `force-load` para insmod.

Resultado da compilação (`make`):
```
hid-shanwan.ko: 10576 bytes, ELF 64-bit LSB relocatable, x86-64
```

**Bloqueio encontrado ao carregar:**
```
$ sudo modprobe --force-vermagic --force-modversion hid-shanwan
modprobe: ERROR: could not insert 'hid_shanwan': Exec format error

$ sudo dmesg | grep shanwan
hid_shanwan: disagrees about version of symbol module_layout
hid_shanwan: module_layout: kernel tainted.
hid_shanwan: loading out-of-tree module taints kernel.
hid_shanwan: module verification failed: signature and/or required key missing
```

**Causa:** o kernel em execução é `6.12.57+deb13-amd64` (Debian stock,
conforme `/proc/version`: `debian-kernel@lists.debian.org`, Debian
14.2.0-19 binutils, build 2025-11-05), mas os únicos headers
disponíveis nos repos MiniOS são `linux-headers-6.12.57-mos-amd64`
(`6.12.57-mos-1`, do repositório `http://deb.minios.dev/debian
generic`). O CRC de `module_layout` (e outros símbolos exportados)
diferem entre os dois builds → o kernel rejeita o módulo mesmo com
`--force-modversion`. Não há `linux-headers-6.12.57+deb13-amd64`
disponível (a Debian trixie passou para 6.12.86+); nem há `vmlinux`
debug instalado que permita extrair os CRCs reais do kernel em execução
para sintetizar um `Module.symvers` compatível.

**Para destravar (futuro)**, é necessário que headers E kernel binário
sejam da mesma compilação. Três caminhos, em ordem de preferência:

1. **(Recomendado) Instalar kernel mos + reboot:**
   ```bash
   sudo apt-get install -y linux-image-6.12.57-mos-amd64
   # Verificar qual bootloader o MiniOS usa (syslinux.cfg / grub / isolinux)
   ls /boot/*.cfg /live/*.cfg /minios/boot/ 2>/dev/null
   # Atualizar entrada de boot para apontar para vmlinuz-6.12.57-mos-amd64
   # Rebootar
   # Após reboot: uname -r deve mostrar 6.12.57-mos-amd64
   # Então: cd ~/projects/hid-shanwan && make && sudo insmod hid-shanwan.ko
   # Replugar o controle
   # Capturar rdesc dump: sudo dmesg | grep -A30 'shanwan-rdesc'
   # Decodificar rdesc e escrever o report_fixup/input_mapping reais
   ```
   Risco: môi ambiente live com `perchdir=resume` — confirmar que
   bootloader suporta troca de kernel; fazer backup da entrada atual.

2. **Obter headers exatos da Debian:** baixar
   `linux-headers-6.12.57+deb13-amd64` (não mais em trixie; checar em
   snapshot.debian.org) e instalar manualmente, depois reconstruir o
   `hid-shanwan.ko`. Sem reboot.

3. **Migrar para MiniOS stock:** se futuras versões do MiniOS voltarem
   a usar kernel mos como execução padrão, o problema desaparece.

**Lembrete de design do `hid-shanwan.c` final** (não implementado por
falta do rdesc decifrado): usar `report_fixup` para (a) adicionar bytes
`0xC0` extras ao fim até balancear as collections (correção mínima),
ou (b) subsituir o rdesc por um pré-compilado estilo sixaxis (template
do `hid-sony.c` motion_rdesc/ps3remote_rdesc como referência).
`input_mapping` para rotear usages Consumer.Volume → `BTN_START`/
`BTN_SELECT`, e usages Generic Desktop.Hat → `ABS_HAT0X`/`HAT0Y`.
Para rumble: herdar `CONFIG_SONY_FF` template ou `hid-betopff.c`.

**Resumo:** sem reboot/headers-matching, o driver de kernel **não
avança** no ambiente atual. O merger em userspace (`shanwan-merger.service`)
continua sendo a solução operacional completa.

---

## 4. Comandos úteis para retomar

```bash
# Estado atual da quirk
cat /sys/module/usbcore/parameters/quirks
# Esperado: 2563:0575:r

# Estado do merger
sudo systemctl status shanwan-merger.service
sudo journalctl -u shanwan-merger.service -n 40

# Reiniciar o merger (depois de desplugar/replugar)
sudo systemctl restart shanwan-merger.service

# Confirmar joystick virtual
grep -A8 'merged' /proc/bus/input/devices
# /dev/input/eventX ou jsY em Handlers

# Testar botoes (instalar se faltar: sudo apt-get install evtest)
sudo evtest /dev/input/event24   # pressionar Start, Clear, faces...

# Testar SDL2 GUID
sdl2-jstest --list               # ver "SHANWAN Android Gamepad (merged)"

# Capture do descritor HID do modo real (futuro driver)
sudo modprobe usbmon
sudo usbhid-dump -d 2563:0575    # ja plugado, rapido
# OU capture completa: cat /sys/kernel/debug/usb/usbmon/1u > /tmp/usb.cap

# Reverter/remover TUDO se precisar
sudo systemctl disable --now shanwan-merger.service usbcore-shanwan-quirk.service
sudo rm /etc/systemd/system/shanwan-merger.service /etc/systemd/system/usbcore-shanwan-quirk.service
sudo systemctl daemon-reload
# O .bak da regra udev antiga está em /etc/udev/rules.d/99-njp308.rules.bak
```

## 5. Mapa de nós evdev (numeros mudam entre reboots/replugs)

| Papel              | Interface USB | Nome (chave usado pelo merger)   | Tratamento merger        |
|--------------------|---------------|-----------------------------------|--------------------------|
| joystick (faces/axes) | 1.0        | "SHANWAN Android Gamepad"         | encaminhado 1:1          |
| System Control     | 1.1           | "…System Control"                 | (ignorado)               |
| Consumer Control   | 1.1           | "…Consumer Control"               | grab + traduz p/ BTN_*   |
| Keyboard (D-pad)   | 1.1           | "…Keyboard"                       | **grab + traduz p/ ABS_HAT** |
| Virtual merged     | virtual       | "SHANWAN Android Gamepad (merged)" | criado via `uinput`    |

Snapshot atual (pós-replug, 2026-08-16 21:00):
- joystick fisico: `event4` (grabbed); System Control: `event19`;
  consumer: `event20` (grabbed); keyboard: `event21` (grabbed);
  virtual merged: `event22` / `js2`.

> Os números `eventNN` são dinâmicos — `merger.py` localiza por
> VID/PID (20bc:5501) + nome, sem hardcode. Sempre confirme pelo
> comando:
> ```bash
> grep "merged" /proc/bus/input/devices           # qual event/js do virtual
> for p in /sys/class/input/js*/device; do printf "%s -> %s\n" "$(basename $(dirname $p))" "$(cat $p/name)"; done
> ```

> **Importante para o `jstest-gtk`:** selecione **`/dev/input/js2`**
> (o virtual "merged") ao testar. Selecionar `js1` (físico antigo) faz
> você ver faces/analogicos sem Start/Clear/D-pad — exatamente o
> sintoma reportado.

---

## 6. Resumo operacional de uma linha

> usbcore quirk `2563:0575:r` (persistida) + daemon Python
> `shanwan-merger.service` funde joystick+consumer+keyboard em
> joystick virtual "merged" com `BTN_START`/`BTN_SELECT`, D-pad
> (`ABS_HAT0X/HAT0Y`) e gatilhos `ABS_GAS`/`ABS_BRAKE` (LT/RT)
> corretos, com grab exclusivo em TODOS os nós físicos (joystick,
> consumer, keyboard) → **controle usável na Steam: um único
> dispositivo, gatilhos reconhecidos** (validado 2026-08-16). Driver
> de kernel `hid-shanwan.c` (Estágio 2) **tentado e bloqueado por
> mismatch ABI do MiniOS** (kernel `+deb13` vs headers `mos`);
> resolver exige instalar kernel mos e rebootar (ver §3.6).
>
> **No `jstest-gtk`, selecione `/dev/input/js2` (o virtual merged).**

---

## 7. Mapa final de arquivos / instalações

| Caminho                                              | Tipo          | Estado                              |
|-----------------------------------------------------|---------------|-------------------------------------|
| `/home/gbshadow/projects/shanwan-merger/merger.py`  | Python script | funcionando (281 linhas)            |
| `/etc/systemd/system/shanwan-merger.service`        | systemd unit  | active + enabled                    |
| `/etc/systemd/system/usbcore-shanwan-quirk.service` | systemd unit  | active + enabled                    |
| `/home/gbshadow/projects/hid-shanwan/`              | módulo kernel | compilado, **não carrega** (ABI)    |
| `/usr/src/linux-headers-6.12.57-mos-amd64/`         | headers       | instalado (mos, mismatch com kernel)|
| `/lib/modules/$(uname -r)/build`                    | symlink       | -> `/usr/src/linux-headers-...mos-amd64` |
| `/etc/udev/rules.d/99-njp308.rules.bak`             | backup        | regra quebrada (typo `20b`)         |
| `/home/gbshadow/AppImages/AntiMicroX-x86_64.AppImage` | AppImage    | **instalado** (3.6.1, 40592576 bytes) |
| `/home/gbshadow/projects/shanwan-merger/README.md` | documentação  | guia de configuração do zero        |
| `/home/gbshadow/projects/shanwan-merger/setup.sh`  | script        | **instala/remove tudo** (`install`/`uninstall`) |
| `/home/gbshadow/projects/shanwan-merger/STATUS.md`  | este documento | —                                  |

## 8. Rollback completo (remover tudo)

```bash
# AUTOMATICO (recomendado):
sudo /home/gbshadow/projects/shanwan-merger/setup.sh --uninstall

# MANUAL (equivalente):
# Parar e desabilitar serviços
sudo systemctl disable --now shanwan-merger.service usbcore-shanwan-quirk.service
sudo rm /etc/systemd/system/shanwan-merger.service /etc/systemd/system/usbcore-shanwan-quirk.service
sudo systemctl daemon-reload

# Remover quirk (cinza: inofensiva mesmo sem merger)
echo "" | sudo tee /sys/module/usbcore/parameters/quirks

# Restaurar regra udev original (nao recomendado - corrigir typo se necessario)
sudo mv /etc/udev/rules.d/99-njp308.rules.bak /etc/udev/rules.d/99-njp308.rules
sudo sed -i 's/=="20b"/=="20bc"/' /etc/udev/rules.d/99-njp308.rules

# Remover modulo compilado (nao em memoria)
sudo rm /lib/modules/$(uname -r)/extra/hid-shanwan.ko
sudo rm -f /lib/modules/$(uname -r)/build   # remove symlink pos-build
sudo rmdir /lib/modules/$(uname -r)/extra 2>/dev/null || true
sudo /sbin/depmod -a 2>/dev/null

# Restaurar controle ao estado nativo (Android mode)
# Replugar o USB apos rollback. Controle voltara a apresentar o bug
# de Start/Clear como volume keys (sintoma original).
```