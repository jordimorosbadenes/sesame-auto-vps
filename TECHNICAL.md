# sesame_auto – Documentación Técnica

## Arquitectura General

```
Cron (Linux)
    ↓ 05:30 lunes-viernes
sesame_auto.py --env users/nombre/.env
    ├─ Carga estado semanal (state.json)
    ├─ Calcula horario de hoy con jitter ±15 min
    ├─ Aplica sobrescrituras del .env (por día de la semana)
    ├─ Ajusta salida para ~40 h/semana
    ├─ Espera a cada hora y ejecuta do_check() via HTTP
    └─ Actualiza estado (horas, timestamps)
```

---

## 1. Cron Job: Arranque a las 05:30

### Configuración (VPS Linux)

El instalador (`install.sh`) configura automáticamente el cron. Todos los usuarios
comparten la misma hora (05:30) ya que no hay contención de recursos.

```
30 5 * * 1-5 /opt/sesame/venv/bin/python /opt/sesame/sesame_auto.py --env /opt/sesame/users/jordi/.env
```

- **30 5** → Minuto 30, hora 5 (05:30 CET)
- **\* \* 1-5** → Lunes-viernes
- El script **se ejecuta una sola vez al día**, a las 05:30

### Qué pasa en el arranque

1. `main()` se ejecuta a las 05:30
2. Lee `state.json` para saber:
   - Horas acumuladas esta semana
   - Si ya se han hecho fichajes hoy
3. Si es fin de semana, sale sin hacer nada
4. Si es vacaciones (según `vacaciones.txt`), sale sin hacer nada
5. Calcula el horario de hoy (con jitter y sobrescrituras del .env)
6. **Se queda corriendo** hasta que termine el día (~19:00)

---

## 2. SesameHTTP: Fichaje via HTTP

Ya no se usa navegador. Todo el fichaje se hace con peticiones HTTP directas
usando `requests` + `BeautifulSoup`.

### Login (CakePHP CSRF)

```
GET  https://panel.sesametime.com
  → Extrae campos ocultos del formulario #UserLoginForm:
     - _method
     - data[_Token][key]
     - data[_Token][fields]
     - data[_Token][unlocked]

POST https://panel.sesametime.com/users/login?redirect=...
  → Envía todos los campos CSRF + data[User][email] + data[User][password]
  → Si el login es correcto, redirige a /admin/users/checks
```

### Lectura de estado

```
GET https://panel.sesametime.com/admin/users/checks
  → Busca <a id="check_button"> en el HTML
  → Clase CSS:
     - "ssm-btn-checkout" → estado = IN (puede hacer check out)
     - "ssm-btn-checkin"  → estado = OUT (puede hacer check in)
```

### Toggle fichaje

```
GET https://panel.sesametime.com/admin/checks/check_panel/1
  → El servidor cambia el estado (toggle)
  → Redirige a /admin/users/checks/0
```

Cada operación tarda **0.5–2 segundos** (vs 5–15 minutos con Playwright).

---

## 3. Ciclo de vida del script: Estado persistente

### Fichero: `state.json`

```json
{
  "week_start": "2026-06-15",
  "week_hours": 39.5,
  "today_schedule": {
    "date": "2026-06-17",
    "done": ["check_in", "lunch_out"],
    "actual_times": {
      "check_in": "2026-06-17T09:04:30+02:00",
      "lunch_out": "2026-06-17T13:02:15+02:00"
    },
    "target_hours": 8.2,
    "check_in": "2026-06-17T09:04:30+02:00",
    "lunch_out": "2026-06-17T13:02:15+02:00",
    "lunch_in": "2026-06-17T14:01:45+02:00",
    "check_out": "2026-06-17T18:18:30+02:00"
  }
}
```

**Campos clave:**
- `week_start`: Lunes de la semana actual (ISO format)
- `week_hours`: Horas acumuladas desde el lunes
- `today_schedule`: Horario de hoy con tiempos reales cuando se ejecutan

**Reset automático:** Si la `week_start` ya pasó, se resetea toda la semana.

---

## 4. Sesión HTTP persistente

El script guarda las cookies de sesión en `http_session.json` después de cada
operación. Si se reinicia el script, carga las cookies guardadas para evitar
un nuevo login.

Si la sesión ha expirado (el servidor redirige a `/users/login`), el script
reloguea automáticamente antes de la siguiente operación.

---

## 5. Jitter: ±15 minutos aleatoria

### Función: `_jittered()`

```python
def _jittered(hour: int, minute: int, jitter: int, tz) -> datetime:
    """Datetime de hoy a la hora dada ± jitter minutos."""
    base = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_sec = random.randint(-jitter * 60, jitter * 60)
    return base + timedelta(seconds=delta_sec)
```

**Ejemplo:**
- Entrada programada: 09:00
- Jitter: ±15 min = ±900 segundos
- Resultado posible: 09:04:30 (4 min 30 seg después)
- Otro resultado: 08:53:12 (6 min 48 seg antes)

**Propósito:** Simular un comportamiento humano realista.

### Horarios del día con jitter:

```python
SCHEDULE = {
    "check_in":  dict(hour=9,  minute=0,  jitter=15),   # 08:45 ~ 09:15
    "lunch_out": dict(hour=13, minute=0,  jitter=15),   # 12:45 ~ 13:15
    "lunch_in":  dict(hour=14, minute=0,  jitter=15),   # 13:45 ~ 14:15
    "check_out": dict(hour=18, minute=0,  jitter=15),   # 17:45 ~ 18:15
}
```

### Sobrescritura por .env

El horario se puede personalizar por día de la semana via CSV en el `.env`:

```env
# CSV: lunes, martes, miercoles, jueves, viernes
SESAME_CHECK_IN_1_HOUR=9,9,10,9,9
SESAME_CHECK_OUT_2_HOUR=18,18,19,18,18
```

El orden de prioridad es:
1. `SESAME_{DAY}_{STEP}_{FIELD}` (por día, ej: `SESAME_TUESDAY_CHECK_IN_HOUR`)
2. `SESAME_{STEP}_{FIELD}` (global, ej: `SESAME_CHECK_IN_1_HOUR`)
3. Valores por defecto del código (9, 13, 14, 18)

---

## 6. Cálculo del horario diario: 40 horas semanales

### Objetivo: Acumular ~40 horas/semana con margen flexible

#### Paso 1: Calcular horas restantes

```python
def build_schedule(tz, week_hours: float, schedule: dict) -> dict:
    today = date.today()
    days_left = 5 - today.weekday()  # lunes=0, viernes=4
    
    # Si hoy es miércoles (2): days_left = 5 - 2 = 3 (miér, jue, vie)
    # Si hoy es viernes (4): days_left = 5 - 4 = 1 (viernes)
    
    target_today = (TARGET_H - week_hours) / max(days_left, 1)
    target_today = max(6.5, min(9.5, target_today))
```

**Ejemplo:**
- Lunes: semana_horas=0, dias_restantes=5 → objetivo hoy = 40/5 = 8h
- Martes: semana_horas=8.5, dias_restantes=4 → objetivo = (40-8.5)/4 = 7.875h
- Viernes: semana_horas=33, dias_restantes=1 → objetivo = (40-33)/1 = 7h

#### Paso 2: Calcular tiempo de mañana

```python
t_in  = _jittered(9,  0, 15, tz=tz)   # 09:00 ±15 min
t_lo  = _jittered(13, 0, 15, tz=tz)   # 13:00 ±15 min
t_li  = _jittered(14, 0, 15, tz=tz)   # 14:00 ±15 min

morning_h = (t_lo - t_in).total_seconds() / 3600
# Ejemplo: 13:02 - 09:04 = 3h 58 min = ~3.97 horas
```

#### Paso 3: Calcular hora de salida

```python
afternoon_h = target_today - morning_h
# Ejemplo: 8.0 - 3.97 = 4.03 horas

t_out = t_li + timedelta(hours=afternoon_h)
# Ejemplo: 14:01 + 4:01:48 = 18:02:48

# Jitter adicional en la salida (±6 min)
t_out += timedelta(seconds=random.randint(-6*60, 6*60))
```

#### Paso 4: Validación (sanity check)

```python
co_hour = schedule["check_out"]["hour"]
lo_limit = t_li.replace(hour=max(6, co_hour - 1), minute=0)  # ej: 17:00
hi_limit = t_li.replace(hour=min(23, co_hour + 1), minute=30)  # ej: 19:30

if t_out < lo_limit:
    t_out = lo_limit + timedelta(seconds=random.randint(0, 5*60))
if t_out > hi_limit:
    t_out = hi_limit - timedelta(seconds=random.randint(0, 5*60))
```

Los límites se adaptan automáticamente según la hora de salida configurada.

---

## 7. Loop principal

```python
steps = [
    ("check_in",  "check_in"),
    ("lunch_out", "lunch_out"),
    ("lunch_in",  "lunch_in"),
    ("check_out", "check_out"),
]

for step_name, action_label in steps:
    if is_done(state, step_name):
        continue  # ya hecho hoy

    target_dt = datetime.fromisoformat(sched[step_name]).replace(tzinfo=tz)
    _sleep_until(target_dt)

    success = False
    sesame = SesameHTTP()
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(backoff)
            sesame = SesameHTTP()
        success = sesame.do_check(action_label, expected_before, expected_after)
        if success:
            break

    if success:
        mark_done(state, step_name, actual_now.isoformat())
```

Cada paso:
1. Espera hasta la hora programada
2. Crea una sesión HTTP (con cookies guardadas)
3. Obtiene la página de checks, lee el estado actual
4. Si el estado ya es el esperado, omite
5. Hace GET al endpoint de toggle (`/admin/checks/check_panel/1`)
6. Verifica que el estado cambió
7. Guarda cookies actualizadas
8. Si falla, reintenta con backoff (10s, 20s, 40s)

---

## 8. Garantía de desfichaje

Si `check_out` falla durante el día, el script sigue reintentando hasta las
19:00, cada 5 minutos, usando HTTP (instantáneo, sin bloqueo).

```python
while datetime.now(tz) < hard_deadline:
    sesame = SesameHTTP()
    actual_state = sesame.get_check_state()
    if actual_state == "OUT":
        break  # ya desfichado
    elif actual_state == "IN":
        sesame.do_toggle()  # fuerza check out
    time.sleep(300)
```

---

## 9. Logs y debugging

### Fichero de log

```
/opt/sesame/users/jordi/sesame_auto.log
```

Ejemplo de ejecución:

```
2026-06-17 05:30:01 [INFO] =======================================================
2026-06-17 05:30:01 [INFO]   sesame_auto arrancando para 2026-06-17
2026-06-17 05:30:01 [INFO]   MODO REAL
2026-06-17 05:30:01 [INFO] =======================================================
2026-06-17 05:30:02 [INFO] Horas esta semana: 16.50h / 40.0h
2026-06-17 05:30:03 [INFO] Horario hoy -> entrada: 08:53 | salida comida: 13:10 | vuelta: 14:01 | salida: 18:05 (objetivo 7.83 h)
2026-06-17 05:30:03 [INFO] [WAIT] Esperando 203.0 min hasta 08:53:00 ...
2026-06-17 08:53:01 [INFO]   Iniciando sesion...
2026-06-17 08:53:02 [INFO]   Sesion ya activa.
2026-06-17 08:53:02 [INFO]   Estado correcto (OUT -> IN)
2026-06-17 08:53:02 [INFO]   Toggle: https://panel.sesametime.com/admin/checks/check_panel/1
2026-06-17 08:53:03 [INFO]   [OK] Estado verificado: IN
2026-06-17 08:53:03 [INFO]   check_in completado.
2026-06-17 08:53:04 [INFO] [WAIT] Esperando 251.9 min hasta 13:10:00 ...
```

### HTML de debug

Cuando ocurre un error, se guarda el HTML de la respuesta en `users/jordi/debug/`:

```bash
ls /opt/sesame/users/jordi/debug/
```

---

## 10. Comandos útiles

### Test en seco (no hace nada real)

```bash
SESAME_DRY_RUN=true python sesame_auto.py --env users/jordi/.env
```

### Test HTTP manual

```bash
python test_check_http.py --env users/jordi/.env           # solo lectura
python test_check_http.py --env users/jordi/.env --check    # fichaje real
```

### Resetear estado (borrar historial de la semana)

```bash
rm /opt/sesame/users/jordi/state.json
rm /opt/sesame/users/jordi/http_session.json  # forzar login nuevo
```

### Ver log en tiempo real

```bash
tail -f /opt/sesame/users/jordi/sesame_auto.log
```

### Cambiar zona horaria

En el `.env`:
```env
TZ=Europe/Madrid    # Por defecto
TZ=America/New_York # Para US
```

---

## 11. Comparativa: Playwright vs HTTP

| Aspecto | Playwright (v1/v2) | HTTP (v3) |
|---------|-------------------|-----------|
| Tiempo por fichaje | 5–15 min | 0.5–2 s |
| RAM por operación | ~500 MB (Chromium) | ~5 MB |
| Dependencias | playwright + Chromium (~400 MB) | requests + bs4 (~5 MB) |
| Lock entre usuarios | fcntl global | No necesario |
| Sesión | browser_session.json | http_session.json |
| Debug | screenshots PNG | HTML dumps |
| Robustez ante red lenta | Timeouts frecuentes | Timeouts de 30s |
| Líneas de código | ~1123 | ~637 |

## Sumario

- **Cron:** Una ejecución/día a las 05:30 (lunes-viernes)
- **Transporte:** HTTP requests (sin navegador)
- **Jitter:** ±15 min en cada hora para simular comportamiento humano
- **40 horas:** Se calcula dinámicamente cada día según horas restantes
- **Sobrescritura:** Horario personalizable por día via CSV en .env
- **Estado:** Persistente en `state.json`, permite recuperación ante fallos
- **Idempotencia:** Si algo falla, próxima ejecución continúa desde donde quedó
- **Logs:** Todo en `/opt/sesame/users/nombre/sesame_auto.log`
