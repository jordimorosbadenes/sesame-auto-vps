#!/usr/bin/env python3
"""
update_vacaciones.py – Descarga el iCal de Sesame HR y actualiza vacaciones.txt
================================================================================
Uso:
  # Descarga automática via Playwright (recomendado):
  python update_vacaciones.py --env users/jordi/.env

  # Con fichero .ics ya descargado manualmente:
  python update_vacaciones.py --env users/jordi/.env --ical /ruta/Sesame-Calendar.ics

El fichero vacaciones.txt se regenera cada vez (sobrescribe el anterior).
Incluye:
  · Días "Calendario Valencia" que sean laborables (L-V) → festivos nacionales/autonómicos
  · Días "Vacaciones*"                                   → vacaciones propias

Los fines de semana (que también aparecen en el iCal como "Calendario Valencia")
se omiten del fichero porque sesame_auto.py ya los gestiona por separado.

Se puede ejecutar manualmente o vía cron mensual (ej: 1 de cada mes a las 05:00).
"""

import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# ── Carga del .env (igual que sesame_auto.py) ─────────────────────────────────
_env_arg = next(
    (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--env" and i + 1 < len(sys.argv)),
    None,
)
_env_path = Path(_env_arg).resolve() if _env_arg else Path(__file__).parent / ".env"
_user_dir = _env_path.parent
load_dotenv(_env_path)


def _resolve_path(env_var: str, default: str) -> Path:
    val = os.environ.get(env_var, default)
    p = Path(val)
    if not p.is_absolute():
        p = _user_dir / p
    return p.resolve()


SESAME_EMAIL    = os.environ.get("SESAME_EMAIL", "")
SESAME_PASSWORD = os.environ.get("SESAME_PASSWORD", "")
SESSION_FILE    = _resolve_path("SESAME_SESSION",    "browser_session.json")
VACACIONES_FILE = _resolve_path("SESAME_VACACIONES", "vacaciones.txt")
HEADLESS        = os.environ.get("SESAME_HEADLESS",  "true").lower() == "true"

LOGIN_URL     = "https://panel.sesametime.com"
VACATIONS_URL = "https://panel.sesametime.com/admin/users/vacations"
CHECKS_URL    = "https://panel.sesametime.com/admin/users/checks"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── iCal parser ───────────────────────────────────────────────────────────────

def parse_ical(text: str) -> list:
    """
    Lee un fichero iCal y devuelve lista ordenada de fechas (date) que deben
    tratarse como días no laborables:
      · SUMMARY empieza por "Calendario" → festivos (solo días L-V; los fines
        de semana en este grupo se omiten porque sesame_auto ya los salta).
      · SUMMARY empieza por "Vacaciones" → días de vacaciones (todos incluidos).
    """
    dates = []
    in_event = False
    summary = ""
    dtstart = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line == "BEGIN:VEVENT":
            in_event, summary, dtstart = True, "", None

        elif line == "END:VEVENT":
            if dtstart:
                s = summary.upper()
                if s.startswith("VACACIONES"):
                    dates.append(dtstart)
                elif s.startswith("CALENDARIO"):
                    # Solo incluir si es día laborable (lunes=0 … viernes=4)
                    if dtstart.weekday() < 5:
                        dates.append(dtstart)
            in_event = False

        elif in_event:
            if line.startswith("SUMMARY:"):
                summary = line[8:]
            elif line.startswith("DTSTART"):
                val = line.split(":", 1)[-1]
                try:
                    dtstart = datetime.strptime(val[:8], "%Y%m%d").date()
                except ValueError:
                    pass

    return sorted(set(dates))


def dates_to_vacaciones(dates: list) -> str:
    """
    Convierte lista de fechas ordenadas al formato de vacaciones.txt.
    Agrupa días consecutivos en rangos (2026-08-01..2026-08-14).
    """
    if not dates:
        return f"# Generado automáticamente – {date.today().isoformat()}\n# (sin días no laborables)\n"

    lines = [
        f"# Generado automáticamente desde Sesame iCal – {date.today().isoformat()}",
        "# Festivos laborables + vacaciones propias (fines de semana excluidos)",
        "",
    ]

    start = end = dates[0]
    for d in dates[1:]:
        from datetime import timedelta
        if (d - end).days == 1:
            end = d
        else:
            lines.append(str(start) if start == end else f"{start}..{end}")
            start = end = d
    lines.append(str(start) if start == end else f"{start}..{end}")

    return "\n".join(lines) + "\n"


# ── Playwright: login ─────────────────────────────────────────────────────────

def _needs_login(page: Page) -> bool:
    url = page.url.lower()
    if any(k in url for k in ("login", "signin", "sign-in", "auth")):
        return True
    for sel in ["#UserEmail", 'input[name="email"]', 'input[type="email"]']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def _do_login(page: Page) -> bool:
    log.info("  Sesión no activa, haciendo login…")
    for sel in ["#UserEmail", 'input[name="email"]', 'input[type="email"]']:
        try:
            el = page.wait_for_selector(sel, timeout=8000, state="visible")
            if el:
                el.fill(SESAME_EMAIL)
                break
        except PlaywrightTimeout:
            continue
    time.sleep(0.3)
    for sel in ["#UserPassword", 'input[name="password"]', 'input[type="password"]']:
        try:
            el = page.wait_for_selector(sel, timeout=5000, state="visible")
            if el:
                el.fill(SESAME_PASSWORD)
                break
        except PlaywrightTimeout:
            continue
    time.sleep(0.3)
    for sel in ['button[type="submit"]', 'input[type="submit"]',
                "button:has-text('Entrar')", "button:has-text('Iniciar sesión')",
                "button:has-text('Login')"]:
        try:
            el = page.wait_for_selector(sel, timeout=5000, state="visible")
            if el:
                el.click()
                break
        except PlaywrightTimeout:
            continue
    try:
        page.wait_for_url(lambda url: "login" not in url.lower(), timeout=15000)
        log.info("  Login completado.")
        return True
    except PlaywrightTimeout:
        return not _needs_login(page)


# ── Playwright: descarga del iCal ─────────────────────────────────────────────

# Selectores para el botón de exportar iCal (en orden de probabilidad)
ICAL_EXPORT_SELECTORS = [
    "a:has-text('Exportar iCal')",
    "button:has-text('Exportar iCal')",
    "a:has-text('Export iCal')",
    "button:has-text('iCal')",
    "a:has-text('iCal')",
    "a[href*='.ics']",
    "a[href*='ical']",
    "a[href*='calendar']",
    "[data-action*='ical']",
    "[title*='iCal']",
]

# Selectores para navegar a "Mis vacaciones" desde el menú
VACATIONS_NAV_SELECTORS = [
    "a:has-text('Mis vacaciones')",
    "a:has-text('Vacaciones')",
    "nav a:has-text('Vacaciones')",
    "[href*='vacation']",
    "[href*='vacacion']",
]


def _find_ical_button(page: Page):
    """Busca el botón de exportar iCal en la página actual. Devuelve el elemento o None."""
    for sel in ICAL_EXPORT_SELECTORS:
        try:
            el = page.wait_for_selector(sel, timeout=4000, state="visible")
            if el:
                log.info(f"  Botón iCal encontrado: {sel!r}")
                return el
        except PlaywrightTimeout:
            continue
    return None


def download_ical_playwright() -> str | None:
    """
    Abre Playwright, navega a la sección de vacaciones y descarga el iCal.
    Devuelve el contenido del fichero .ics como string, o None si falla.
    """
    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "es-ES",
            "accept_downloads": True,
        }
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context: BrowserContext = browser.new_context(**ctx_kwargs)
        page: Page = context.new_page()
        page.set_default_navigation_timeout(90_000)
        page.set_default_timeout(15_000)

        try:
            # ── Intento 1: ir directamente a la URL de vacaciones ─────────────
            log.info(f"  Navegando a {VACATIONS_URL} …")
            page.goto(VACATIONS_URL, wait_until="domcontentloaded", timeout=90_000)
            time.sleep(2)

            if _needs_login(page):
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(1)
                if not _do_login(page):
                    log.error("  Login fallido.")
                    return None
                # Guardar sesión
                SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(SESSION_FILE))
                page.goto(VACATIONS_URL, wait_until="domcontentloaded", timeout=90_000)
                time.sleep(2)

            ical_el = _find_ical_button(page)

            # ── Intento 2: navegar via menú lateral ───────────────────────────
            if not ical_el:
                log.info("  Botón no encontrado en URL directa. Intentando via menú…")
                for nav_sel in VACATIONS_NAV_SELECTORS:
                    try:
                        nav_el = page.wait_for_selector(nav_sel, timeout=3000, state="visible")
                        if nav_el:
                            nav_el.click()
                            time.sleep(2)
                            ical_el = _find_ical_button(page)
                            if ical_el:
                                break
                    except PlaywrightTimeout:
                        continue

            if not ical_el:
                log.error("  ✗ No se encontró el botón 'Exportar iCal'.")
                log.error("  → Usa --ical /ruta/Sesame-Calendar.ics para modo manual.")
                # Screenshot de diagnóstico
                try:
                    from pathlib import Path as _P
                    _sc = _user_dir / "screenshots" / "update_vacaciones_no_button.png"
                    _sc.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(_sc))
                    log.error(f"  Screenshot: {_sc}")
                except Exception:
                    pass
                return None

            # ── Descarga ──────────────────────────────────────────────────────
            log.info("  Iniciando descarga del iCal…")
            with page.expect_download(timeout=30_000) as dl_info:
                ical_el.click(no_wait_after=True)
            download = dl_info.value
            ical_path = Path(download.path())
            content = ical_path.read_text(encoding="utf-8")
            log.info(f"  ✓ iCal descargado ({len(content):,} bytes).")
            return content

        except Exception as e:
            log.error(f"  ✗ Error descargando iCal: {e}")
            return None
        finally:
            context.close()
            browser.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ical_arg = next(
        (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--ical" and i + 1 < len(sys.argv)),
        None,
    )

    if ical_arg:
        ical_path = Path(ical_arg)
        if not ical_path.exists():
            log.error(f"Fichero no encontrado: {ical_path}")
            sys.exit(1)
        log.info(f"Usando iCal local: {ical_path}")
        ical_text = ical_path.read_text(encoding="utf-8")
    else:
        log.info("Descargando iCal desde Sesame…")
        ical_text = download_ical_playwright()
        if not ical_text:
            log.error("No se pudo obtener el iCal.")
            sys.exit(1)

    dates = parse_ical(ical_text)
    log.info(f"Días no laborables encontrados: {len(dates)}")

    content = dates_to_vacaciones(dates)
    VACACIONES_FILE.parent.mkdir(parents=True, exist_ok=True)
    VACACIONES_FILE.write_text(content, encoding="utf-8")
    log.info(f"✓ {VACACIONES_FILE} actualizado.")

    # Mostrar primeras entradas para verificación
    preview = [l for l in content.splitlines() if l and not l.startswith("#")][:12]
    log.info("Primeras entradas:\n  " + "\n  ".join(preview))


if __name__ == "__main__":
    main()
