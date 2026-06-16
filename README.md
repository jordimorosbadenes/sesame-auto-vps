# sesame_auto – Fichaje automático para Sesame HR

Script Python que automatiza el fichaje en `panel.sesametime.com` usando **HTTP requests** (sin navegador).  
No necesita token de API ni permisos de administrador: usa tu email y contraseña normales.

Diseñado para correr en un VPS Linux (p.ej. Google Cloud free tier e2-micro).

## Qué hace

| Evento | Hora objetivo | Margen |
|--------|--------------|--------|
| Entrada | 09:00 | ±15 min |
| Salida comida | 13:00 | ±15 min |
| Vuelta comida | 14:00 | ±15 min |
| Salida | ~18:00 | ajustable |

El horario se puede personalizar por usuario y por día de la semana (CSV en `.env`).  
La hora de salida se ajusta automáticamente cada día para que al final de la semana se acumulen ~40 horas.

---

## Requisitos previos

Solo necesitas tu **email y contraseña** de `panel.sesametime.com` — los mismos que usas para entrar a la web.

---

## Configuración

Cada usuario tiene su propia carpeta con su `.env` independiente.

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

### Personalizar horario por día (opcional)

Añade estas líneas al `.env` para cambiar el horario según el día de la semana.  
Formato CSV: 5 valores (lunes, martes, miércoles, jueves, viernes).

```env
SESAME_CHECK_IN_1_HOUR=9,9,10,9,9
SESAME_CHECK_OUT_1_HOUR=13,13,13,13,13
SESAME_CHECK_IN_2_HOUR=14,14,14,14,14
SESAME_CHECK_OUT_2_HOUR=18,18,19,18,18
```

Solo hace falta poner las líneas que se quieran cambiar.

---

## Instalación en el VPS (Debian/Ubuntu)

### Primera vez

```bash
# 1. Conectarse al VPS
ssh tu_usuario@ip_vps

# 2. Clonar el repositorio
git clone https://github.com/tu_repo/sesame.git
cd sesame

# 3. Crear las credenciales de cada usuario
mkdir -p users/jordi
cp .env.example users/jordi/.env
nano users/jordi/.env

mkdir -p users/sofia
cp .env.example users/sofia/.env
nano users/sofia/.env

# 4. Instalar
sudo bash install.sh
```

### Actualizaciones

```bash
ssh tu_usuario@ip_vps
cd sesame
git pull
sudo bash install.sh
```

### Qué hace `install.sh`

- Instala dependencias de sistema (Python3, pip, venv)
- Copia scripts a `/opt/sesame/`
- Crea venv Python con `requests` + `beautifulsoup4`
- Configura el cron (L-V 05:30, misma hora para todos los usuarios)
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

O descarga el iCal automáticamente desde Sesame:

```bash
python update_vacaciones.py --env users/jordi/.env
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
05:30  Cron arranca el script
  │
  ├─ Carga estado semanal (horas acumuladas)
  ├─ Calcula horario de hoy con aleatoriedad ±15 min
  ├─ Aplica sobrescrituras del .env (por día de la semana)
  ├─ Ajusta hora de salida para cumplir ~40h/semana
  │
  ├─ ~09:00  HTTP GET → login si caducó → pulsa botón "Fichar" (→ ENTRADA)
  ├─ ~13:00  HTTP GET → pulsa botón "Fichar" (→ SALIDA COMIDA)
  ├─ ~14:00  HTTP GET → pulsa botón "Fichar" (→ VUELTA COMIDA)
  └─ ~18:xx  HTTP GET → pulsa botón "Fichar" (→ SALIDA)
       │
       └─ Guarda horas trabajadas → actualiza contador semanal
```

Cada fichaje es una petición HTTP directa (~0.5s). No se abre ningún navegador.

El script es **idempotente**: si el VPS se reinicia entre eventos, comprueba qué pasos ya se hicieron hoy y continúa desde donde quedó.

## Estructura de carpetas

```
sesame/                        ← repositorio git
  sesame_auto.py               ← script principal (igual para todos)
  update_vacaciones.py         ← descarga iCal de vacaciones desde Sesame
  test_http.py                 ← script de test manual
  install.sh                   ← instalador (ejecutar en el VPS)
  .env.example                 ← template para crear .env de usuario
  users/
    jordi/
      .env                     ← gitignored, solo en el VPS
      vacaciones.txt           ← en git, se actualiza con git pull
      state.json               ← gitignored, generado por el script
      http_session.json        ← gitignored, cookies de sesión
      sesame_auto.log          ← gitignored
      debug/                   ← gitignored, HTMLs de error
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
SESAME_DRY_RUN=true /opt/sesame/venv/bin/python /opt/sesame/sesame_auto.py --env /opt/sesame/users/jordi/.env
```

Verás el horario calculado y los fichajes simulados, sin hacer nada en Sesame.

## Prueba HTTP manual

```bash
python test_http.py --env users/jordi/.env            # solo lectura
python test_http.py --env users/jordi/.env --check    # fichaje real
```

---

## Notas de seguridad

- El fichero `.env` tiene permisos `600` (solo legible por root/tu usuario)
- Nunca subas `.env` a git (está en `.gitignore`)
- Las cookies de sesión se guardan en `http_session.json` (gitignored)

---

## Solución de problemas

### Login fallido
- Comprueba `SESAME_EMAIL` y `SESAME_PASSWORD` en el `.env`
- Mira el HTML de debug en `users/jordi/debug/`
- Verifica que puedes entrar manualmente en `https://panel.sesametime.com`

### "No se encontró el botón de fichaje"
Sesame puede haber cambiado el HTML del botón `#check_button`.
Mira el HTML de debug y abre un issue para actualizar los selectores.

### El script no arranca por cron
```bash
grep sesame_auto /var/log/syslog   # ver errores de cron
crontab -l                          # verificar que el cron está instalado
```

### Ver el log en tiempo real
```bash
tail -f /opt/sesame/users/jordi/sesame_auto.log
```
