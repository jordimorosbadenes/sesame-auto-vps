#!/usr/bin/env python3
"""
test_fichar.py – Prueba manual de fichaje
==========================================
Ejecuta UN solo fichaje (check in o check out, lo que toque según el estado
actual en la web) y muestra el resultado claramente.

Uso:
    python test_fichar.py --env users/jordi/.env
    python test_fichar.py --env users/sofia/.env

Opciones:
    --visible              Abre el navegador visible (por defecto)
    --headless             Modo headless (sin ventana)
    --env users/x/.env     Usa credenciales de ese usuario

Requiere:
    pip install playwright python-dotenv
    playwright install chromium

Cada usuario necesita una carpeta: users/nombre/.env
Copia .env.example a users/nombre/.env y rellena credenciales.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Comprobación de dependencias antes de importar ────────────────────────────
def _check_deps():
    missing = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        missing.append("playwright")
    try:
        import dotenv  # noqa: F401
    except ImportError:
        missing.append("python-dotenv")

    if missing:
        print("\n[ERROR] Faltan dependencias. Instálalas con:\n")
        print(f"  pip install {' '.join(missing)}")
        if "playwright" in missing:
            print("  playwright install chromium")
        print()
        sys.exit(1)

_check_deps()

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

# ── Colores para consola Windows (ANSI) ──────────────────────────────────────
os.system("")  # activa ANSI en Windows
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):  print(f"{GREEN}  ✔  {msg}{RESET}")
def err(msg): print(f"{RED}  ✘  {msg}{RESET}")
def info(msg):print(f"{CYAN}  →  {msg}{RESET}")
def warn(msg):print(f"{YELLOW}  ⚠  {msg}{RESET}")
def sep():    print(f"{BOLD}{'─'*55}{RESET}")

# ── Configuración ─────────────────────────────────────────────────────────────
# Soporte multi-usuario: python test_fichar.py --env users/alice/.env
_here = Path(__file__).parent
_env_arg = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--env" and i + 1 < len(sys.argv)), None)
_env_path = Path(_env_arg).resolve() if _env_arg else _here / ".env"
_user_dir = _env_path.parent
load_dotenv(_env_path)

def _resolve_path(env_var: str, default: str) -> Path:
    """Resuelve rutas relativas respecto a _user_dir."""
    val = os.environ.get(env_var, default)
    p = Path(val)
    if not p.is_absolute():
        p = _user_dir / p
    return p.resolve()

EMAIL    = os.environ.get("SESAME_EMAIL", "")
PASSWORD = os.environ.get("SESAME_PASSWORD", "")

# Los ficheros de sesión y screenshots para test van junto al .env de cada usuario
# (con sufijo _test para no mezclar con los de producción)
SCREENSHOT_DIR = _resolve_path("SESAME_SCREENSHOTS", "screenshots").parent / "screenshots_test"
SESSION_FILE   = _resolve_path("SESAME_SESSION", "browser_session.json").parent / "browser_session_test.json"

CHECKS_URL = "https://panel.sesametime.com/admin/users/checks"
LOGIN_URL  = "https://panel.sesametime.com"

HEADLESS = "--headless" in sys.argv  # por defecto muestra el navegador

# ── Selectores (mismos que sesame_auto.py) ────────────────────────────────────
LOGIN_EMAIL_SEL    = ["#UserEmail", 'input[name="email"]', 'input[type="email"]']
LOGIN_PASSWORD_SEL = ["#UserPassword", 'input[name="password"]', 'input[type="password"]']
LOGIN_SUBMIT_SEL   = [
    "#UserLoginForm .submit input",
    'button[type="submit"]',
    'input[type="submit"]',
    "button:has-text('Entrar')",
    "button:has-text('Iniciar sesión')",
    "button:has-text('Login')",
]
CHECK_BUTTON_SEL = [
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
CONFIRM_SEL = [
    "button:has-text('Confirmar')",
    "button:has-text('Aceptar')",
    "button:has-text('OK')",
    "button:has-text('Sí')",
    ".swal2-confirm",
    ".modal-footer button.btn-primary",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def screenshot(page, label) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%H%M%S")
    path = SCREENSHOT_DIR / f"{ts}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        info(f"Screenshot guardado: {path}")
    except Exception as e:
        warn(f"No se pudo guardar screenshot: {e}")
    return path


def open_file(path: Path):
    """Abre un fichero con el programa por defecto del sistema."""
    try:
        os.startfile(str(path))
    except Exception:
        pass


def first_visible(page, selectors, timeout=5000):
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except PlaywrightTimeout:
            continue
    return None


def needs_login(page) -> bool:
    url = page.url.lower()
    if any(k in url for k in ("login", "signin", "sign-in", "auth")):
        return True
    for sel in LOGIN_EMAIL_SEL:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


# ── Lógica principal ──────────────────────────────────────────────────────────

def run_test():
    sep()
    print(f"{BOLD}  sesame_auto – Test de fichaje manual{RESET}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sep()

    # Validar configuración
    if not EMAIL or not PASSWORD:
        err("SESAME_EMAIL o SESAME_PASSWORD no están configurados en el .env")
        print()
        print("  Crea el fichero .env a partir de .env.example:")
        print("    copy .env.example .env")
        print("    notepad .env")
        print()
        sys.exit(1)

    info(f"Usuario:   {EMAIL}")
    info(f"URL:       {CHECKS_URL}")
    info(f"Navegador: {'headless' if HEADLESS else 'visible (puedes verlo)'}")
    sep()

    final_screenshot = None
    result_ok = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=150 if not HEADLESS else 0,  # va más lento en modo visible para que se vea
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "es-ES",
        }
        if SESSION_FILE.exists():
            info("Cargando sesión guardada…")
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        try:
            # ── 1. Navegar a la página de fichajes ────────────────────────────
            info(f"Navegando a {CHECKS_URL} …")
            page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # ── 2. Login si hace falta ────────────────────────────────────────
            if needs_login(page):
                warn("Sesión no activa → haciendo login…")

                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                time.sleep(1)

                email_el = first_visible(page, LOGIN_EMAIL_SEL, timeout=8000)
                if not email_el:
                    err("No se encontró el campo de email en el formulario de login.")
                    final_screenshot = screenshot(page, "error_no_email_field")
                    open_file(final_screenshot)
                    return False

                pass_el = first_visible(page, LOGIN_PASSWORD_SEL, timeout=5000)
                if not pass_el:
                    err("No se encontró el campo de contraseña.")
                    final_screenshot = screenshot(page, "error_no_pass_field")
                    open_file(final_screenshot)
                    return False

                info("Rellenando credenciales…")
                email_el.fill(EMAIL)
                time.sleep(0.4)
                pass_el.fill(PASSWORD)
                time.sleep(0.4)

                submit_el = first_visible(page, LOGIN_SUBMIT_SEL, timeout=5000)
                if not submit_el:
                    err("No se encontró el botón de login.")
                    final_screenshot = screenshot(page, "error_no_submit")
                    open_file(final_screenshot)
                    return False

                submit_el.click()

                try:
                    page.wait_for_url(
                        lambda url: "login" not in url.lower(),
                        timeout=15000,
                    )
                    ok("Login completado.")
                except PlaywrightTimeout:
                    if not needs_login(page):
                        ok("Login completado.")
                    else:
                        err("Login fallido. Comprueba email/contraseña.")
                        final_screenshot = screenshot(page, "error_login_failed")
                        open_file(final_screenshot)
                        return False

                # Volver a la página de fichajes
                info("Navegando a la página de fichajes…")
                page.goto(CHECKS_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)

            else:
                ok("Sesión activa, no hace falta login.")

            # Guardar sesión para próximas veces
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(SESSION_FILE))

            # ── 3. Buscar y pulsar el botón de fichaje ────────────────────────
            info("Buscando el botón de fichaje…")
            btn = first_visible(page, CHECK_BUTTON_SEL, timeout=10000)

            if not btn:
                err("No se encontró el botón de fichaje en la página.")
                err("Puede que el diseño de la web haya cambiado.")
                final_screenshot = screenshot(page, "error_no_button")
                open_file(final_screenshot)

                # Imprimir los primeros botones visibles para diagnóstico
                buttons = page.query_selector_all("button")
                if buttons:
                    print()
                    warn("Botones visibles en la página:")
                    for b in buttons[:10]:
                        try:
                            if b.is_visible():
                                print(f"     [{b.get_attribute('id') or '?id'}] "
                                      f"clase='{b.get_attribute('class') or ''}' "
                                      f"texto='{b.inner_text().strip()[:40]}'")
                        except Exception:
                            pass
                return False

            btn_text  = btn.inner_text().strip()
            btn_id    = btn.get_attribute("id") or "?"
            btn_class = (btn.get_attribute("class") or "")[:50]
            info(f"Botón encontrado → id='{btn_id}'  texto='{btn_text}'  clase='{btn_class}'")

            screenshot(page, "1_antes_de_fichar")

            # Interceptar alert nativo
            page.once("dialog", lambda d: d.accept())

            info("Pulsando el botón…")
            btn.click()
            time.sleep(2)

            # Confirmar modal si aparece (SweetAlert2, Bootstrap modal…)
            for sel in CONFIRM_SEL:
                try:
                    confirm_el = page.wait_for_selector(sel, timeout=2500, state="visible")
                    if confirm_el:
                        info(f"Modal de confirmación detectado ({sel}), aceptando…")
                        confirm_el.click()
                        time.sleep(1.0)
                        break
                except PlaywrightTimeout:
                    continue

            time.sleep(1.5)
            final_screenshot = screenshot(page, "2_despues_de_fichar")
            result_ok = True

        except Exception as e:
            err(f"Error inesperado: {e}")
            try:
                final_screenshot = screenshot(page, "error_inesperado")
            except Exception:
                pass
            result_ok = False

        finally:
            context.close()
            browser.close()

    return result_ok


# ── Punto de entrada ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    success = run_test()
    sep()

    if success:
        print(f"\n{BOLD}{GREEN}  ✔  FICHAJE REALIZADO CORRECTAMENTE{RESET}\n")
        print(f"  Revisa los screenshots en: {SCREENSHOT_DIR}")
        print(f"  El screenshot 'despues_de_fichar' muestra el estado final.\n")
        # Abrir el screenshot final automáticamente
        after = sorted(SCREENSHOT_DIR.glob("*despues_de_fichar*"))
        if after:
            open_file(after[-1])
    else:
        print(f"\n{BOLD}{RED}  ✘  FICHAJE FALLIDO{RESET}\n")
        print(f"  Revisa los screenshots en: {SCREENSHOT_DIR}")
        print(f"  Se ha abierto el screenshot de error automáticamente.\n")
        sys.exit(1)
