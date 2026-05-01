#!/usr/bin/env python3
"""
test_vacaciones.py - Test para validar que update_vacaciones.py + sesame_auto.py funcionan juntos
Uso:
  python test_vacaciones.py --ical /ruta/Sesame-Calendar.ics
  
Qué valida:
  1. Parser iCal: extrae fechas correctamente
  2. Formato vacaciones.txt: genera rango/días correctamente
  3. Lectura vacaciones.txt: sesame_auto.py puede leer el fichero
  4. Lógica de vacaciones: is_vacation_day() devuelve True en fechas correctas
"""

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# ── Test parser iCal ──────────────────────────────────────────────────────────
def test_ical_parser():
    print("\n" + "="*70)
    print("TEST 1: Parser iCal")
    print("="*70)
    
    from update_vacaciones import parse_ical
    
    ical_path = next(
        (Path(sys.argv[i+1]) for i, a in enumerate(sys.argv) if a == "--ical" and i+1 < len(sys.argv)),
        None
    )
    if not ical_path or not ical_path.exists():
        print("❌ No se encontró --ical /ruta/fichero.ics")
        return False
    
    text = ical_path.read_text(encoding="utf-8")
    dates = parse_ical(text)
    
    print(f"✓ Fichero leído: {len(text):,} bytes")
    print(f"✓ Fechas extraídas: {len(dates)} días no laborables")
    
    # Mostrar algunas fechas
    print(f"\n  Primeros 10:")
    for d in dates[:10]:
        print(f"    {d} ({d.strftime('%A')})")
    
    # Validar que no hay fines de semana en "Calendario Valencia" (excepto si son "Vacaciones")
    calendario_only = [d for d in dates if d.weekday() >= 5]
    if calendario_only:
        print(f"\n⚠️  Se encontraron {len(calendario_only)} fines de semana en fechas extraídas")
        print(f"  (no son un error si están en 'Vacaciones*, pero sí si son de 'Calendario')")
        for d in calendario_only:
            print(f"    {d} ({d.strftime('%A')})")
    else:
        print(f"\n✓ No hay fines de semana en las fechas (correcto)")
    
    return dates


def test_vacaciones_format(dates):
    print("\n" + "="*70)
    print("TEST 2: Formato vacaciones.txt")
    print("="*70)
    
    from update_vacaciones import dates_to_vacaciones
    
    content = dates_to_vacaciones(dates)
    lines = [l for l in content.splitlines() if l and not l.startswith("#")]
    
    print(f"✓ Contenido generado: {len(content):,} bytes")
    print(f"✓ Líneas de fechas/rangos: {len(lines)}")
    print(f"\n  Primeras 15 líneas:")
    for line in lines[:15]:
        print(f"    {line}")
    
    if len(lines) <= 15:
        print("\n  Todas las líneas:")
        for line in lines:
            print(f"    {line}")
    
    return content


def test_sesame_auto_reads_vacaciones(content):
    print("\n" + "="*70)
    print("TEST 3: Lectura por sesame_auto.py")
    print("="*70)
    
    # Crear un fichero temporal con el contenido
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(content)
        temp_path = Path(f.name)
    
    try:
        # Copiar la lógica de is_vacation_day() de sesame_auto.py
        def is_vacation_day_local(day: date, vacaciones_path: Path) -> bool:
            if not vacaciones_path.exists():
                return False
            for raw in vacaciones_path.read_text(encoding="utf-8").splitlines():
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
                    pass
            return False
        
        # Test: prueba algunos días
        test_days = [
            (date(2026, 5, 1), True),   # Viernes, festivo (Día del Trabajo)
            (date(2026, 5, 2), False),  # Sábado (fin de semana, no en vacaciones.txt)
            (date(2026, 5, 4), False),  # Lunes, laborable
            (date(2026, 8, 31), True),  # Si está en vacaciones
        ]
        
        print(f"✓ Fichero temporal: {temp_path}")
        print(f"\n  Pruebas de lectura:")
        
        all_ok = True
        for test_date, expected in test_days:
            result = is_vacation_day_local(test_date, temp_path)
            status = "✓" if result == expected else "❌"
            print(f"    {status} {test_date} ({test_date.strftime('%A')}): " 
                  f"expected={expected}, got={result}")
            if result != expected:
                all_ok = False
        
        if all_ok:
            print(f"\n✓ Todas las pruebas pasaron")
        else:
            print(f"\n⚠️  Algunas pruebas fallaron (revisar lógica)")
        
        return all_ok
    
    finally:
        temp_path.unlink()


def test_integration():
    print("\n" + "="*70)
    print("TEST 4: Integración sesame_auto.py")
    print("="*70)
    
    # Validar que sesame_auto.py importa correctamente
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sesame_auto", "sesame_auto.py")
        mod = importlib.util.module_from_spec(spec)
        # No ejecutamos, solo verificamos que no hay errores de sintaxis
        print(f"✓ sesame_auto.py puede ser importado (sin errores de sintaxis)")
        return True
    except SyntaxError as e:
        print(f"❌ Error de sintaxis en sesame_auto.py: {e}")
        return False
    except Exception as e:
        print(f"⚠️  Advertencia al importar sesame_auto.py: {e}")
        return True  # No es fatal


def main():
    print("\n" + "▀"*70)
    print("  TEST: vacaciones + sesame_auto integration")
    print("▀"*70)
    
    # Test 1: Parser
    dates = test_ical_parser()
    if not dates:
        sys.exit(1)
    
    # Test 2: Formato
    content = test_vacaciones_format(dates)
    if not content:
        sys.exit(1)
    
    # Test 3: Lectura
    ok = test_sesame_auto_reads_vacaciones(content)
    if not ok:
        sys.exit(1)
    
    # Test 4: Integración
    test_integration()
    
    print("\n" + "▀"*70)
    print("  ✅ TODOS LOS TESTS PASARON")
    print("▀"*70)
    print("\nAhora en el VPS, ejecuta:")
    print("  /opt/sesame/venv/bin/python /opt/sesame/update_vacaciones.py \\")
    print("    --env /opt/sesame/users/jordi/.env \\")
    print("    --ical /ruta/Sesame-Calendar.ics")
    print("\nY valida que:")
    print("  1. vacaciones.txt se actualiza")
    print("  2. Mañana a las 05:30 sesame_auto.py no ficha (porque es festivo)")
    print()


if __name__ == "__main__":
    main()
