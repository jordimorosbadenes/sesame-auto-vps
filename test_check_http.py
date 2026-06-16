#!/usr/bin/env python3
"""
test_check_http.py – Verifica el fichaje HTTP en Sesame HR.
======================================================================
Usa requests + BeautifulSoup (sin navegador).

Si esto funciona, podemos reescribir sesame_auto.py sin navegador.

Uso:
    python test_http.py --env users/jordi/.env            # solo lectura
    python test_http.py --env users/jordi/.env --check    # fichaje real (toggle)
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlencode, quote

from dotenv import load_dotenv

_env_arg = next(
    (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--env" and i + 1 < len(sys.argv)),
    None,
)
_env_path = Path(_env_arg).resolve() if _env_arg else Path(__file__).parent / ".env"
_user_dir = _env_path.parent
load_dotenv(_env_path)

DO_CHECK = "--check" in sys.argv

SESAME_EMAIL = os.environ.get("SESAME_EMAIL", "")
SESAME_PASSWORD = os.environ.get("SESAME_PASSWORD", "")

BASE_URL = "https://panel.sesametime.com"

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Faltan dependencias. Instala: pip install requests beautifulsoup4")
    sys.exit(1)


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def ok(msg): print(f"  {GREEN}✔ {msg}{RESET}")
def err(msg): print(f"  {RED}✘ {msg}{RESET}")
def info(msg): print(f"  {CYAN}→ {msg}{RESET}")
def warn(msg): print(f"  {YELLOW}⚠ {msg}{RESET}")
def sep(): print(f"{BOLD}{'─' * 55}{RESET}")


def find_check_button(soup):
    btn = soup.select_one("#check_button")
    if btn:
        return btn
    for sel in [
        "[data-testid='check-button']",
        "a.ssm-btn-checkin",
        "a.ssm-btn-checkout",
        "a.check-button",
        "button.btn-check",
    ]:
        btn = soup.select_one(sel)
        if btn:
            return btn
    return None


def extract_state_from_button(btn) -> str | None:
    classes = btn.get("class", [])
    text = btn.get_text(strip=True).upper()
    if "ssm-btn-checkout" in classes:
        return "IN"
    if "ssm-btn-checkin" in classes:
        return "OUT"
    if "OUT" in text:
        return "IN"
    if "IN" in text:
        return "OUT"
    return None


def save_debug_html(content: str, name: str):
    path = _user_dir / f"debug_{name}.html"
    path.write_text(content, encoding="utf-8")
    info(f"HTML guardado: {path}")


def save_debug_headers(resp, name: str):
    d = {
        "status": resp.status_code,
        "url": resp.url,
        "headers": dict(resp.headers),
        "cookies": dict(resp.cookies),
    }
    (_user_dir / f"debug_{name}.json").write_text(json.dumps(d, indent=2))


def extract_form_fields(soup):
    """
    Extrae todos los campos ocultos de #UserLoginForm (o del primer <form>).
    Devuelve un dict con todos los name/value, incluyendo los CSRF de CakePHP.
    """
    fields = {}
    form = soup.select_one("#UserLoginForm") or soup.select_one("form")
    if not form:
        return fields, None

    action = form.get("action", "")
    action_url = urljoin(BASE_URL, action) if action else BASE_URL

    for inp in form.select("input[type='hidden'], input[type='submit'], input:not([type])"):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            fields[name] = value

    return fields, action_url


def main():
    if not SESAME_EMAIL or not SESAME_PASSWORD:
        err("SESAME_EMAIL y SESAME_PASSWORD deben estar en .env")
        sys.exit(1)

    sep()
    print(f"{BOLD}  test_http – Sesame HR via HTTP{RESET}")
    print(f"  Usuari: {SESAME_EMAIL}")
    print(f"  Check:  {'SÍ (se hará fichaje real)' if DO_CHECK else 'NO (solo lectura)'}")
    sep()

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    })

    # ── 1. GET login page ────────────────────────────────────────────
    print()
    info("Obteniendo página de login…")
    t0 = time.time()
    resp = session.get(BASE_URL, timeout=30)
    t1 = time.time()
    ok(f"Status {resp.status_code} en {t1-t0:.1f}s | URL: {resp.url}")

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── 2. POST login with ALL form fields (keeps CakePHP CSRF intact) ──
    print()
    info("Extrayendo campos del formulario de login…")
    form_fields, login_action = extract_form_fields(soup)
    info(f"Campos ocultos encontrados: {list(form_fields.keys())}")
    info(f"Action URL: {login_action}")

    if not form_fields.get("data[_Token][key]"):
        save_debug_html(resp.text, "login_page")
        err("No se encontró data[_Token][key] — ¿la página contiene el formulario de login?")
        return False

    # Añadir email y password
    form_fields["data[User][email]"] = SESAME_EMAIL
    form_fields["data[User][password]"] = SESAME_PASSWORD

    print()
    info("Haciendo login con todos los campos CSRF…")
    t0 = time.time()
    resp = session.post(login_action, data=form_fields, timeout=30, allow_redirects=True)
    t1 = time.time()
    ok(f"Status {resp.status_code} en {t1-t0:.1f}s | URL final: {resp.url}")

    # Verificar si el login fue exitoso
    if "login" in resp.url.lower() and not resp.url.lower().endswith("checks"):
        soup_check = BeautifulSoup(resp.text, "html.parser")
        email_field = soup_check.select_one("#UserEmail")
        if email_field:
            save_debug_html(resp.text, "login_failed")
            save_debug_headers(resp, "login_failed_headers")
            err("Login FALLÓ — seguimos en la página de login.")
            if "X-DEBUG" in os.environ:
                print(resp.text[:2000])
            return False

    ok("Login exitoso.")

    # ── 3. GET checks page ───────────────────────────────────────────
    print()
    checks_url = f"{BASE_URL}/admin/users/checks"
    info(f"Obteniendo página de fichajes: {checks_url}…")
    t0 = time.time()
    resp = session.get(checks_url, timeout=30)
    t1 = time.time()
    ok(f"Status {resp.status_code} en {t1-t0:.1f}s | URL final: {resp.url}")

    if "login" in resp.url.lower():
        save_debug_html(resp.text, "checks_redirected_login")
        save_debug_headers(resp, "checks_redirected_login_headers")
        err("Redirigido a login — la sesión no se estableció correctamente.")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")

    btn = find_check_button(soup)
    if not btn:
        err("No se encontró #check_button.")
        save_debug_html(resp.text, "checks_no_button")
        for tag in ["a", "button"]:
            els = soup.select(f"{tag}[class*='check'], {tag}[class*='fich']")
            if els:
                warn(f"Elementos <{tag}> con clase check/fich:")
                for el in els[:5]:
                    print(f"         {tag}[class='{' '.join(el.get('class', []))}'] text='{el.get_text(strip=True)[:40]}'")
        return False

    btn_classes = btn.get("class", [])
    btn_text = btn.get_text(strip=True)
    btn_href = btn.get("href", "")
    btn_id = btn.get("id", "")
    info(f"Botón: id='{btn_id}' texto='{btn_text}' href='{btn_href}'")
    info(f"  Clases: {btn_classes}")

    current_state = extract_state_from_button(btn)
    if current_state:
        ok(f"Estado actual: {current_state}")
    else:
        warn(f"No se pudo determinar estado. Clases: {btn_classes} Texto: '{btn_text}'")

    # ── 4. Hacer fichaje (si --check) ────────────────────────────────
    if DO_CHECK and btn_href:
        print()
        info(f"Haciendo fichaje -> {btn_href}...")

        check_url = urljoin(checks_url, btn_href)
        t0 = time.time()
        resp = session.get(check_url, timeout=30, allow_redirects=True)
        t1 = time.time()
        ok(f"GET {check_url} -> Status {resp.status_code} en {t1-t0:.1f}s | URL: {resp.url}")

        if "login" in resp.url.lower():
            warn("GET redirigio a login. Probando POST...")
            t0 = time.time()
            resp = session.post(check_url, timeout=30, allow_redirects=True)
            t1 = time.time()
            ok(f"POST {check_url} -> Status {resp.status_code} en {t1-t0:.1f}s | URL: {resp.url}")

        time.sleep(1)

        # ── 5. Verificar estado post-fichaje ─────────────────────────
        print()
        info("Verificando estado despues del fichaje...")

        resp = session.get(checks_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        btn_after = find_check_button(soup)

        if btn_after:
            new_classes = btn_after.get("class", [])
            new_text = btn_after.get_text(strip=True)
            info(f"Boton ahora: texto='{new_text}' clases={new_classes}")
            new_state = extract_state_from_button(btn_after)
            if new_state:
                info(f"Nuevo estado: {new_state}")
                if new_state != current_state:
                    print()
                    ok("FICHAJE EXITOSO - el estado cambio correctamente.")
                else:
                    print()
                    warn("El estado NO cambio. Reintenta el comando.")
                    save_debug_html(resp.text, "post_check_same_state")
            else:
                warn("No se pudo determinar el nuevo estado.")
                save_debug_html(resp.text, "post_check_no_state")
        else:
            err("No se encontro boton despues del fichaje.")
            save_debug_html(resp.text, "post_check_no_button")
            return False

    elif DO_CHECK and not btn_href:
        err("El botón no tiene href.")

    else:
        print()
        info("Modo solo lectura. Usa --check para fichar de verdad.")

    # ── Guardar cookies de sesión ────────────────────────────────────
    session_file = _user_dir / "http_session.json"
    cookies_dict = {
        "cookies": requests.utils.dict_from_cookiejar(session.cookies),
    }
    session_file.write_text(json.dumps(cookies_dict, indent=2))
    ok(f"Cookies guardadas en: {session_file}")

    print()
    sep()
    return True


if __name__ == "__main__":
    success = main()
    if success:
        print(f"\n{GREEN}  ✅ test_http OK{RESET}\n")
    else:
        print(f"\n{RED}  ❌ test_http FALLÓ. Revisa debug_*.html{RESET}\n")
        sys.exit(1)
