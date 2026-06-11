#!/usr/bin/env python3
"""
sesame_auto.py – Fichaje automático para Sesame HR (vía navegador)
====================================================================
Automatiza el fichaje en panel.sesametime.com usando Playwright.
Inicia sesión con email/contraseña si la sesión ha caducado y pulsa
el botón de fichaje (toggle: primera vez entra, segunda sale, etc.).

Mejoras v2:
  - File lock: solo un browser Playwright a la vez (evita colisiones entre usuarios)
  - Verificación de estado: lee IN/OUT antes de clicar para evitar cascadas de errores
  - Recuperación de estado: si un paso falla, los siguientes se adaptan al estado real
  - Desfichaje garantizado: siempre se hace check_out al final del día
  - Limpieza de sesión: invalida cookies si un intento falla
  - Backoff exponencial en reintentos
  - Limpieza de procesos Chromium huérfanos

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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
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

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

# ── Carga del .env ────────────────────────────────────────────────────────────
_env_arg = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--env" and i + 1 < len(sys.argv)), None)
_env_path = Path(_env_arg).resolve() if _env_arg else Path(__file__).parent / ".env"
_user_dir = _env_path.parent
load_dotenv(_env_path)

def _resolve_path(env_var: str, default: str) -> Path:
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

# Robustez: timeout y reintentos por fichaje
MAX_CHECK_TIMEOUT_S = 15 * 60  # 15 min por intento (la navegación posterior al click puede ser lenta)
MAX_RETRIES         = 3
RETRY_BASE_DELAY_S  = 3 * 60
LOCK_TIMEOUT_S      = 15 * 60
HARD_DEADLINE_H     = 19

LOCK_FILE = Path(os.environ.get("SESAME_LOCK_FILE", "/tmp/sesame_playwright.lock"))

CHECKS_URL  = "https://panel.sesametime.com/admin/users/checks"
LOGIN_URL   = "https://panel.sesametime.com"

SCHEDULE = {
    "check_in":  dict(hour=8,  minute=0,  jitter=15),
    "lunch_out": dict(hour=13, minute=0,  jitter=15),
    "lunch_in":  dict(hour=14, minute=0,  jitter=15),
    "check_out": dict(hour=17, minute=0,  jitter=15),
}

STEP_TRANSITIONS = {
    "check_in":  {"before_state": "OUT", "after_state": "IN"},
    "lunch_out": {"before_state": "IN",  "after_state": "OUT"},
    "lunch_in":  {"before_state": "OUT", "after_state": "IN"},
    "check_out": {"before_state": "IN",  "after_state": "OUT"},
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

# Botón de fichaje: selector principal #check_button (basado en HTML real de Sesame)
CHECK_BUTTON_PRIMARY = "#check_button"
CHECK_BUTTON_FALLBACKS = [
    "[data-testid='check-button']",
    "[data-action*='check']",
    "a:has-text('Check IN')",
    "a:has-text('Check OUT')",
    "button:has-text('Fichar')",
    "button:has-text('Check')",
    ".check-button",
    "button.btn-check",
    "[class*='checkin']",
    "[class*='check-in']",
]

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

# Confirmación tras pulsar el botón (alertas nativas o modales)
CONFIRM_SELECTORS = [
    "button:has-text('Confirmar')",
    "button:has-text('Aceptar')",
    "button:has-text('OK')",
    "button:has-text('Sí')",
    ".swal2-confirm",
    ".modal-footer button.btn-primary",
]


# ── File Lock ──────────────────────────────────────────────────────────────────

_lock_fd = None

def _acquire_playwright_lock():
    """Adquiere un file lock para serializar el uso de Playwright entre usuarios."""
    global _lock_fd
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not HAS_FCNTL:
        log.info("  fcntl no disponible (Windows) — saltando lock.")
        return None

    lock_fd = open(LOCK_FILE, "w")
    deadline = time.time() + LOCK_TIMEOUT_S
    while time.time() < deadline:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_fd = lock_fd
            log.info(f"  🔒 Lock adquirido ({LOCK_FILE})")
            return lock_fd
        except (IOError, OSError):
            remaining = deadline - time.time()
            if remaining <= 0:
                lock_fd.close()
                raise TimeoutError(
                    f"No se pudo adquirir lock tras {LOCK_TIMEOUT_S // 60} min — "
                    f"otro usuario está usando el navegador?"
                )
            log.info(f"  ⏳ Esperando lock de Playwright… (quedan {remaining:.0f}s)")
            time.sleep(15)
    lock_fd.close()
    raise TimeoutError(f"No se pudo adquirir lock tras {LOCK_TIMEOUT_S // 60} min")


def _release_playwright_lock(lock_fd):
    """Libera el file lock."""
    global _lock_fd
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_fd.close()
    except Exception:
        pass
    _lock_fd = None
    log.info("  🔓 Lock liberado")


# ── Estado del fichaje ─────────────────────────────────────────────────────────

def _get_check_state(page: Page) -> str | None:
    """
    Lee el estado actual del usuario en Sesame.
    Devuelve 'IN' (fichado, puede salir) o 'OUT' (desfichado, puede entrar).

    Basado en el HTML real del botón #check_button:
      <a id="check_button" class="... ssm-btn-checkout ...">Check OUT</a>
        → usuario está IN, el botón permite salir
      <a id="check_button" class="... ssm-btn-checkin ...">Check IN</a>
        → usuario está OUT, el botón permite entrar
    """
    try:
        # Esperar a que la página esté estable antes de leer el estado
        _wait_page_stable(page, timeout_s=8)
        btn = page.query_selector(CHECK_BUTTON_PRIMARY)
        if not btn:
            for sel in CHECK_BUTTON_FALLBACKS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        break
                except Exception:
                    btn = None
                    continue
        if not btn:
            log.warning("  No se encontró el botón de fichaje para leer estado.")
            return None

        classes = btn.get_attribute("class") or ""
        text = btn.inner_text().strip().upper()

        if "ssm-btn-checkout" in classes:
            log.info(f"  Estado actual: IN (botón dice '{text}')")
            return "IN"
        if "ssm-btn-checkin" in classes:
            log.info(f"  Estado actual: OUT (botón dice '{text}')")
            return "OUT"

        if "OUT" in text:
            log.info(f"  Estado actual: IN (botón dice '{text}')")
            return "IN"
        if "IN" in text:
            log.info(f"  Estado actual: OUT (botón dice '{text}')")
            return "OUT"

        log.warning(f"  No se pudo determinar estado. Clases: '{classes}', texto: '{text}'")
        return None
    except Exception as e:
        log.warning(f"  Error leyendo estado: {e}")
        return None


# ── Limpieza ──────────────────────────────────────────────────────────────────

def _quick_check_current_state(tz) -> str | None:
    """
    Abre un navegador rápido, lee el estado actual en Sesame y cierra.
    Devuelve 'IN', 'OUT' o None si no se pudo determinar.
    """
    if DRY_RUN:
        return None
    lock_fd = None
    try:
        lock_fd = _acquire_playwright_lock()
    except TimeoutError:
        log.warning("  No se pudo adquirir lock para verificación de estado.")
        return None

    try:
        _kill_stale_chromium()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=HEADLESS,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
            )
            ctx_kwargs = {
                "viewport": {"width": 1280, "height": 720},
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "locale": "es-ES",
            }
            if SESSION_FILE.exists():
                ctx_kwargs["storage_state"] = str(SESSION_FILE)

            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.set_default_navigation_timeout(30_000)
            page.set_default_timeout(15_000)

            try:
                page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)

                if _needs_login(page):
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15_000)
                    time.sleep(1)
                    if not _do_login(page):
                        _invalidate_session()
                        return None
                    page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30_000)
                    time.sleep(2)

                return _get_check_state(page)
            except Exception:
                return None
            finally:
                context.close()
                browser.close()
    finally:
        if lock_fd:
            _release_playwright_lock(lock_fd)


def _kill_stale_chromium():
    """Mata procesos Chromium huérfanos (solo se ejecuta con el lock adquirido)."""
    try:
        os.system("pkill -f 'chrome-headless-shell.*--no-sandbox' 2>/dev/null || true")
        time.sleep(1)
    except Exception:
        pass


def _invalidate_session():
    """Borra browser_session.json para forzar login limpio en el próximo intento."""
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
            log.info("  🗑  Sesión invalidada (se forzará login limpio)")
    except Exception as e:
        log.debug(f"  No se pudo borrar sesión: {e}")


# ── Automatización del navegador ──────────────────────────────────────────────

def _wait_page_stable(page: Page, timeout_s: int = 10):
    """Espera a que la página esté estable (DOM cargado + network idle)."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
        page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
    except Exception:
        pass


def _take_screenshot(page: Page, label: str):
    """Guarda un screenshot para debug (con timeout para no colgarse si la página navega)."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{ts}_{label}.png"
        page.screenshot(path=str(path), timeout=5000)
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

    btn = None
    try:
        btn = page.wait_for_selector(CHECK_BUTTON_PRIMARY, timeout=10000, state="visible")
    except PlaywrightTimeout:
        pass

    if not btn:
        log.info("  Botón directo no encontrado. Probando dropdown 'Acciones'…")
        acciones_btn = _first_visible(page, [
            "button.dropdown-toggle.btn-settings",
            "button:has-text('Acciones')",
            ".btn-settings.dropdown-toggle",
        ], timeout=5000)
        if acciones_btn:
            acciones_btn.click()
            time.sleep(0.8)
            btn = _first_visible(page, [
                ".dropdown-menu a:has-text('Fichar')",
                ".dropdown-menu li:has-text('Fichar')",
                "a:has-text('Fichar')",
                "li:has-text('Fichar') a",
                "[class*='dropdown'] a:has-text('Fichar')",
            ], timeout=5000)

    if not btn:
        for sel in CHECK_BUTTON_FALLBACKS:
            try:
                btn = page.wait_for_selector(sel, timeout=3000, state="visible")
                if btn:
                    break
            except PlaywrightTimeout:
                continue

    if not btn:
        log.error("  ✗ No se encontró el botón de fichaje.")
        _take_screenshot(page, f"no_button_{action_label}")
        return False

    btn_text = btn.inner_text().strip()
    btn_class = (btn.get_attribute("class") or "").strip()
    log.info(f"  Botón encontrado: '{btn_text}'  [class: {btn_class}]")
    _take_screenshot(page, f"before_{action_label}")

    # Interceptar alert nativo antes del click
    page.once("dialog", lambda d: d.accept())
    try:
        # no_wait_after=True: el <a href> navega tras el confirm, NO esperamos a que termine
        btn.click(timeout=10_000, no_wait_after=True)
    except PlaywrightTimeout:
        log.warning("  ⚠ Timeout en click — el fichaje probablemente se completó igualmente.")
        _take_screenshot(page, f"click_timeout_{action_label}")
    # Esperar a que la página termine de navegar tras el click
    _wait_page_stable(page, timeout_s=15)
    time.sleep(1)

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
    log.info(f"  ✓ Fichaje '{action_label}' pulsado.")
    return True


def do_check(action_label: str, expected_before: str = None, expected_after: str = None) -> bool:
    """
    Abre Playwright, inicia sesión si es necesario y pulsa el botón de fichaje.
    action_label es solo informativo (check_in / lunch_out / lunch_in / check_out).

    Con verificación de estado:
      - Lee el estado IN/OUT antes de clicar
      - Si el estado ya es el esperado tras el fichaje, saltea
      - Si el estado es inesperado, intenta recuperación (doble click)
      - Verifica el estado después de clicar
    """
    if DRY_RUN:
        log.info(f"  [DRY RUN] Simular fichaje: {action_label}")
        return True

    lock_fd = None

    try:
        lock_fd = _acquire_playwright_lock()
    except TimeoutError as e:
        log.error(f"  ✗ {e}")
        return False

    try:
        _kill_stale_chromium()

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
            page.set_default_navigation_timeout(60_000)
            page.set_default_timeout(30_000)

            try:
                _do_check_start = time.time()
                log.info(f"  Navegando a {CHECKS_URL} …")
                page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(2)
                log.info(f"  URL actual: {page.url}")

                if _needs_login(page):
                    log.info("  Sesión no activa, haciendo login…")
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(1)
                    if not _do_login(page):
                        _invalidate_session()
                        return False
                    page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)

                # Guardar cookies / sesión actualizada
                SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(SESSION_FILE))

                # ── Verificación de estado ANTES de fichar ──────────────
                recovery_done = False
                current_state = _get_check_state(page)

                if current_state:
                    log.info(f"  URL: {page.url}")

                if current_state and expected_before and expected_after:

                    if current_state == expected_after:
                        log.info(f"  ✅ Ya en estado {expected_after} — no hace falta fichar.")
                        return True

                    if current_state != expected_before:
                        log.warning(
                            f"  ⚠ Estado inesperado para {action_label}: "
                            f"esperaba {expected_before}, encontré {current_state}. "
                            f"Objetivo: llegar a {expected_after}."
                        )

                        # Recuperación: necesitamos llegar a expected_after
                        if current_state == "OUT" and expected_after == "OUT":
                            # Estamos OUT y necesitamos terminar OUT (ej: check_out pero estamos en comida)
                            # Primero Check IN, luego Check OUT
                            log.warning("  🔧 Recuperación: haciendo Check IN primero (estamos en comida)…")
                            if not _click_check_button(page, f"recovery_in_{action_label}"):
                                _invalidate_session()
                                return False
                            _wait_page_stable(page, timeout_s=15)
                            new_state = _get_check_state(page)
                            if new_state != "IN":
                                log.error(f"  ❌ Recuperación falló: estado={new_state}, esperaba IN")
                                _invalidate_session()
                                return False
                            log.info("  ✅ Ahora estamos IN. Procediendo con el fichaje…")
                            recovery_done = True

                        elif current_state == "IN" and expected_after == "IN":
                            # Estamos IN y necesitamos terminar IN (ej: lunch_in pero nunca salimos)
                            # Primero Check OUT, luego Check IN
                            log.warning("  🔧 Recuperación: haciendo Check OUT primero (nunca salimos a comer)…")
                            if not _click_check_button(page, f"recovery_out_{action_label}"):
                                _invalidate_session()
                                return False
                            _wait_page_stable(page, timeout_s=15)
                            new_state = _get_check_state(page)
                            if new_state != "OUT":
                                log.error(f"  ❌ Recuperación falló: estado={new_state}, esperaba OUT")
                                _invalidate_session()
                                return False
                            log.info("  ✅ Ahora estamos OUT. Procediendo con el fichaje…")
                            recovery_done = True

                    elif current_state == "IN" and expected_after == "OUT":
                        log.info(f"  Estado correcto para fichar salida ({current_state} → {expected_after})")

                    elif current_state == "OUT" and expected_after == "IN":
                        log.info(f"  Estado correcto para fichar entrada ({current_state} → {expected_after})")

                # ── Clicar el botón ──────────────────────────────────────
                if not _click_check_button(page, action_label):
                    _invalidate_session()
                    return False

                time.sleep(2)

                # ── Verificación de estado DESPUÉS de fichar ─────────────
                # NO hacer page.reload() - la página ya navegó tras el click del <a href>
                # Solo esperar a que se estabilice y leer el estado actual
                if expected_after:
                    _wait_page_stable(page, timeout_s=15)
                    new_state = _get_check_state(page)
                    if new_state == expected_after:
                        log.info(f"  ✅ Estado verificado tras fichaje: {new_state} (correcto)")
                    elif new_state:
                        log.warning(
                            f"  ⚠ Estado tras fichaje: {new_state} "
                            f"(esperaba {expected_after})"
                        )
                        _wait_page_stable(page, timeout_s=8)
                        new_state2 = _get_check_state(page)
                        if new_state2 == expected_after:
                            log.info(f"  ✅ Estado verificado (segundo intento): {new_state2}")
                        else:
                            log.warning(f"  Estado final: {new_state2} (esperaba {expected_after})")

                # Guardar sesión actualizada
                context.storage_state(path=str(SESSION_FILE))
                elapsed = time.time() - _do_check_start
                log.info(f"  ⏱  Fichaje completado en {elapsed:.0f}s")
                return True

            except Exception as e:
                log.error(f"  ✗ Error inesperado en do_check({action_label}): {e}")
                _take_screenshot(page, f"error_{action_label}")
                _invalidate_session()
                return False

            finally:
                context.close()
                browser.close()

    finally:
        if lock_fd:
            _release_playwright_lock(lock_fd)


def do_check_with_timeout(action_label: str, expected_before: str = None, expected_after: str = None) -> bool:
    """
    Ejecuta do_check con un límite de MAX_CHECK_TIMEOUT_S segundos.
    Si Playwright se queda colgado (por ejemplo, página muy lenta),
    el futuro expira y se devuelve False para que el llamador pueda reintentar.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(do_check, action_label, expected_before, expected_after)
        try:
            return future.result(timeout=MAX_CHECK_TIMEOUT_S)
        except FuturesTimeout:
            log.error(
                f"  ✗ do_check({action_label}) superó el límite de "
                f"{MAX_CHECK_TIMEOUT_S // 60} min. Abortando intento."
            )
            global _lock_fd
            if _lock_fd:
                _release_playwright_lock(_lock_fd)
                _lock_fd = None
            _invalidate_session()
            _kill_stale_chromium()
            return False
        except Exception as e:
            log.error(f"  ✗ Excepción en do_check({action_label}): {e}")
            return False


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
    days_left = 5 - today.weekday()

    # Horas objetivo para hoy
    target_today = (TARGET_H - week_hours) / max(days_left, 1)
    target_today = max(6.5, min(9.5, target_today))

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
        line = raw.split("#")[0].strip()
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
        log.info(f"  Hoy ({today.isoformat()}) es día de vacaciones. No se ficha.")
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
    lunch_out_done = False
    lunch_in_done  = False
    # Tracking de pasos que realmente se ejecutaron (no solo marcados como done)
    last_step_success = True

    for step_name, action_label in steps:
        if is_done(state, step_name):
            log.info(f"  {step_name}: ya realizado hoy, omitiendo.")
            actual = sched.get("actual_times", {}).get(step_name)
            if step_name == "check_in" and actual:
                check_in_time = datetime.fromisoformat(actual).replace(tzinfo=tz)
            elif step_name == "check_out" and actual:
                check_out_time = datetime.fromisoformat(actual).replace(tzinfo=tz)
            elif step_name == "lunch_out":
                lunch_out_done = True
            elif step_name == "lunch_in":
                lunch_in_done = True
            continue

        target_dt = datetime.fromisoformat(sched[step_name]).replace(tzinfo=tz)
        now = datetime.now(tz)

        # Si el momento ya pasó hace >45 min, omitir (excepto check_out)
        if target_dt < now - timedelta(minutes=45) and step_name != "check_out":
            log.warning(
                f"  {step_name}: la hora programada ({target_dt.strftime('%H:%M')}) "
                "pasó hace >45 min — omitiendo."
            )
            continue

        # Si el paso anterior falló pero realmente funcionó en Sesame, recuperar
        if not last_step_success:
            log.info(f"  Verificando estado real tras paso anterior…")
            actual_state = _quick_check_current_state(tz)
            if actual_state:
                prev_step = steps[steps.index((step_name, action_label)) - 1] if steps.index((step_name, action_label)) > 0 else None
                if prev_step:
                    prev_name = prev_step[0]
                    prev_transition = STEP_TRANSITIONS.get(prev_name, {})
                    prev_expected_after = prev_transition.get("after_state")
                    if prev_expected_after and actual_state == prev_expected_after:
                        log.info(f"  ✅ Paso anterior '{prev_name}' realmente funcionó (estado={actual_state}). Recuperando.")
                        # Marcar como completado y actualizar variables
                        mark_done(state, prev_name, datetime.now(tz).isoformat())
                        if prev_name == "check_in":
                            check_in_time = datetime.now(tz)
                        elif prev_name == "lunch_out":
                            lunch_out_done = True
                        elif prev_name == "lunch_in":
                            lunch_in_done = True
                        last_step_success = True
                    # También verificar si ya estamos en el estado esperado para este paso
                    this_transition = STEP_TRANSITIONS.get(step_name, {})
                    if this_transition.get("after_state") == actual_state:
                        log.info(f"  ✅ Ya en estado {actual_state} para '{step_name}'. Saltando.")
                        mark_done(state, step_name, datetime.now(tz).isoformat())
                        if step_name == "check_in":
                            check_in_time = datetime.now(tz)
                        elif step_name == "check_out":
                            check_out_time = datetime.now(tz)
                        elif step_name == "lunch_out":
                            lunch_out_done = True
                        elif step_name == "lunch_in":
                            lunch_in_done = True
                        continue

        _sleep_until(target_dt)

        transition = STEP_TRANSITIONS.get(step_name, {})
        expected_before = transition.get("before_state")
        expected_after = transition.get("after_state")

        success = False
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                backoff = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 30 * 60)
                log.warning(
                    f"  🔄 Reintentando {step_name} en {backoff // 60} min "
                    f"(intento {attempt + 1}/{MAX_RETRIES + 1})…"
                )
                time.sleep(backoff)
            success = do_check_with_timeout(action_label, expected_before, expected_after)
            if success:
                break

        actual_now = datetime.now(tz)

        if success:
            mark_done(state, step_name, actual_now.isoformat())
            if step_name == "check_in":
                check_in_time = actual_now
            elif step_name == "check_out":
                check_out_time = actual_now
            elif step_name == "lunch_out":
                lunch_out_done = True
            elif step_name == "lunch_in":
                lunch_in_done = True
            last_step_success = True
        else:
            log.error(f"  ⚠️  {step_name} FALLÓ. Revisa screenshots en {SCREENSHOT_DIR}")
            last_step_success = False

    # ── GARANTÍA: El usuario DEBE estar desfichado (OUT) al final del día ────
    if not is_done(state, "check_out") and check_out_time is None:
        log.warning("  ⚠️  check_out no se completó. Intentando garantía de desfichaje…")

        hard_deadline = datetime.now(tz).replace(hour=HARD_DEADLINE_H, minute=0, second=0)
        attempt = 0
        while datetime.now(tz) < hard_deadline:
            attempt += 1
            log.info(f"  🔄 Intento de garantía check_out #{attempt}…")
            # Leer estado actual real: si ya está OUT, no hacer nada
            actual_state = _quick_check_current_state(tz)
            if actual_state == "OUT":
                log.info("  ✅ Ya está desfichado (OUT). Marcando como completado.")
                actual_now = datetime.now(tz)
                mark_done(state, "check_out", actual_now.isoformat())
                check_out_time = actual_now
                break
            elif actual_state == "IN":
                success = do_check_with_timeout("check_out_guarantee", "IN", "OUT")
                if success:
                    actual_now = datetime.now(tz)
                    mark_done(state, "check_out", actual_now.isoformat())
                    check_out_time = actual_now
                    log.info("  ✅ Garantía de desfichaje completada.")
                    break
            else:
                # No se pudo determinar estado - intentar fichaje a ciegas
                success = do_check_with_timeout("check_out_guarantee")
                if success:
                    actual_now = datetime.now(tz)
                    mark_done(state, "check_out", actual_now.isoformat())
                    check_out_time = actual_now
                    log.info("  ✅ Garantía de desfichaje completada.")
                    break
            remaining = hard_deadline - datetime.now(tz)
            if remaining.total_seconds() > 300:
                time.sleep(300)

        if check_out_time is None:
            log.error("  ❌ No se pudo garantizar el desfichaje. INTERVENCIÓN MANUAL NECESARIA.")

    # ── Actualizar contador semanal de horas ──────────────────────────────────
    if check_in_time and check_out_time:
        lunch_h = 0.0
        if lunch_out_done and lunch_in_done:
            lunch_h = (
                datetime.fromisoformat(sched["lunch_in"]).replace(tzinfo=tz)
                - datetime.fromisoformat(sched["lunch_out"]).replace(tzinfo=tz)
            ).total_seconds() / 3600
        elif lunch_out_done and not lunch_in_done:
            log.warning("  ⚠ lunch_in falló — no se resta tiempo de comida (estuvo OUT más tiempo del previsto).")
            lunch_h = 0.0
        else:
            if not lunch_out_done and not lunch_in_done:
                log.warning("  ⚠ Sin pausa de comida registrada — no se resta nada.")
            lunch_h = 0.0

        worked = (check_out_time - check_in_time).total_seconds() / 3600 - lunch_h
        add_week_hours(state, worked)
        log.info(
            f"Horas trabajadas hoy: {worked:.2f} h  "
            f"|  Total semana: {get_week_hours(state):.2f} h / {TARGET_H} h"
        )
    else:
        log.warning("  No se pudo calcular horas: falta check_in o check_out.")

    log.info("=" * 55)
    log.info("  sesame_auto finalizado")
    log.info("=" * 55)


if __name__ == "__main__":
    main()