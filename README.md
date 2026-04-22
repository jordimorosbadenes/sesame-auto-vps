# sesame_auto – Fichaje automático para Sesame HR

Script Python que automatiza el fichaje en `panel.sesametime.com` usando **Playwright** (automatización de navegador headless).  
No necesita token de API ni permisos de administrador: usa tu email y contraseña normales.

Diseñado para correr en un VPS Linux (p.ej. Google Cloud free tier e2-micro).

## Qué hace

| Evento | Hora objetivo | Margen |
|--------|--------------|--------|
| Entrada | 08:00 | ±15 min |
| Salida comida | 13:00 | ±15 min |
| Vuelta comida | 14:00 | ±15 min |
| Salida | ~17:00 | ajustable |

La hora de salida se ajusta automáticamente cada día para que al final de la semana se acumulen ~40 horas.  
El estado se guarda en `/var/lib/sesame/state.json` para ser robusto ante reinicios del VPS.

---

## Requisitos previos

Solo necesitas tu **email y contraseña** de `panel.sesametime.com` — los mismos que usas para entrar a la web.

---

## Configuración

La estructura siempre es **multi-usuario**: cada usuario tiene su propia carpeta con su `.env` independiente.

```bash
mkdir -p users/tu_usuario
cp .env.example users/tu_usuario/.env
nano users/tu_usuario/.env   # rellena SESAME_EMAIL y SESAME_PASSWORD
```

Si tienes varios trabajadores, repite el proceso:

```bash
mkdir -p users/jordi
cp .env.example users/jordi/.env
nano users/jordi/.env

mkdir -p users/sofia
cp .env.example users/sofia/.env
nano users/sofia/.env
```

---

## Instalación en el VPS (Google Cloud / cualquier Debian·Ubuntu)

### Primera vez

```bash
# 1. Conectarse al VPS
ssh tu_usuario@ip_vps

# 2. Clonar el repositorio
git clone https://github.com/tu_repo/sesame.git
cd sesame

# 3. Crear las credenciales de cada usuario (solo esta vez, no están en git)
mkdir -p users/jordi
cp .env.example users/jordi/.env
nano users/jordi/.env   # rellena SESAME_EMAIL y SESAME_PASSWORD

mkdir -p users/sofia
cp .env.example users/sofia/.env
nano users/sofia/.env

# 4. Instalar
sudo bash install.sh
```

### Actualizaciones (código, vacaciones, añadir usuario)

```bash
ssh tu_usuario@ip_vps
cd sesame
git pull

# Si es un usuario nuevo, crear su .env antes de instalar:
# mkdir -p users/nuevo && cp .env.example users/nuevo/.env && nano users/nuevo/.env

sudo bash install.sh
```

Eso es todo. El install es **idempotente**: los `.env` existentes no se sobreescriben,
los `vacaciones.txt` sí (están en git y son la fuente autoritativa).

### Qué hace `install.sh`

- Instala dependencias de sistema y Chromium
- Copia scripts y `vacaciones.txt` a `/opt/sesame/`
- Crea venv Python con Playwright
- Reconfigura el cron (uno por usuario, L-V 07:00)
- Los `.env` los crea solo si no existen — nunca los sobreescribe

### Ver el log en tiempo real

```bash
tail -f /opt/sesame/users/jordi/sesame_auto.log
```

---

## Vacaciones

Edita `users/nombre/vacaciones.txt` localmente, haz push y vuelve a instalar:

```bash
# Localmente
nano users/jordi/vacaciones.txt
git add users/jordi/vacaciones.txt
git commit -m "Vacaciones Jordi agosto"
git push

# En el VPS
ssh tu_usuario@ip_vps
cd sesame && git pull && sudo bash install.sh
```

Formato del fichero:

```
# comentario (ignorado)
2026-04-23               # día suelto
2026-08-03..2026-08-21   # rango (extremos incluidos)
```

---

## Cómo funciona

```
07:00  Cron arranca el script
  │
  ├─ Carga estado semanal (horas acumuladas)
  ├─ Calcula horario de hoy con aleatoriedad ±15 min
  ├─ Ajusta hora de salida para cumplir ~40h/semana
  │
  ├─ ~08:00  Abre Chromium headless → login si caducó → pulsa botón "Fichar" (→ ENTRADA)
  ├─ ~13:00  Abre Chromium headless → pulsa botón "Fichar" (→ SALIDA COMIDA)
  ├─ ~14:00  Abre Chromium headless → pulsa botón "Fichar" (→ VUELTA COMIDA)
  └─ ~17:xx  Abre Chromium headless → pulsa botón "Fichar" (→ SALIDA)
       │
       └─ Guarda horas trabajadas → actualiza contador semanal
```

El script es **idempotente**: si el VPS se reinicia entre eventos, comprueba qué pasos ya se hicieron hoy y continúa desde donde quedó.

## Estructura de carpetas

```
sesame/                        ← repositorio git
  sesame_auto.py               ← script principal (igual para todos)
  test_fichar.py               ← script de test (igual para todos)
  install.sh                   ← instalador (ejecutar en el VPS)
  .env.example                 ← template para crear .env de usuario
  users/
    jordi/
      .env                     ← 🔒 gitignored, solo en el VPS
      vacaciones.txt           ← ✅ en git, se actualiza con git pull
      state.json               ← 🔒 gitignored, generado por el script
      browser_session.json     ← 🔒 gitignored
      sesame_auto.log          ← 🔒 gitignored
      screenshots/             ← 🔒 gitignored
    sofia/
      .env
      vacaciones.txt
      ...
```

**Regla simple:** lo que está en git se actualiza con `git pull` + `install.sh`.
Los `.env` son la única excepción: se crean una vez en el VPS y nunca se sobreescriben.

---

## Prueba en seco (sin fichar de verdad)

```bash
# En el VPS tras instalar:
SESAME_DRY_RUN=true /opt/sesame/venv/bin/python /opt/sesame/sesame_auto.py --env /opt/sesame/users/jordi/.env
```

Verás el horario calculado y los fichajes simulados, sin abrir el navegador.

## Verificar que funciona

Tras la primera ejecución real, comprueba los screenshots:
```bash
ls /opt/sesame/users/jordi/screenshots/
```

---

## Notas de seguridad

- El fichero `.env` tiene permisos `600` (solo legible por root/tu usuario)
- Nunca subas `.env` a git (está en `.gitignore`)
- Rota el token periódicamente desde el panel de Sesame

---

## Solución de problemas

### Login fallido
- Comprueba `SESAME_EMAIL` y `SESAME_PASSWORD` en el `.env`
- Mira el screenshot `login_failed_*.png` en `/var/lib/sesame/screenshots/`
- Verifica que puedes entrar manualmente en `https://panel.sesametime.com`

### "No se encontró el botón de fichaje"
Sesame puede haber cambiado el HTML. Mira el screenshot `no_button_*.png`.
Abre un issue con el screenshot para actualizar los selectores.

### El script no arranca por cron
```bash
grep sesame_auto /var/log/syslog   # ver errores de cron
crontab -l                          # verificar que el cron está instalado
```

### Ver el log en tiempo real
```bash
tail -f /var/log/sesame_auto.log
```
