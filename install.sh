#!/usr/bin/env bash
# install.sh – Instala sesame_auto en un VPS Debian/Ubuntu
# ─────────────────────────────────────────────────────────────────────────────
# Uso:  bash install.sh
#
# Para actualizar tras un git pull:
#   cd ~/sesame-auto-vps && git pull && bash install.sh
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
echo "   ✔ Dependencias del sistema listas."

# ── 3. Crear directorio de instalación ────────────────────────────────────────
echo "[ 2/6 ] Preparando directorios…"
mkdir -p "$INSTALL_DIR"

# ── 4. Copiar scripts ─────────────────────────────────────────────────────────
echo "[ 3/6 ] Copiando scripts…"
cp "$SCRIPT_DIR/sesame_auto.py"        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/update_vacaciones.py"  "$INSTALL_DIR/"
cp "$SCRIPT_DIR/test_fichar.py"        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt"      "$INSTALL_DIR/"
cp "$SCRIPT_DIR/.env.example"          "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/sesame_auto.py" "$INSTALL_DIR/update_vacaciones.py" "$INSTALL_DIR/test_fichar.py"
echo "   ✔ Scripts copiados."

# ── 5. Copiar carpetas de usuarios ────────────────────────────────────────────
echo "[ 4/6 ] Copiando usuarios…"
mkdir -p "$INSTALL_DIR/users"
for username in "${FOUND_USERS[@]}"; do
    src="$USERS_DIR/$username"
    dst="$INSTALL_DIR/users/$username"
    mkdir -p "$dst"

    # Copiar solo el .env (no sobreescribir state.json, sesiones, etc. si ya existen)
    cp "$src/.env" "$dst/.env"
    chmod 600 "$dst/.env"
    echo "   ✔ Usuario '$username': .env copiado → $dst/.env"

    # Copiar vacaciones.txt si existe (no sobreescribir si ya hay uno en destino)
    if [ -f "$src/vacaciones.txt" ] && [ ! -f "$dst/vacaciones.txt" ]; then
        cp "$src/vacaciones.txt" "$dst/vacaciones.txt"
        echo "   ✔ Usuario '$username': vacaciones.txt copiado → $dst/vacaciones.txt"
    elif [ -f "$src/vacaciones.txt" ]; then
        echo "   ℹ  Usuario '$username': vacaciones.txt ya existe en destino, no sobreescrito."
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
# Instala las dependencias de sistema que Chromium necesita (detecta la distro automáticamente)
"$VENV_DIR/bin/python" -m playwright install-deps chromium
echo "   ✔ Python y Chromium listos."

# ── 7. Cron jobs (uno por usuario, L-V a las 07:00) ──────────────────────────
echo "[ 6/6 ] Configurando cron jobs…"

# Elimina TODAS las entradas anteriores para reconstruirlas limpias
# Esto garantiza que si se añade/elimina un usuario, el cron queda siempre correcto
CLEAN_CRONTAB=$(crontab -l 2>/dev/null | grep -v "sesame_auto" | grep -v "update_vacaciones" | grep -v "^TZ=Europe/Madrid" || true)

NEW_CRON_LINES=""
# La línea TZ= en crontab fija la zona horaria para todos los jobs siguientes.
# Sin esto, cron usa UTC del servidor y el script ficharía 2h tarde en verano.
NEW_CRON_LINES=$'\nTZ=Europe/Madrid'
for username in "${FOUND_USERS[@]}"; do
    ENV_PATH="$INSTALL_DIR/users/$username/.env"
    LOG_AUTO="$INSTALL_DIR/users/$username/sesame_auto.log"
    LOG_VAC="$INSTALL_DIR/users/$username/update_vacaciones.log"
    # Fichaje diario: lunes a viernes a las 05:30
    CRON_LINE="30 5 * * 1-5 $VENV_DIR/bin/python $INSTALL_DIR/sesame_auto.py --env $ENV_PATH >> $LOG_AUTO 2>&1"
    NEW_CRON_LINES="$NEW_CRON_LINES"$'\n'"$CRON_LINE"
    echo "   ✔ Cron '$username': L-V 05:30 Madrid → sesame_auto.py (log: $LOG_AUTO)"
    # Actualización diaria de vacaciones: cada día a las 05:00, antes del fichaje de las 05:30
    CRON_VAC="0 5 * * * $VENV_DIR/bin/python $INSTALL_DIR/update_vacaciones.py --env $ENV_PATH >> $LOG_VAC 2>&1"
    NEW_CRON_LINES="$NEW_CRON_LINES"$'\n'"$CRON_VAC"
    echo "   ✔ Cron '$username': diario 05:00 → update_vacaciones.py (log: $LOG_VAC)"
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
echo "Cron activo (L-V 05:30):"
crontab -l | grep "sesame_auto" || true
echo ""
echo "Actualizar vacaciones.txt desde Sesame iCal (ejecutar tras instalar, luego automático cada día a las 05:00):"
for username in "${FOUND_USERS[@]}"; do
    echo "  $VENV_DIR/bin/python $INSTALL_DIR/update_vacaciones.py --env $INSTALL_DIR/users/$username/.env"
done
echo ""
echo "  O bien con un iCal ya descargado de Sesame → Mis vacaciones → Exportar iCal:"
for username in "${FOUND_USERS[@]}"; do
    echo "  $VENV_DIR/bin/python $INSTALL_DIR/update_vacaciones.py --env $INSTALL_DIR/users/$username/.env --ical /ruta/Sesame-Calendar.ics"
done
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
