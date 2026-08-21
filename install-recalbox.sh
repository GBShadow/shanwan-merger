#!/bin/sh
# =============================================================================
# install-recalbox.sh - Instala o merger stdlib no Recalbox
#
# Recalbox usa init do BusyBox (nao systemd). O boot roda
# /recalbox/share/system/custom.sh, que e o ponto de entrada.
# Este script:
#   1. Copia recalbox-merger.py e mapping.json para /recalbox/share/system/shanwan/
#   2. Cria / atualiza custom.sh para iniciar o merger no boot
#      (com loop de reinicio - equivalente ao Restart=always)
#   3. (Opcional) adiciona usbcore.quirks ao cmdline do kernel
#
# Como rodar (via SSH, como root):
#   scp recalbox-merger.py mapping.json install-recalbox.sh root@<ip-recalbox>:/tmp/
#   ssh root@<ip-recalbox> 'sh /tmp/install-recalbox.sh'
# =============================================================================
set -e

SHARE_DIR="/recalbox/share/system"
APP_DIR="$SHARE_DIR/shanwan"
MERGER_SRC="$(dirname "$0")/recalbox-merger.py"
MAPPING_SRC="$(dirname "$0")/mapping.json"

echo "==> Instalando merger ShanWan no Recalbox"

if [ ! -d "$SHARE_DIR" ]; then
    echo "ERRO: $SHARE_DIR nao existe. Este script roda dentro do Recalbox (filesystem compartilhado /recalbox/share)." >&2
    exit 1
fi

if [ ! -f "$MERGER_SRC" ]; then
    echo "ERRO: nao encontrei recalbox-merger.py junto deste script." >&2
    exit 1
fi

if [ ! -f "$MAPPING_SRC" ]; then
    echo "ERRO: nao encontrei mapping.json junto deste script." >&2
    exit 1
fi

mkdir -p "$APP_DIR"
cp "$MERGER_SRC" "$APP_DIR/recalbox-merger.py"
chmod +x "$APP_DIR/recalbox-merger.py"
cp "$MAPPING_SRC" "$APP_DIR/mapping.json"
echo "==> Copiado para $APP_DIR/recalbox-merger.py e mapping.json"

# --- custom.sh ---------------------------------------------------------------
# O S99custom do Recalbox executa custom.sh no boot. Criamos/atualizamos
# o arquivo preservando o conteudo existente e adicionando nosso bloco.
CUSTOM="$SHARE_DIR/custom.sh"
touch "$CUSTOM"

if grep -q "shanwan-merger" "$CUSTOM"; then
    echo "==> custom.sh ja contem o merger - bloco atualizado"
else
    echo "" >> "$CUSTOM"
fi

cat >> "$CUSTOM" <<'EOF'
# --- ShanWan merger (instalado por install-recalbox.sh) ---
if [ -x /recalbox/share/system/shanwan/recalbox-merger.py ]; then
    # reinicia o merger se o controle for replugado/removido
    ( while true; do
        /usr/bin/python3 /recalbox/share/system/shanwan/recalbox-merger.py \
            >> /recalbox/share/system/logs/shanwan-merger.log 2>&1
        sleep 2
    done ) &
fi
EOF

chmod +x "$CUSTOM"
echo "==> custom.sh atualizado: $CUSTOM"

# --- quirk usbcore (opcional) --------------------------------------------------
# Sem systemd, o quirk entra na linha de comando do kernel. Verifica se
# /proc/cmdline ja tem; se nao, avisa para editar o arquivo de cmdline
# do boot (varia conforme a placa).
if grep -q "usbcore.quirks=2563:0575:r" /proc/cmdline; then
    echo "==> quirk usbcore.quirks=2563:0575:r ja presente no cmdline (OK)"
else
    echo ""
    echo "==> ATENCAO: adicione 'usbcore.quirks=2563:0575:r' ao cmdline do kernel"
    echo "    (sem systemd nao ha unit de quirk). Locais comuns:"
    echo "      - Raspberry Pi: /boot/cmdline.txt"
    echo "      - PC x86/efi  : /boot/recalbox-cmdline.txt (se existir)"
    echo "    Exemplo (RPi):"
    echo "      # em /boot/cmdline.txt, acrescente ao final da linha unica:"
    echo "      usbcore.quirks=2563:0575:r"
    echo ""
fi

echo "==> Pronto!"
echo "    Reinicie o Recalbox OU inicie agora com:"
echo "      /etc/init.d/S99custom start"
echo "    Log: /recalbox/share/system/logs/shanwan-merger.log"