#!/usr/bin/env python3
"""
sesame_auto.py - Fichaje automatico para Sesame HR (via HTTP)
==============================================================
Automatiza el fichaje en panel.sesametime.com usando requests HTTP.

Horario diario:
  ~08:00  Check in
  ~13:00  Salida comida
  ~14:00  Vuelta comida
  ~17:xx  Salida (ajustada para acumular ~40 h/semana)

Uso:
  python sesame_auto.py --env users/jordi/.env
  python sesame_auto.py --env users/sofia/.env
"""

import copy
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Faltan dependencias. Ejecuta: pip install requests beautifulsoup4 python-dotenv")
    sys.exit(1)

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


SESAME_EMAIL = os.environ.get("SESAME_EMAIL", "")
SESAME_PASSWORD = os.environ.get("SESAME_PASSWORD", "")
TIMEZONE = os.environ.get("TZ", "Europe/Madrid")
STATE_FILE = _resolve_path("SESAME_STATE", "state.json")
SESSION_FILE = _resolve_path("SESAME_HTTP_SESSION", "http_session.json")
DEBUG_DIR = _resolve_path("SESAME_DEBUG", "debug")
LOG_FILE = str(_resolve_path("SESAME_LOG", "sesame_auto.log"))
VACACIONES_FILE = _resolve_path("SESAME_VACACIONES", "vacaciones.txt")
DRY_RUN = os.environ.get("SESAME_DRY_RUN", "false").lower() == "true"
TARGET_H = float(os.environ.get("SESAME_TARGET_HOURS", "40.0"))

MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 10
HARD_DEADLINE_H = 19

BASE_URL = "https://panel.sesametime.com"
CHECKS_URL = f"{BASE_URL}/admin/users/checks"

SCHEDULE = {
    "check_in":  dict(hour=9,  minute=0,  jitter=15),
    "lunch_out": dict(hour=13, minute=0,  jitter=15),
    "lunch_in":  dict(hour=14, minute=0,  jitter=15),
    "check_out": dict(hour=18, minute=0,  jitter=15),
}

STEP_TRANSITIONS = {
    "check_in":  {"before_state": "OUT", "after_state": "IN"},
    "lunch_out": {"before_state": "IN",  "after_state": "OUT"},
    "lunch_in":  {"before_state": "OUT", "after_state": "IN"},
    "check_out": {"before_state": "IN",  "after_state": "OUT"},
}

_log_path = Path(LOG_FILE)
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


class SesameHTTP:
    """Sesame HR via HTTP requests (sin navegador)."""

    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        self._load_cookies()

    def _cookies_path(self) -> Path:
        return SESSION_FILE

    def _load_cookies(self):
        if self._cookies_path().exists():
            try:
                data = json.loads(self._cookies_path().read_text())
                jar = requests.utils.cookiejar_from_dict(data.get("cookies", {}))
                self.session.cookies = jar
            except Exception:
                pass

    def _save_cookies(self):
        self._cookies_path().parent.mkdir(parents=True, exist_ok=True)
        data = {"cookies": requests.utils.dict_from_cookiejar(self.session.cookies)}
        self._cookies_path().write_text(json.dumps(data, indent=2))

    def _save_debug(self, resp: requests.Response, label: str):
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = DEBUG_DIR / f"{ts}_{label}.html"
        html_path.write_text(resp.text, encoding="utf-8")
        log.info(f"  HTML guardado: {html_path}")

    def login(self) -> bool:
        log.info("  Iniciando sesion...")
        try:
            resp = self.session.get(BASE_URL, timeout=30)
            soup = BeautifulSoup(resp.text, "html.parser")
            form = soup.select_one("#UserLoginForm")
            if not form:
                log.info("  No hay formulario de login - sesion ya activa.")
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

            self._save_debug(resp, "login_failed")
            log.error("  Login fallido - seguir en pagina de login.")
            return False
        except requests.RequestException as e:
            log.error(f"  Error de red en login: {e}")
            return False

    def get_check_state(self) -> str | None:
        """Devuelve 'IN', 'OUT' o None."""
        try:
            resp = self.session.get(CHECKS_URL, timeout=30)
            if "login" in resp.url.lower():
                if not self.login():
                    return None
                resp = self.session.get(CHECKS_URL, timeout=30)

            self._save_cookies()
            soup = BeautifulSoup(resp.text, "html.parser")
            btn = soup.select_one("#check_button")
            if not btn:
                log.warning("  No se encontro #check_button en la pagina.")
                return None

            return self._state_from_button(btn)
        except requests.RequestException as e:
            log.error(f"  Error de red obteniendo estado: {e}")
            return None

    def do_toggle(self) -> bool:
        """Pulsa el boton de fichaje."""
        try:
            resp = self.session.get(CHECKS_URL, timeout=30)
            if "login" in resp.url.lower():
                if not self.login():
                    return False
                resp = self.session.get(CHECKS_URL, timeout=30)

            self._save_cookies()
            soup = BeautifulSoup(resp.text, "html.parser")
            btn = soup.select_one("#check_button")
            if not btn:
                log.error("  No se encontro boton de fichaje.")
                return False

            href = btn.get("href", "")
            if not href:
                log.error("  Boton sin href.")
                return False

            check_url = urljoin(CHECKS_URL, href)
            log.info(f"  Toggle: {check_url}")
            resp = self.session.get(check_url, timeout=30, allow_redirects=True)

            if "login" in resp.url.lower():
                log.error("  Redirigido a login tras toggle.")
                return False

            return True
        except requests.RequestException as e:
            log.error(f"  Error de red en toggle: {e}")
            return False

    def do_check(self, action_label: str, expected_before: str = None, expected_after: str = None) -> bool:
        """Fichaje completo con verificacion de estado."""
        try:
            resp = self.session.get(CHECKS_URL, timeout=30)
            if "login" in resp.url.lower():
                if not self.login():
                    return False
                resp = self.session.get(CHECKS_URL, timeout=30)

            self._save_cookies()
            soup = BeautifulSoup(resp.text, "html.parser")
            current_state = self._state_from_button(soup.select_one("#check_button"))

            if expected_before and expected_after:
                if current_state == expected_after:
                    log.info(f"  Ya en {expected_after} - no hace falta fichar.")
                    return True
                if current_state == expected_before:
                    log.info(f"  Estado correcto ({current_state} -> {expected_after})")
                else:
                    log.warning(f"  Estado inesperado: {current_state} (esperaba {expected_before})")
                    if current_state == "OUT" and expected_after == "OUT":
                        log.warning("  Recuperacion: Check IN primero...")
                        if not self.do_toggle():
                            return False
                        time.sleep(1)
                        if self.get_check_state() != "IN":
                            log.error("  Recuperacion fallo.")
                            return False
                    elif current_state == "IN" and expected_after == "IN":
                        log.warning("  Recuperacion: Check OUT primero...")
                        if not self.do_toggle():
                            return False
                        time.sleep(1)
                        if self.get_check_state() != "OUT":
                            log.error("  Recuperacion fallo.")
                            return False

            if not self.do_toggle():
                return False

            if expected_after:
                time.sleep(1)
                new_state = self.get_check_state()
                if new_state == expected_after:
                    log.info(f"  [OK] Estado verificado: {new_state}")
                else:
                    log.warning(f"  Estado tras fichaje: {new_state} (esperaba {expected_after})")
                    return False

            return True
        except requests.RequestException as e:
            log.error(f"  Error de red en do_check: {e}")
            return False

    @staticmethod
    def _state_from_button(btn) -> str | None:
        if btn is None:
            return None
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
        "date": date.today().isoformat(), "done": [], "actual_times": {},
    }
    if step not in sched.get("done", []):
        sched.setdefault("done", []).append(step)
    sched.setdefault("actual_times", {})[step] = actual_time
    state["today_schedule"] = sched
    _save_state(state)


def _apply_schedule_overrides(schedule: dict) -> dict:
    """Aplica sobrescrituras del .env al horario.

    Formato CSV (5 valores: lun,mar,mie,jue,vie):
      SESAME_CHECK_IN_1_HOUR=9,9,10,9,9
      SESAME_CHECK_OUT_1_HOUR=13,13,13,13,13
      SESAME_CHECK_IN_2_HOUR=14,14,14,14,14
      SESAME_CHECK_OUT_2_HOUR=18,18,19,18,18

    Tambien soporta los formatos anteriores como fallback:
      SESAME_CHECK_IN_HOUR=10
      SESAME_TUESDAY_CHECK_IN_HOUR=11
    """
    sched = copy.deepcopy(schedule)

    # Mapa: CSV prefix -> schedule key
    csv_map = {
        "SESAME_CHECK_IN_1":    "check_in",
        "SESAME_CHECK_OUT_1":   "lunch_out",
        "SESAME_CHECK_IN_2":    "lunch_in",
        "SESAME_CHECK_OUT_2":   "check_out",
    }

    weekday = date.today().weekday()  # 0=lun .. 4=vie

    # 1. CSV overrides (5 valores por semana)
    for csv_prefix, step_key in csv_map.items():
        for field in ("HOUR", "MINUTE", "JITTER"):
            raw = os.environ.get(f"{csv_prefix}_{field}")
            if raw:
                parts = [p.strip() for p in raw.split(",")]
                if len(parts) == 5 and parts[weekday]:
                    sched[step_key][field.lower()] = int(parts[weekday])

    # 2. Per-day y global overrides (tradicional, tienen prioridad)
    day_name = date.today().strftime("%A").upper()
    for step_key in sched:
        step_prefix = step_key.upper()
        for field in ("HOUR", "MINUTE", "JITTER"):
            val = os.environ.get(f"SESAME_{day_name}_{step_prefix}_{field}") or \
                  os.environ.get(f"SESAME_{step_prefix}_{field}")
            if val is not None:
                sched[step_key][field.lower()] = int(val)

    return sched


def _jittered(hour: int, minute: int, jitter: int, tz) -> datetime:
    base = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_sec = random.randint(-jitter * 60, jitter * 60)
    return base + timedelta(seconds=delta_sec)


def build_schedule(tz, week_hours: float, schedule: dict = None) -> dict:
    if schedule is None:
        schedule = SCHEDULE
    today = date.today()
    days_left = 5 - today.weekday()
    target_today = (TARGET_H - week_hours) / max(days_left, 1)
    target_today = max(6.5, min(9.5, target_today))

    t_in = _jittered(**schedule["check_in"], tz=tz)
    t_lo = _jittered(**schedule["lunch_out"], tz=tz)
    t_li = _jittered(**schedule["lunch_in"], tz=tz)

    morning_h = (t_lo - t_in).total_seconds() / 3600
    afternoon_h = target_today - morning_h
    t_out = t_li + timedelta(hours=afternoon_h)
    t_out += timedelta(seconds=random.randint(-6 * 60, 6 * 60))

    co_hour = schedule["check_out"]["hour"]
    lo_limit = t_li.replace(hour=max(6, co_hour - 1), minute=0, second=0)
    hi_limit = t_li.replace(hour=min(23, co_hour + 1), minute=30, second=0)
    if t_out < lo_limit:
        t_out = lo_limit + timedelta(seconds=random.randint(0, 5 * 60))
    if t_out > hi_limit:
        t_out = hi_limit - timedelta(seconds=random.randint(0, 5 * 60))

    return {
        "date": today.isoformat(), "done": [], "actual_times": {},
        "target_hours": round(target_today, 2),
        "check_in": t_in.isoformat(), "lunch_out": t_lo.isoformat(),
        "lunch_in": t_li.isoformat(), "check_out": t_out.isoformat(),
    }


def _sleep_until(target: datetime):
    wait = (target - datetime.now(target.tzinfo)).total_seconds()
    if wait <= 0:
        return
    log.info(f"  [WAIT] Esperando {wait / 60:.1f} min hasta {target.strftime('%H:%M:%S')} ...")
    time.sleep(wait)


def is_vacation_day(day: date) -> bool:
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
                end = date.fromisoformat(end_s.strip())
                if start <= day <= end:
                    return True
            else:
                if date.fromisoformat(line) == day:
                    return True
        except ValueError:
            log.warning(f"  vacaciones.txt: linea no reconocida -> '{line}'")
    return False


def validate_config() -> bool:
    ok = True
    if not SESAME_EMAIL:
        log.error("SESAME_EMAIL no configurado.")
        ok = False
    if not SESAME_PASSWORD:
        log.error("SESAME_PASSWORD no configurado.")
        ok = False
    return ok


def main():
    if not validate_config():
        log.error("Configuracion incompleta. Revisa el fichero .env")
        sys.exit(1)

    tz = ZoneInfo(TIMEZONE)
    today = date.today()

    if today.weekday() >= 5:
        log.info(f"Fin de semana ({today.strftime('%A')}), no hay nada que hacer.")
        return

    if is_vacation_day(today):
        log.info(f"  Hoy ({today.isoformat()}) es dia de vacaciones. No se ficha.")
        return

    log.info("=" * 55)
    log.info(f"  sesame_auto arrancando para {today.isoformat()}")
    log.info(f"  {'[DRY RUN] - no se haran fichajes reales' if DRY_RUN else 'MODO REAL'}")
    log.info("=" * 55)

    state = _load_state()
    week_h = get_week_hours(state)
    log.info(f"Horas esta semana: {week_h:.2f}h / {TARGET_H}h")

    sched = get_today_schedule(state)
    if sched is None:
        effective_schedule = _apply_schedule_overrides(SCHEDULE)
        sched = build_schedule(tz, week_h, effective_schedule)
        set_today_schedule(state, sched)

    fmt = lambda s: datetime.fromisoformat(s).strftime("%H:%M")
    log.info(
        f"Horario hoy -> entrada: {fmt(sched['check_in'])}"
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

    check_in_time = None
    check_out_time = None
    lunch_out_done = False
    lunch_in_done = False

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

        if target_dt < now - timedelta(minutes=45) and step_name != "check_out":
            log.warning(
                f"  {step_name}: hora programada ({target_dt.strftime('%H:%M')}) "
                "paso hace >45 min - omitiendo."
            )
            continue

        _sleep_until(target_dt)

        transition = STEP_TRANSITIONS.get(step_name, {})
        expected_before = transition.get("before_state")
        expected_after = transition.get("after_state")

        if DRY_RUN:
            log.info(f"  [DRY RUN] {action_label}: {expected_before} -> {expected_after}")
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

        success = False
        sesame = SesameHTTP()
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                backoff = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 120)
                log.warning(
                    f"  [RETRY] Reintentando {step_name} en {backoff}s "
                    f"(intento {attempt + 1}/{MAX_RETRIES + 1})..."
                )
                time.sleep(backoff)
                sesame = SesameHTTP()

            success = sesame.do_check(action_label, expected_before, expected_after)
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
        else:
            log.error(f"  [!]  {step_name} FALLO.")

    # ── Garantia de desfichaje ──────────────────────────────────────
    if not is_done(state, "check_out") and check_out_time is None:
        log.warning("  [!]  check_out no completado. Intentando garantia de desfichaje...")
        hard_deadline = datetime.now(tz).replace(hour=HARD_DEADLINE_H, minute=0, second=0)
        attempt = 0
        while datetime.now(tz) < hard_deadline:
            attempt += 1
            log.info(f"  Intento de garantia #{attempt}...")
            sesame = SesameHTTP()
            actual_state = sesame.get_check_state()
            if actual_state == "OUT":
                log.info("  [OK] Ya desfichado (OUT).")
                mark_done(state, "check_out", datetime.now(tz).isoformat())
                check_out_time = datetime.now(tz)
                break
            elif actual_state == "IN":
                if sesame.do_toggle():
                    time.sleep(1)
                    if sesame.get_check_state() == "OUT":
                        mark_done(state, "check_out", datetime.now(tz).isoformat())
                        check_out_time = datetime.now(tz)
                        log.info("  [OK] Garantia de desfichaje completada.")
                        break
            time.sleep(300)

        if check_out_time is None:
            log.error("  [FAIL] No se pudo garantizar el desfichaje. INTERVENCION MANUAL NECESARIA.")

    # ── Actualizar horas semanales ──────────────────────────────────
    if check_in_time and check_out_time:
        lunch_h = 0.0
        if lunch_out_done and lunch_in_done:
            lunch_h = (
                datetime.fromisoformat(sched["lunch_in"]).replace(tzinfo=tz)
                - datetime.fromisoformat(sched["lunch_out"]).replace(tzinfo=tz)
            ).total_seconds() / 3600
        elif lunch_out_done and not lunch_in_done:
            log.warning("  [!] lunch_in fallo - no se resta comida.")
        elif not lunch_out_done and not lunch_in_done:
            log.warning("  [!] Sin pausa de comida registrada.")

        worked = (check_out_time - check_in_time).total_seconds() / 3600 - lunch_h
        add_week_hours(state, worked)
        log.info(
            f"Horas hoy: {worked:.2f}h  |  "
            f"Semana: {get_week_hours(state):.2f}h / {TARGET_H}h"
        )
    else:
        log.warning("  No se pudo calcular horas: falta check_in o check_out.")

    log.info("=" * 55)
    log.info("  sesame_auto finalizado")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
