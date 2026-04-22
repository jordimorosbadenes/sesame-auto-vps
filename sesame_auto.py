#!/usr/bin/env python3
"""
sesame_auto.py – Fichaje automático para Sesame HR (vía navegador)
===================================================================
Automatiza el fichaje en panel.sesametime.com usando Playwright.
Inicia sesión con email/contraseña si la sesión ha caducado y pulsa
el botón de fichaje (toggle: primera vez entra, segunda sale, etc.).

Horario diario:
  ~08:00  Check in
  ~13:00  Salida comida
  ~14:00  Vuelta comida
  ~17:xx  Salida (ajustada para acumular ~40 h/semana)

Uso:
  python sesame_auto.py --env users/jordi/.env
  python sesame_auto.py --env users/sofia/.env

Cada usuario tiene su carpeta: users/nombre/.env
Copia .env.example a users/nombre/.env y rellena credenciales.
"""

import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# ── Carga del .env ────────────────────────────────────────────────────────────
# Soporte multi-usuario: python sesame_auto.py --env users/alice/.env
_env_arg = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--env" and i + 1 < len(sys.argv)), None)
_env_path = Path(_env_arg).resolve() if _env_arg else Path(__file__).parent / ".env"
_user_dir = _env_path.parent
load_dotenv(_env_path)

def _resolve_path(env_var: str, default: str) -> Path:
    """Resuelve rutas relativas respecto a _user_dir."""
    val = os.environ.get(env_var, default)
    p = Path(val)
    if not p.is_absolute():
        p = _user_dir / p
    return p.resolve()

# ── Configuración ─────────────────────────────────────────────────────────────
SESAME_EMAIL    = os.environ.get("SESAME_EMAIL", "")
SESAME_PASSWORD = os.environ.get("SESAME_PASSWORD", "")
TIMEZONE        = os.environ.get("TZ", "Europe/Madrid")
STATE_FILE       = _resolve_path("SESAME_STATE",       "state.json")
SESSION_FILE     = _resolve_path("SESAME_SESSION",     "browser_session.json")
SCREENSHOT_DIR   = _resolve_path("SESAME_SCREENSHOTS", "screenshots")
LOG_FILE         = str(_resolve_path("SESAME_LOG",     "sesame_auto.log"))
VACACIONES_FILE  = _resolve_path("SESAME_VACACIONES",  "vacaciones.txt")
DRY_RUN          = os.environ.get("SESAME_DRY_RUN", "false").lower() == "true"
TARGET_H         = float(os.environ.get("SESAME_TARGET_HOURS", "40.0"))
HEADLESS         = os.environ.get("SESAME_HEADLESS", "true").lower() == "true"

CHECKS_URL  = "https://panel.sesametime.com/admin/users/checks"
LOGIN_URL   = "https://panel.sesametime.com"

# Horario objetivo y margen en minutos
SCHEDULE = {
    "check_in":  dict(hour=8,  minute=0,  jitter=15),
    "lunch_out": dict(hour=13, minute=0,  jitter=15),
    "lunch_in":  dict(hour=14, minute=0,  jitter=15),
    "check_out": dict(hour=17, minute=0,  jitter=15),
}

# ── Logging ───────────────────────────────────────────────────────────────────
_log_path = Path(LOG_FILE)
_log_path.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Selectores del navegador (con fallbacks por si la web cambia) ─────────────

LOGIN_EMAIL_SELECTORS    = ["#UserEmail", 'input[name="email"]', 'input[type="email"]']
LOGIN_PASSWORD_SELECTORS = ["#UserPassword", 'input[name="password"]', 'input[type="password"]']
LOGIN_SUBMIT_SELECTORS   = [
    "#UserLoginForm .submit input",
    'button[type="submit"]',
    'input[type="submit"]',
    "button:has-text('Entrar')",
    "button:has-text('Iniciar sesión')",
    "button:has-text('Login')",
]

# Botón de fichar (toggle check in / check out)
CHECK_BUTTON_SELECTORS = [
    "#check_button",
    "[data-testid='check-button']",
    "[data-action*='check']",
    "button:has-text('Fichar')",
    "button:has-text('Check')",
    ".check-button",
    "button.btn-check",
    "[class*='checkin']",
    "[class*='check-in']",
]

# Confirmación tras pulsar el botón (alertas nativas o modales)
CONFIRM_SELECTORS = [
    "button:has-text('Confirmar')",
    "button:has-text('Aceptar')",
    "button:has-text('OK')",
    "button:has-text('Sí')",
    ".swal2-confirm",
    ".modal-footer button.btn-primary",
]


# ── Automatización del navegador ──────────────────────────────────────────────

def _take_screenshot(page: Page, label: str):
    """Guarda un screenshot para debug."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{ts}_{label}.png"
        page.screenshot(path=str(path))
        log.info(f"  📷 Screenshot: {path}")
    except Exception as e:
        log.debug(f"  Screenshot fallido: {e}")


def _first_visible(page: Page, selectors: list, timeout: int = 5000):
    """Devuelve el primer elemento visible que coincida con algún selector."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except PlaywrightTimeout:
            continue
    return None


def _needs_login(page: Page) -> bool:
    """True si la página actual es el login."""
    url = page.url.lower()
    if any(k in url for k in ("login", "signin", "sign-in", "auth")):
        return True
    for sel in LOGIN_EMAIL_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def _do_login(page: Page) -> bool:
    """Rellena el formulario de login y espera a que se complete."""
    log.info("  Rellenando formulario de login…")

    email_el = _first_visible(page, LOGIN_EMAIL_SELECTORS, timeout=8000)
    if not email_el:
        log.error("  No se encontró el campo de email.")
        _take_screenshot(page, "login_no_email")
        return False

    pass_el = _first_visible(page, LOGIN_PASSWORD_SELECTORS, timeout=5000)
    if not pass_el:
        log.error("  No se encontró el campo de contraseña.")
        _take_screenshot(page, "login_no_pass")
        return False

    email_el.fill(SESAME_EMAIL)
    time.sleep(0.3)
    pass_el.fill(SESAME_PASSWORD)
    time.sleep(0.3)

    submit_el = _first_visible(page, LOGIN_SUBMIT_SELECTORS, timeout=5000)
    if not submit_el:
        log.error("  No se encontró el botón de submit.")
        _take_screenshot(page, "login_no_submit")
        return False

    submit_el.click()

    try:
        page.wait_for_url(lambda url: "login" not in url.lower(), timeout=15000)
        log.info("  Login completado.")
        return True
    except PlaywrightTimeout:
        if not _needs_login(page):
            log.info("  Login completado (sin cambio de URL).")
            return True
        log.error("  Login fallido: sigue en la página de login.")
        _take_screenshot(page, "login_failed")
        return False


def _click_check_button(page: Page, action_label: str) -> bool:
    """Localiza y pulsa el botón de fichaje."""
    log.info(f"  Buscando botón de fichaje ({action_label})…")

    btn = _first_visible(page, CHECK_BUTTON_SELECTORS, timeout=10000)
    if not btn:
        log.error("  ✗ No se encontró el botón de fichaje.")
        _take_screenshot(page, f"no_button_{action_label}")
        return False

    btn_text = btn.inner_text().strip()
    log.info(f"  Botón encontrado: '{btn_text}'. Pulsando…")
    _take_screenshot(page, f"before_{action_label}")

    # Interceptar alert nativo antes del click
    page.once("dialog", lambda d: d.accept())
    btn.click()
    time.sleep(1.5)

    # Confirmar modal si aparece (ej. SweetAlert2)
    for sel in CONFIRM_SELECTORS:
        try:
            confirm_el = page.wait_for_selector(sel, timeout=2500, state="visible")
            if confirm_el:
                log.info("  Confirmando modal…")
                confirm_el.click()
                time.sleep(1.0)
                break
        except PlaywrightTimeout:
            continue

    _take_screenshot(page, f"after_{action_label}")
    log.info(f"  ✓ Fichaje '{action_label}' realizado.")
    return True


def do_check(action_label: str) -> bool:
    """
    Abre Playwright, inicia sesión si es necesario y pulsa el botón de fichaje.
    action_label es solo informativo (check_in / lunch_out / lunch_in / check_out).
    """
    if DRY_RUN:
        log.info(f"  [DRY RUN] Simular fichaje: {action_label}")
        return True

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )

        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "es-ES",
        }
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context: BrowserContext = browser.new_context(**ctx_kwargs)
        page: Page = context.new_page()

        try:
            log.info(f"  Navegando a {CHECKS_URL} …")
            page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            if _needs_login(page):
                log.info("  Sesión no activa, haciendo login…")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                time.sleep(1)
                if not _do_login(page):
                    return False
                page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)

            # Guardar cookies / sesión actualizada
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(SESSION_FILE))

            return _click_check_button(page, action_label)

        except Exception as e:
            log.error(f"  ✗ Error inesperado en do_check({action_label}): {e}")
            _take_screenshot(page, f"error_{action_label}")
            return False

        finally:
            context.close()
            browser.close()


# ── Estado persistente ────────────────────────────────────────────────────────

def _load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_week_hours(state: dict) -> float:
    """Devuelve las horas acumuladas esta semana (resetea al inicio de semana)."""
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    if state.get("week_start") != week_start:
        state["week_start"] = week_start
        state["week_hours"] = 0.0
        _save_state(state)
    return float(state.get("week_hours", 0.0))


def add_week_hours(state: dict, hours: float):
    state["week_hours"] = get_week_hours(state) + hours
    _save_state(state)


def get_today_schedule(state: dict) -> dict | None:
    sched = state.get("today_schedule", {})
    if sched.get("date") == date.today().isoformat():
        return sched
    return None


def set_today_schedule(state: dict, sched: dict):
    state["today_schedule"] = sched
    _save_state(state)


def is_done(state: dict, step: str) -> bool:
    sched = get_today_schedule(state)
    return bool(sched and step in sched.get("done", []))


def mark_done(state: dict, step: str, actual_time: str):
    sched = get_today_schedule(state) or {
        "date": date.today().isoformat(),
        "done": [],
        "actual_times": {},
    }
    if step not in sched.get("done", []):
        sched.setdefault("done", []).append(step)
    sched.setdefault("actual_times", {})[step] = actual_time
    state["today_schedule"] = sched
    _save_state(state)


# ── Cálculo del horario ───────────────────────────────────────────────────────

def _jittered(hour: int, minute: int, jitter: int, tz) -> datetime:
    """Datetime de hoy a la hora dada ± jitter minutos."""
    base = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_sec = random.randint(-jitter * 60, jitter * 60)
    return base + timedelta(seconds=delta_sec)


def build_schedule(tz, week_hours: float) -> dict:
    """
    Calcula los 4 horarios del día con aleatoriedad.
    La hora de salida se ajusta para alcanzar el objetivo semanal.
    """
    today = date.today()
    days_left = 5 - today.weekday()  # días laborables restantes incluyendo hoy

    # Horas objetivo para hoy
    target_today = (TARGET_H - week_hours) / max(days_left, 1)
    target_today = max(6.5, min(9.5, target_today))  # límite razonable

    t_in  = _jittered(**SCHEDULE["check_in"],  tz=tz)
    t_lo  = _jittered(**SCHEDULE["lunch_out"], tz=tz)
    t_li  = _jittered(**SCHEDULE["lunch_in"],  tz=tz)

    # Calcular salida para cumplir objetivo de horas
    morning_h   = (t_lo - t_in).total_seconds() / 3600
    afternoon_h = target_today - morning_h
    t_out       = t_li + timedelta(hours=afternoon_h)

    # Pequeño jitter extra en la salida (aspecto natural)
    t_out += timedelta(seconds=random.randint(-6 * 60, 6 * 60))

    # Sanity check: salida nunca antes de las 16:30 ni después de las 18:30
    lo_limit = t_li.replace(hour=16, minute=30, second=0)
    hi_limit = t_li.replace(hour=18, minute=30, second=0)
    if t_out < lo_limit:
        t_out = lo_limit + timedelta(seconds=random.randint(0, 5 * 60))
    if t_out > hi_limit:
        t_out = hi_limit - timedelta(seconds=random.randint(0, 5 * 60))

    return {
        "date":         today.isoformat(),
        "done":         [],
        "actual_times": {},
        "target_hours": round(target_today, 2),
        "check_in":     t_in.isoformat(),
        "lunch_out":    t_lo.isoformat(),
        "lunch_in":     t_li.isoformat(),
        "check_out":    t_out.isoformat(),
    }


def _sleep_until(target: datetime):
    wait = (target - datetime.now(target.tzinfo)).total_seconds()
    if wait <= 0:
        return
    log.info(f"  ⏳ Esperando {wait / 60:.1f} min hasta {target.strftime('%H:%M:%S')} …")
    time.sleep(wait)


# ── Calendario de vacaciones ──────────────────────────────────────────────────

def is_vacation_day(day: date) -> bool:
    """
    Lee VACACIONES_FILE y comprueba si `day` es día de vacaciones.

    Formato del fichero (líneas vacías y # comentarios se ignoran):
      2026-08-01             # día suelto
      2026-08-04..2026-08-14 # rango de fechas (ambos extremos incluidos)
    """
    if not VACACIONES_FILE.exists():
        return False

    for raw in VACACIONES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()   # quitar comentarios inline
        if not line:
            continue
        try:
            if ".." in line:
                start_s, end_s = line.split("..", 1)
                start = date.fromisoformat(start_s.strip())
                end   = date.fromisoformat(end_s.strip())
                if start <= day <= end:
                    return True
            else:
                if date.fromisoformat(line) == day:
                    return True
        except ValueError:
            log.warning(f"  vacaciones.txt: línea no reconocida → '{line}'")
    return False


# ── Validación de configuración ───────────────────────────────────────────────

def validate_config() -> bool:
    ok = True
    if not SESAME_EMAIL:
        log.error("SESAME_EMAIL no configurado.")
        ok = False
    if not SESAME_PASSWORD:
        log.error("SESAME_PASSWORD no configurado.")
        ok = False
    return ok


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not validate_config():
        log.error("Configuración incompleta. Revisa el fichero .env")
        sys.exit(1)

    tz = ZoneInfo(TIMEZONE)
    today = date.today()

    # No hacer nada en fin de semana
    if today.weekday() >= 5:
        log.info(f"Fin de semana ({today.strftime('%A')}), no hay nada que hacer.")
        return

    # No hacer nada en días de vacaciones
    if is_vacation_day(today):
        log.info(f"🏖️  Hoy ({today.isoformat()}) es día de vacaciones. No se ficha.")
        return

    log.info("=" * 55)
    log.info(f"  sesame_auto arrancando para {today.isoformat()}")
    log.info(f"  {'[DRY RUN] – no se harán fichajes reales' if DRY_RUN else 'MODO REAL'}")
    log.info("=" * 55)

    state = _load_state()
    week_h = get_week_hours(state)
    log.info(f"Horas esta semana: {week_h:.2f}h / {TARGET_H}h")

    # Cargar o generar el horario de hoy
    sched = get_today_schedule(state)
    if sched is None:
        sched = build_schedule(tz, week_h)
        set_today_schedule(state, sched)

    fmt = lambda s: datetime.fromisoformat(s).strftime("%H:%M")
    log.info(
        f"Horario hoy → entrada: {fmt(sched['check_in'])}"
        f"  |  salida comida: {fmt(sched['lunch_out'])}"
        f"  |  vuelta: {fmt(sched['lunch_in'])}"
        f"  |  salida: {fmt(sched['check_out'])}"
        f"  (objetivo {sched['target_hours']} h)"
    )

    steps = [
        ("check_in",  "check_in"),
        ("lunch_out", "lunch_out"),
        ("lunch_in",  "lunch_in"),
        ("check_out", "check_out"),
    ]

    check_in_time  = None
    check_out_time = None

    for step_name, action_label in steps:
        if is_done(state, step_name):
            log.info(f"  {step_name}: ya realizado hoy, omitiendo.")
            actual = sched.get("actual_times", {}).get(step_name)
            if step_name == "check_in" and actual:
                check_in_time = datetime.fromisoformat(actual).replace(tzinfo=tz)
            elif step_name == "check_out" and actual:
                check_out_time = datetime.fromisoformat(actual).replace(tzinfo=tz)
            continue

        target_dt = datetime.fromisoformat(sched[step_name]).replace(tzinfo=tz)
        now = datetime.now(tz)

        # Si el momento ya pasó hace >45 min, omitir
        if target_dt < now - timedelta(minutes=45):
            log.warning(
                f"  {step_name}: la hora programada ({target_dt.strftime('%H:%M')}) "
                "pasó hace >45 min — omitiendo."
            )
            continue

        _sleep_until(target_dt)

        success = do_check(action_label)
        actual_now = datetime.now(tz)

        if success:
            mark_done(state, step_name, actual_now.isoformat())
            if step_name == "check_in":
                check_in_time = actual_now
            elif step_name == "check_out":
                check_out_time = actual_now
        else:
            log.error(f"  ⚠️  {step_name} FALLÓ. Revisa screenshots en {SCREENSHOT_DIR}")

    # Actualizar contador semanal de horas
    if check_in_time and check_out_time:
        lunch_h = (
            datetime.fromisoformat(sched["lunch_in"]).replace(tzinfo=tz)
            - datetime.fromisoformat(sched["lunch_out"]).replace(tzinfo=tz)
        ).total_seconds() / 3600
        worked = (check_out_time - check_in_time).total_seconds() / 3600 - lunch_h
        add_week_hours(state, worked)
        log.info(
            f"Horas trabajadas hoy: {worked:.2f} h  "
            f"|  Total semana: {get_week_hours(state):.2f} h / {TARGET_H} h"
        )

    log.info("=" * 55)
    log.info("  sesame_auto finalizado")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
