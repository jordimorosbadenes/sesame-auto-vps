# sesame_auto – Documentación Técnica

## Arquitectura General

```
Cron (Linux)
    ↓ 07:00 lunes-viernes
sesame_auto.py --env users/nombre/.env
    ├─ Carga estado semanal (state.json)
    ├─ Calcula horario de hoy con jitter ±15 min
    ├─ Ajusta salida para ~40 h/semana
    ├─ Espera a cada hora y ejecuta do_check()
    └─ Actualiza estado (horas, timestamps)
```

---

## 1. Cron Job: Arranque a las 07:00

### Configuración (VPS Linux)

```bash
crontab -e
```

```
0 7 * * 1-5 /opt/sesame/venv/bin/python /opt/sesame/sesame_auto.py --env /opt/sesame/users/jordi/.env
```

- **0 7** → Minuto 0, hora 7 (07:00:00 CET)
- **\* \* 1-5** → Todos los meses, todos los días, lunes-viernes (1=lun, 5=vie)
- El script **se ejecuta una sola vez al día**, a las 07:00

### Qué pasa en el arranque

1. `main()` se ejecuta a las 07:00
2. Lee `state.json` para saber:
   - Horas acumuladas esta semana
   - Si ya se han hecho fichajes hoy
3. Si es fin de semana, sale sin hacer nada
4. Calcula el horario de hoy
5. **Se queda corriendo** hasta que termine el día (aprox. 18:00)

---

## 2. Ciclo de vida del script: Estado persistente

### Fichero: `state.json`

```json
{
  "week_start": "2026-04-20",
  "week_hours": 39.5,
  "today_schedule": {
    "date": "2026-04-22",
    "done": ["check_in"],
    "actual_times": {
      "check_in": "2026-04-22T08:04:30+02:00"
    },
    "target_hours": 8.2,
    "check_in": "2026-04-22T08:04:30+02:00",
    "lunch_out": "2026-04-22T13:02:15+02:00",
    "lunch_in": "2026-04-22T14:01:45+02:00",
    "check_out": "2026-04-22T17:18:30+02:00"
  }
}
```

**Campos clave:**
- `week_start`: Lunes de la semana actual (ISO format)
- `week_hours`: Horas acumuladas desde el lunes
- `today_schedule`: Horario de hoy con tiempos reales cuando se ejecutan

**Reset automático:** Si la `week_start` ya pasó, se resetea toda la semana.

---

## 3. Jitter: ±15 minutos aleatoria

### Función: `_jittered()`

```python
def _jittered(hour: int, minute: int, jitter: int, tz) -> datetime:
    """Datetime de hoy a la hora dada ± jitter minutos."""
    base = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_sec = random.randint(-jitter * 60, jitter * 60)
    return base + timedelta(seconds=delta_sec)
```

**Ejemplo:**
- Entrada programada: 08:00
- Jitter: ±15 min = ±900 segundos
- Resultado posible: 08:04:30 (4 min 30 seg después)
- Otro resultado: 07:53:12 (6 min 48 seg antes)

**Propósito:** Simular un comportamiento humano realista (no siempre a la hora exacta).

### Horarios del día con jitter:

```python
SCHEDULE = {
    "check_in":  dict(hour=8,  minute=0,  jitter=15),   # 07:45 ~ 08:15
    "lunch_out": dict(hour=13, minute=0,  jitter=15),   # 12:45 ~ 13:15
    "lunch_in":  dict(hour=14, minute=0,  jitter=15),   # 13:45 ~ 14:15
    "check_out": dict(hour=17, minute=0,  jitter=15),   # 16:45 ~ 17:15
}
```

---

## 4. Cálculo del horario diario: 40 horas semanales

### Objetivo: Acumular ~40 horas/semana con margen flexible

#### Paso 1: Calcular horas restantes

```python
def build_schedule(tz, week_hours: float) -> dict:
    today = date.today()
    days_left = 5 - today.weekday()  # lunes=0, viernes=4
    
    # Si hoy es miércoles (2): days_left = 5 - 2 = 3 (miér, jue, vie)
    # Si hoy es viernes (4): days_left = 5 - 4 = 1 (viernes)
    
    target_today = (TARGET_H - week_hours) / max(days_left, 1)
    target_today = max(6.5, min(9.5, target_today))  # Límite razonable
```

**Ejemplo:**
- Lunes: semana_horas=0, dias_restantes=5 → objetivo hoy = 40/5 = 8h
- Martes: semana_horas=8.5, dias_restantes=4 → objetivo = (40-8.5)/4 = 7.875h
- Viernes: semana_horas=33, dias_restantes=1 → objetivo = (40-33)/1 = 7h

#### Paso 2: Calcular tiempo de mañana

```python
t_in  = _jittered(8,  0, 15, tz=tz)   # 08:00 ±15 min
t_lo  = _jittered(13, 0, 15, tz=tz)   # 13:00 ±15 min
t_li  = _jittered(14, 0, 15, tz=tz)   # 14:00 ±15 min

morning_h = (t_lo - t_in).total_seconds() / 3600
# Ejemplo: 13:02 - 08:04 = 4h 58 min = ~4.97 horas
```

#### Paso 3: Calcular hora de salida para cumplir objetivo

```python
afternoon_h = target_today - morning_h
# Ejemplo: 8.0 - 4.97 = 3.03 horas

t_out = t_li + timedelta(hours=afternoon_h)
# Ejemplo: 14:01 + 3:01:48 = 17:02:48

# Pequeño jitter adicional en la salida (±6 min)
t_out += timedelta(seconds=random.randint(-6*60, 6*60))
# Resultado posible: 17:05:20
```

#### Paso 4: Validación (sanity check)

```python
# La salida nunca debe ser antes de 16:30 ni después de 18:30
lo_limit = t_li.replace(hour=16, minute=30)  # 16:30
hi_limit = t_li.replace(hour=18, minute=30)  # 18:30

if t_out < lo_limit:
    t_out = lo_limit + timedelta(seconds=random.randint(0, 5*60))
if t_out > hi_limit:
    t_out = hi_limit - timedelta(seconds=random.randint(0, 5*60))
```

**Resultado final:**
```
check_in:  08:04:30
lunch_out: 13:02:15
lunch_in:  14:01:45
check_out: 17:05:20  ← ajustado para ~8 horas
```

---

## 5. Loop principal: Espera y ejecución de fichajes

### Código principal

```python
steps = [
    ("check_in",  "check_in"),
    ("lunch_out", "lunch_out"),
    ("lunch_in",  "lunch_in"),
    ("check_out", "check_out"),
]

for step_name, action_label in steps:
    # ¿Ya se hizo este paso hoy?
    if is_done(state, step_name):
        log.info(f"  {step_name}: ya realizado hoy, omitiendo.")
        continue

    # ¿Qué hora es ahora? ¿Hay que esperar?
    target_dt = datetime.fromisoformat(sched[step_name]).replace(tzinfo=tz)
    now = datetime.now(tz)

    # Si el momento ya pasó hace >45 min, omitir
    if now > target_dt and (now - target_dt).total_seconds() > 45*60:
        log.info(f"  {step_name}: momento ya pasó hace >45 min, omitiendo.")
        mark_done(state, step_name, actual_time="SKIPPED")
        continue

    # Si aún no es hora, esperar
    if now < target_dt:
        _sleep_until(target_dt)

    # YA ES HORA: Ejecutar fichaje
    success = do_check(action_label)
    if success:
        mark_done(state, step_name, actual_time=datetime.now(tz).isoformat())
        actual_hours = calculate_hours_worked(check_in_time, check_out_time)
        add_week_hours(state, actual_hours)
```

### Flujo paso a paso

**07:00** → Script arranca
- Carga estado (`state.json`)
- Hoy es 2026-04-22 (miércoles)
- Semana: lunes 2026-04-20
- Horas acumuladas: 16.5h

**Calcula horario:**
- `target_today = (40 - 16.5) / 3 = 7.83h`
- `check_in: 08:04:30`
- `lunch_out: 13:02:15`
- `lunch_in: 14:01:45`
- `check_out: 17:06:20` (ajustado para 7.83h)

**Guarda en `state.json`**

**Loop inicia:**
1. **08:04:30** → Espera hasta esa hora → Ejecuta `do_check("check_in")` → Abre navegador, hace login, pulsa botón → Guarda sesión, screenshot
2. **13:02:15** → Espera → Ejecuta `do_check("lunch_out")`
3. **14:01:45** → Espera → Ejecuta `do_check("lunch_in")`
4. **17:06:20** → Espera → Ejecuta `do_check("check_out")`

**Después de check_out:**
- Calcula horas reales: 17:06:20 - 08:04:30 = 8.95 horas (con pausa comida)
- Actualiza `state.json`: `week_hours: 16.5 + 7.83 = 24.33h`
- Script termina

**Próximo día (jueves):**
- Cron arranca a las 07:00 otra vez
- Lee `state.json`: semana_horas = 24.33h
- Calcula: `target_today = (40 - 24.33) / 2 = 7.835h`
- Repite el ciclo

---

## 6. Funciones clave

### `do_check(action_label: str) -> bool`

Ejecuta un fichaje completo:

```python
def do_check(action_label: str) -> bool:
    if DRY_RUN:
        log.info(f"  [DRY RUN] Simular fichaje: {action_label}")
        return True

    with sync_playwright() as pw:
        # 1. Lanza Chromium
        browser = pw.chromium.launch(headless=HEADLESS, ...)
        
        # 2. Crea contexto con cookies guardadas
        ctx_kwargs = {"storage_state": SESSION_FILE}
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        try:
            # 3. Navega a https://panel.sesametime.com/admin/users/checks
            page.goto(CHECKS_URL, timeout=30000)
            
            # 4. ¿Necesita login?
            if _needs_login(page):
                page.goto(LOGIN_URL)
                _do_login(page)  # Rellena email/password
                page.goto(CHECKS_URL)
            
            # 5. Guarda sesión (cookies) para próxima vez
            context.storage_state(path=SESSION_FILE)
            
            # 6. Pulsa el botón de fichaje
            return _click_check_button(page, action_label)

        finally:
            context.close()
            browser.close()
```

**Retorna:** `True` si fichaje OK, `False` si error

### `_sleep_until(target: datetime)`

Espera hasta la hora objetivo, mostrando progreso:

```python
def _sleep_until(target: datetime):
    wait = (target - datetime.now(target.tzinfo)).total_seconds()
    if wait <= 0:
        return
    log.info(f"  ⏳ Esperando {wait / 60:.1f} min hasta {target.strftime('%H:%M:%S')} …")
    time.sleep(wait)  # Bloquea hasta esa hora
```

**Ejemplo:**
```
⏳ Esperando 234.5 min hasta 08:04:30 …
[espera 234 minutos y medio]
Botón pulsado correctamente
```

### `is_done(state, step) -> bool` y `mark_done()`

Registro persistente de pasos completados:

```python
def is_done(state: dict, step: str) -> bool:
    sched = get_today_schedule(state)
    return bool(sched and step in sched.get("done", []))

def mark_done(state: dict, step: str, actual_time: str):
    sched = get_today_schedule(state) or {
        "date": date.today().isoformat(),
        "done": [],
        "actual_times": {},
    }
    sched.setdefault("done", []).append(step)
    sched.setdefault("actual_times", {})[step] = actual_time
    state["today_schedule"] = sched
    _save_state(state)
```

---

## 7. Robustez: Idempotencia y recuperación

### Caso 1: Script se reinicia a las 14:30

```
07:00  Script arranca (cron)
...
10:00  Error en VPS, reinicio
10:05  Cron intenta ejecutar, pero en crontab aparece como "ya ejecutado"
       → No pasa nada (cron no re-ejecuta el mismo job del mismo día)
```

**NOTA:** El cron no re-ejecuta si el script sigue corriendo. Si se reinicia el sistema:

```
07:00  Cron arranca script
13:00  VPS se reinicia (apagón)
       
13:05  VPS se recupera, cron NO ejecuta (ya fue hoy)
13:30  Usuario ejecuta manualmente:
       python sesame_auto.py --env users/jordi/.env
       
       Script lee state.json:
       - check_in: ✓ done
       - lunch_out: ✓ done
       - lunch_in: ⚠ NO HECHO (hora = 14:01, ahora = 13:30)
       → Continúa normalmente desde aquí
```

### Caso 2: Script se queda colgado

```
Script arrancó a 07:00 pero se queda esperando entrada (network issue)
Usuario ejecuta en otro terminal:
    python test_fichar.py --env users/jordi/.env
    → Abre sesión separada (browser_session_test.json)
    → Prueba manual sin interferir con el principal
```

---

## 8. Logs y debugging

### Fichero de log

```
/opt/sesame/users/jordi/sesame_auto.log
```

Ejemplo de ejecución exitosa:

```
2026-04-22 07:00:01 [INFO] ═══════════════════════════════════════════════════════
2026-04-22 07:00:01 [INFO]   sesame_auto arrancando para 2026-04-22
2026-04-22 07:00:01 [INFO]   MODO REAL
2026-04-22 07:00:01 [INFO] ═══════════════════════════════════════════════════════
2026-04-22 07:00:02 [INFO] Horas esta semana: 16.50h / 40.0h
2026-04-22 07:00:03 [INFO] Horario hoy → entrada: 08:04 | salida comida: 13:02 | vuelta: 14:01 | salida: 17:06 (objetivo 7.83 h)
2026-04-22 07:00:03 [INFO] ⏳ Esperando 64.5 min hasta 08:04:30 …
2026-04-22 08:04:31 [INFO]   Navegando a https://panel.sesametime.com/admin/users/checks …
2026-04-22 08:05:12 [INFO]   Sesión activa, omitiendo login.
2026-04-22 08:05:13 [INFO]   Buscando botón de fichaje (check_in)…
2026-04-22 08:05:15 [INFO]   Botón encontrado: 'Fichar'. Pulsando…
2026-04-22 08:05:16 [INFO]   ✓ Fichaje 'check_in' realizado.
2026-04-22 08:05:17 [INFO]   📷 Screenshot: /opt/sesame/users/jordi/screenshots/20260422_080516_after_check_in.png
2026-04-22 08:05:18 [INFO] ⏳ Esperando 294.5 min hasta 13:02:15 …
2026-04-22 13:02:16 [INFO]   ✓ Fichaje 'lunch_out' realizado.
2026-04-22 14:01:45 [INFO]   ✓ Fichaje 'lunch_in' realizado.
2026-04-22 17:06:21 [INFO]   ✓ Fichaje 'check_out' realizado.
2026-04-22 17:06:22 [INFO] ✓ Horas trabajadas: 8.95h (acumulado: 25.45h)
```

### Ver log en tiempo real

```bash
tail -f /opt/sesame/users/jordi/sesame_auto.log
```

---

## 9. Ejemplo completo: Una semana

| Día | Estado | Horas objetivo | Entrada | Comida | Vuelta | Salida | Real | Acumulado |
|-----|--------|----------------|---------|--------|--------|--------|------|-----------|
| Lun | Lunes 0h | 8.0h | 08:05 | 13:02 | 14:01 | 17:06 | 8.95h | 8.95h |
| Mar | +8.95h | 7.76h | 08:03 | 13:01 | 14:00 | 17:02 | 8.92h | 17.87h |
| Mié | +17.87h | 7.38h | 08:07 | 13:04 | 14:03 | 17:05 | 8.90h | 26.77h |
| Jue | +26.77h | 6.61h | 08:02 | 13:00 | 14:02 | 16:39 | 7.95h | 34.72h |
| Vie | +34.72h | 5.28h | 08:04 | 13:03 | 14:01 | 16:01 | 6.89h | 41.61h |

**Resultado:** 41.61h (1.61h extra, distribuidoras en la semana).

---

## 10. Comandos útiles

### Test en seco (no hace nada real)

```bash
SESAME_DRY_RUN=true python sesame_auto.py --env users/jordi/.env
```

Output:
```
[DRY RUN] Simular fichaje: check_in
[DRY RUN] Simular fichaje: lunch_out
[DRY RUN] Simular fichaje: lunch_in
[DRY RUN] Simular fichaje: check_out
```

### Test manual (abre navegador)

```bash
python test_fichar.py --env users/jordi/.env
```

Hace UN solo fichaje visible.

### Ver screenshots después de ejecutar

```bash
ls -la /opt/sesame/users/jordi/screenshots/
# Abre con imagen viewer si está en máquina local:
eog /opt/sesame/users/jordi/screenshots/*_check_in.png
```

### Resetear estado (borrar historial de la semana)

```bash
rm /opt/sesame/users/jordi/state.json
# Próxima ejecución empezará con 0 horas
```

### Cambiar zona horaria

En el `.env`:
```env
TZ=Europe/Madrid    # Por defecto
TZ=America/New_York # Para US
```

Soporta cualquier timezone de `zoneinfo` (IANA).

---

## 11. Diagrama de flujo

```
Cron 07:00
    ↓
main()
    ├─ ¿Fin de semana? → SALIR
    ├─ load_state() → state.json
    ├─ get_week_hours() → 16.5h
    ├─ build_schedule(16.5h) → horarios + jitter
    │  ├─ check_in:  08:04:30
    │  ├─ lunch_out: 13:02:15
    │  ├─ lunch_in:  14:01:45
    │  └─ check_out: 17:06:20 ← CALCULADO para 7.83h
    ├─ save state
    │
    └─ LOOP: para cada paso
        ├─ ¿Ya hecho hoy? → skip
        ├─ ¿Pasó hace >45min? → skip
        ├─ sleep_until(target_time)
        ├─ do_check()
        │  ├─ Launch Chromium
        │  ├─ Load cookies
        │  ├─ Navigate a checks URL
        │  ├─ Login si needed
        │  ├─ Click button
        │  └─ Save screenshot + cookies
        ├─ mark_done()
        └─ update week_hours

Script finaliza ~18:00
    ↓
Próximo día: Cron ejecuta de nuevo a 07:00
```

---

## Sumario

- **Cron:** Una ejecución/día a las 07:00 (lunes-viernes)
- **Jitter:** ±15 min en cada hora para simular comportamiento humano
- **40 horas:** Se calcula dinámicamente cada día según horas restantes
- **Estado:** Persistente en `state.json`, permite recuperación ante fallos
- **Idempotencia:** Si algo falla, próxima ejecución continúa desde donde quedó
- **Logs:** Todo en `/opt/sesame/users/nombre/sesame_auto.log`
