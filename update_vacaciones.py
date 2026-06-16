#!/usr/bin/env python3
"""
update_vacaciones.py - Descarga el iCal de Sesame HR y actualiza vacaciones.txt
================================================================================
Uso:
  python update_vacaciones.py --env users/jordi/.env

  Con fichero .ics ya descargado manualmente:
  python update_vacaciones.py --env users/jordi/.env --ical /ruta/Sesame-Calendar.ics

El fichero vacaciones.txt se regenera cada vez (sobrescribe el anterior).
Incluye dias "Calendario Valencia" (festivos L-V) y dias "Vacaciones*".
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Faltan dependencias. Ejecuta: pip install requests beautifulsoup4 python-dotenv")
    sys.exit(1)

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


SESAME_EMAIL = os.environ.get("SESAME_EMAIL", "")
SESAME_PASSWORD = os.environ.get("SESAME_PASSWORD", "")
SESSION_FILE = _resolve_path("SESAME_HTTP_SESSION", "http_session.json")
VACACIONES_FILE = _resolve_path("SESAME_VACACIONES", "vacaciones.txt")

BASE_URL = "https://panel.sesametime.com"
VACATIONS_VIEW_URL = f"{BASE_URL}/admin/vacations/view"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Sesame HTTP session ─────────────────────────────────────────────────────


class SesameSession:
    """Mantiene sesion HTTP en Sesame HR."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        self._load_cookies()

    def _load_cookies(self):
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text())
                jar = requests.utils.cookiejar_from_dict(data.get("cookies", {}))
                self.session.cookies = jar
            except Exception:
                pass

    def _save_cookies(self):
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"cookies": requests.utils.dict_from_cookiejar(self.session.cookies)}
        SESSION_FILE.write_text(json.dumps(data, indent=2))

    def login(self) -> bool:
        """Login a Sesame HR. Devuelve True si ok."""
        log.info("  Iniciando sesion...")
        try:
            resp = self.session.get(BASE_URL, timeout=30)
            soup = BeautifulSoup(resp.text, "html.parser")
            form = soup.select_one("#UserLoginForm")
            if not form:
                log.info("  Sesion ya activa.")
                self._save_cookies()
                return True

            fields = {}
            for inp in form.select("input[type='hidden']"):
                name = inp.get("name")
                if name:
                    fields[name] = inp.get("value", "")

            action = form.get("action", "")
            action_url = urljoin(BASE_URL, action) if action else BASE_URL
            fields["data[User][email]"] = SESAME_EMAIL
            fields["data[User][password]"] = SESAME_PASSWORD

            resp = self.session.post(action_url, data=fields, timeout=30, allow_redirects=True)

            if "login" not in resp.url.lower():
                self._save_cookies()
                log.info("  Login completado.")
                return True

            log.error("  Login fallido.")
            return False
        except requests.RequestException as e:
            log.error(f"  Error de red: {e}")
            return False

    def get(self, url: str, **kwargs) -> requests.Response | None:
        """GET request, reloguea si redirige a login."""
        resp = self.session.get(url, timeout=30, **kwargs)
        if "login" in resp.url.lower():
            log.info("  Sesion expirada, relogueando...")
            if not self.login():
                return None
            resp = self.session.get(url, timeout=30, **kwargs)
        return resp


# ── iCal parser (sin cambios) ────────────────────────────────────────────────


def parse_ical(text: str) -> list:
    """Lee un fichero iCal y devuelve lista de fechas no laborables."""
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
    """Convierte fechas a formato vacaciones.txt con rangos."""
    regen_path = _env_path if _env_path else Path(__file__).parent / ".env"
    if not dates:
        return (
            f"# Generado automaticamente desde Sesame iCal - {date.today().isoformat()}\n"
            f"# Para regenerar ahora:\n"
            f"#   python {__file__} --env {regen_path}\n"
            f"# (sin dias no laborables)\n"
        )
    lines = [
        f"# Generado automaticamente desde Sesame iCal - {date.today().isoformat()}",
        "# Festivos laborables + vacaciones propias (fines de semana excluidos)",
        f"# Para regenerar ahora:",
        f"#   python {__file__} --env {regen_path}",
        "",
    ]
    start = end = dates[0]
    for d in dates[1:]:
        if (d - end).days == 1:
            end = d
        else:
            lines.append(str(start) if start == end else f"{start}..{end}")
            start = end = d
    lines.append(str(start) if start == end else f"{start}..{end}")
    return "\n".join(lines) + "\n"


# ── Descarga HTTP del iCal ──────────────────────────────────────────────────


def _find_ical_link(html: str, page_url: str) -> str | None:
    """Busca en el HTML el enlace al iCal. Devuelve URL absoluta o None."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if "export_ical" in href:
            return urljoin(page_url, href)
        if "ical" in text.lower() or "ical" in href.lower():
            return urljoin(page_url, href)
    return None


def download_ical_http() -> str | None:
    """Descarga el iCal de Sesame via HTTP. Devuelve contenido .ics o None."""
    ses = SesameSession()

    # Login
    if not ses.login():
        return None

    # Navegar a la pagina de vacaciones
    log.info(f"  Navegando a {VACATIONS_VIEW_URL} ...")
    resp = ses.get(VACATIONS_VIEW_URL)
    if resp is None:
        log.error("  No se pudo acceder a la pagina de vacaciones.")
        return None

    log.info(f"  URL: {resp.url} (status {resp.status_code})")
    ical_url = _find_ical_link(resp.text, resp.url)
    if ical_url:
        log.info(f"  URL iCal encontrada: {ical_url}")
    else:
        log.error("  No se encontro el enlace iCal.")
        return None

    # Descargar el iCal
    log.info(f"  Descargando iCal...")
    resp = ses.get(ical_url, allow_redirects=True)
    if resp is None:
        log.error("  No se pudo descargar el iCal.")
        return None

    log.info(f"  Descargado ({len(resp.text):,} bytes).")
    return resp.text


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
        log.info("Descargando iCal desde Sesame...")
        ical_text = download_ical_http()
        if not ical_text:
            log.error("No se pudo obtener el iCal.")
            sys.exit(1)

    dates = parse_ical(ical_text)
    log.info(f"Dias no laborables encontrados: {len(dates)}")

    content = dates_to_vacaciones(dates)
    VACACIONES_FILE.parent.mkdir(parents=True, exist_ok=True)
    VACACIONES_FILE.write_text(content, encoding="utf-8")
    log.info(f"[OK] {VACACIONES_FILE} actualizado.")

    preview = [l for l in content.splitlines() if l and not l.startswith("#")][:12]
    log.info("Primeras entradas:\n  " + "\n  ".join(preview))


if __name__ == "__main__":
    main()
