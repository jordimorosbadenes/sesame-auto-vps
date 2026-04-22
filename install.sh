#!/usr/bin/env bash
# install.sh – Instala sesame_auto en un VPS Debian/Ubuntu
# ─────────────────────────────────────────────────────────────────────────────
# Uso:  sudo bash install.sh
#
# Idempotente: puede ejecutarse varias veces sin problema.
# Añadir un nuevo usuario = crear users/nombre/.env y re-ejecutar install.sh.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/sesame"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║          sesame_auto – instalador multi-usuario      ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Comprobar que hay al menos un usuario configurado ───────────────────────
USERS_DIR="$SCRIPT_DIR/users"
if [ ! -d "$USERS_DIR" ] || [ -z "$(ls -d "$USERS_DIR"/*/.env 2>/dev/null)" ]; then
    echo "❌  No se encontró ningún usuario en $USERS_DIR"
    echo ""
    echo "  Crea al menos un usuario antes de instalar:"
    echo "    mkdir -p users/nombre"
    echo "    cp .env.example users/nombre/.env"
    echo "    nano users/nombre/.env   # rellena SESAME_EMAIL y SESAME_PASSWORD"
    echo ""
    exit 1
fi

# Listar usuarios encontrados
FOUND_USERS=()
for env_file in "$USERS_DIR"/*/.env; do
    username="$(basename "$(dirname "$env_file")")"
    FOUND_USERS+=("$username")
done
echo "→ Usuarios encontrados: ${FOUND_USERS[*]}"
echo ""

# ── 2. Dependencias del sistema ───────────────────────────────────────────────
echo "[ 1/6 ] Instalando dependencias del sistema…"
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv

# Dependencias de sistema que Chromium headless necesita en Debian/Ubuntu
apt-get install -y -q \
  libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
  libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
  libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatspi2.0-0 \
  libgtk-3-0 2>/dev/null || true
echo "   ✔ Dependencias del sistema listas."

# ── 3. Crear directorio de instalación ────────────────────────────────────────
echo "[ 2/6 ] Preparando directorios…"
mkdir -p "$INSTALL_DIR"

# ── 4. Copiar scripts ─────────────────────────────────────────────────────────
echo "[ 3/6 ] Copiando scripts…"
cp "$SCRIPT_DIR/sesame_auto.py"  "$INSTALL_DIR/"
cp "$SCRIPT_DIR/test_fichar.py"  "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/.env.example"    "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/sesame_auto.py" "$INSTALL_DIR/test_fichar.py"
echo "   ✔ Scripts copiados."

# ── 5. Copiar carpetas de usuarios ────────────────────────────────────────────
echo "[ 4/6 ] Copiando usuarios…"
mkdir -p "$INSTALL_DIR/users"
for username in "${FOUND_USERS[@]}"; do
    src="$USERS_DIR/$username"
    dst="$INSTALL_DIR/users/$username"
    mkdir -p "$dst"

    # .env: NO sobreescribir si ya existe (gitignored, se crea una sola vez en el VPS)
    if [ ! -f "$dst/.env" ]; then
        cp "$src/.env" "$dst/.env"
        chmod 600 "$dst/.env"
        echo "   ✔ Usuario '$username': .env creado → $dst/.env"
    else
        echo "   ℹ  Usuario '$username': .env ya existe, no sobreescrito (edita manualmente si es necesario)."
    fi

    # vacaciones.txt: SIEMPRE sobreescribir (está en git, es la fuente autoritativa)
    if [ -f "$src/vacaciones.txt" ]; then
        cp "$src/vacaciones.txt" "$dst/vacaciones.txt"
        echo "   ✔ Usuario '$username': vacaciones.txt actualizado."
    else
        echo "   ⚠  Usuario '$username': no tiene vacaciones.txt (crea uno si es necesario)."
    fi
done

# ── 6. Entorno virtual Python ─────────────────────────────────────────────────
echo "[ 5/6 ] Instalando Python + Playwright…"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
"$VENV_DIR/bin/python" -m playwright install chromium
echo "   ✔ Python y Chromium listos."

# ── 7. Cron jobs (uno por usuario, L-V a las 07:00) ──────────────────────────
echo "[ 6/6 ] Configurando cron jobs…"

# Elimina TODAS las entradas anteriores de sesame_auto para reconstruirlas limpias
# Esto garantiza que si se añade/elimina un usuario, el cron queda siempre correcto
CLEAN_CRONTAB=$(crontab -l 2>/dev/null | grep -v "sesame_auto" || true)

NEW_CRON_LINES=""
for username in "${FOUND_USERS[@]}"; do
    ENV_PATH="$INSTALL_DIR/users/$username/.env"
    CRON_LINE="0 7 * * 1-5 $VENV_DIR/bin/python $INSTALL_DIR/sesame_auto.py --env $ENV_PATH"
    NEW_CRON_LINES="$NEW_CRON_LINES"$'\n'"$CRON_LINE"
    echo "   ✔ Cron '$username': L-V 07:00 → sesame_auto.py --env $ENV_PATH"
done

# Instalar crontab limpio + nuevos jobs
printf "%s\n%s\n" "$CLEAN_CRONTAB" "$NEW_CRON_LINES" | grep -v '^$' | crontab -
echo "   ✔ Cron configurado para ${#FOUND_USERS[@]} usuario(s)."

# ── 8. Permisos finales ───────────────────────────────────────────────────────
chmod 750 "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR/users"
for username in "${FOUND_USERS[@]}"; do
    chmod 750 "$INSTALL_DIR/users/$username"
done

# ── Resumen ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║               ✅  Instalación completada             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Usuarios configurados:"
for username in "${FOUND_USERS[@]}"; do
    echo "  · $username → $INSTALL_DIR/users/$username/.env"
done
echo ""
echo "Cron activo (L-V 07:00):"
crontab -l | grep "sesame_auto" || true
echo ""
echo "Prueba en seco (sin fichar):"
for username in "${FOUND_USERS[@]}"; do
    echo "  SESAME_DRY_RUN=true $VENV_DIR/bin/python $INSTALL_DIR/sesame_auto.py --env $INSTALL_DIR/users/$username/.env"
done
echo ""
echo "Test manual (abre navegador):"
for username in "${FOUND_USERS[@]}"; do
    echo "  $VENV_DIR/bin/python $INSTALL_DIR/test_fichar.py --env $INSTALL_DIR/users/$username/.env"
done
echo ""
echo "Logs en tiempo real:"
for username in "${FOUND_USERS[@]}"; do
    echo "  tail -f $INSTALL_DIR/users/$username/sesame_auto.log"
done
echo ""
echo "Añadir un nuevo usuario en el futuro:"
echo "  mkdir -p $SCRIPT_DIR/users/nuevo"
echo "  cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/users/nuevo/.env"
echo "  nano $SCRIPT_DIR/users/nuevo/.env   # rellena credenciales"
echo "  sudo bash $SCRIPT_DIR/install.sh    # re-ejecutar"
echo ""
