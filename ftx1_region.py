#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ftx1_region.py - FTX-1 EEPROM / region tool over CAT.

by R6AAF

Provided "as is", without warranty of any kind.
Предоставляется «как есть», без каких-либо гарантий.
Use at your own risk / используйте на свой риск.

Reads and writes the FTX-1 settings EEPROM through the undocumented
service commands SPR / SPW that are implemented in MAIN firmware but
missing from the CAT manual.  The destination (region) code lives at
EEPROM 0x0109 (u16 big-endian), followed by a flag byte at 0x010B.

Commands
    ports                    list serial ports and mark the CAT one
    info                     identity, firmware, current region
    list                     show every known region code
    dump                     copy EEPROM bytes to a .bin file (+ sidecar)
    set CODE                 change the region (asks for confirmation)
    read ADDR                read one EEPROM byte
    write ADDR VALUE         write one EEPROM byte (advanced, asks twice)

Examples
    python ftx1_region.py ports
    python ftx1_region.py info
    python ftx1_region.py dump
    python ftx1_region.py dump --full --out eeprom_full.bin
    python ftx1_region.py set B5A7
    python ftx1_region.py set "B2(EU)_MC"

Requires: Python 3.8+, pyserial (installed automatically on first run).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import subprocess
import sys
import time
from datetime import datetime


# --------------------------------------------------------------------------
# dependency bootstrap
# --------------------------------------------------------------------------
def _write(stream, text: str) -> None:
    """write() that never dies on a narrow console code page"""
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(enc, "replace").decode(enc, "replace")
    stream.write(text)
    stream.flush()


# --------------------------------------------------------------------------
# interface language
# --------------------------------------------------------------------------
LANG_VALUES = ("ru", "en", "both")
LANG = "ru"

STRINGS = {
    "ru": {
        # dependency bootstrap
        "dep.auto_disabled":
            "pyserial не установлен, автоустановка отключена "
            "(FTX1_NO_AUTOINSTALL).\n"
            "Установите вручную:  python -m pip install pyserial\n",
        "dep.installing": "pyserial не найден - устанавливаю зависимости ...\n",
        "dep.pip_missing": "pip недоступен - включаю его (ensurepip) ...\n",
        "dep.failed":
            "\nНе удалось установить pyserial автоматически.\n"
            "Установите вручную:  python -m pip install pyserial\n"
            "Без интернета: скачайте pyserial и поставьте офлайн:\n"
            "  python -m pip install pyserial-3.5-py2.py3-none-any.whl\n",
        "dep.not_visible":
            "\npyserial установлен, но этот запуск Python его ещё не видит.\n"
            "Закройте окно и запустите скрипт снова.\n",
        "dep.installed": "pyserial установлен.\n\n",

        # language selector
        "lang.title":
            "\nВыберите язык / Choose language:\n\n"
            "  1  Русский\n"
            "  2  English\n"
            "  3  Оба / Both\n",
        "lang.prompt": "\nязык / language> ",
        "lang.invalid": "введите 1, 2 или 3 / enter 1, 2 or 3\n",

        # errors
        "err.bad_code": "это не код региона и не имя: {text}  (см. 'list')",
        "err.no_port":
            "не удалось определить порт CAT автоматически.\n"
            "Запустите 'python ftx1_region.py ports' и укажите --port COMx",

        # regions
        "region.unknown": "неизвестен",
        "describe.unknown": "код не найден",

        # ports
        "ports.title": "COM-порты:",
        "ports.not_found":
            "\nНе видно порта CP2105 'Enhanced'. "
            "Радио включено? Кабель подключён?",

        # list
        "list.title": "Регионы FTX-1 (MAIN V01.13)",
        "list.col.code": "код",
        "list.col.name": "имя",
        "list.col.wide": "широкий",
        "list.col.desc": "описание",
        "list.wide_yes": "да",
        "list.footnote": "«широкий» = сплошное КВ, то есть MARS/CAP.",
        "list.howto": "Смена:  python ftx1_region.py set 0xB5A7   (или просто set)",

        # info
        "info.port": "порт       : {port} @ {baud}",
        "info.id": "ID         : {ident}",
        "info.fw": "прошивка   : {fw}",
        "info.region": "регион     : 0x{code:04X}  {name}",
        "info.bytes": "байты      : {hi:02X} {lo:02X}  флаг {flag:02X}",
        "info.desc_label": "описание",
        "info.unknown_warn":
            "ВНИМАНИЕ: код не из таблицы - не пишите его вслепую.",
        "info.tx_ranges": "Диапазоны передачи ({n}):",
        "info.no_tx": "Отдельной таблицы передачи у этого кода нет.",

        # dump
        "dump.progress":
            "\r  {pct:5.1f}%  {done:04X}/{total:04X}  {ms:5.1f} мс/байт  "
            "осталось {m:3d}:{s:02d}   ",
        "dump.second_pass": "  второй проход для проверки ...",
        "dump.mismatch": "  РАСХОЖДЕНИЕ 0x{addr:04X}: {old:02X} vs {new:02X}",
        "dump.verify_done": "  проверка завершена, различий: {n}",
        "dump.saved": "сохранено {path} ({n} байт, сбоев {bad}) + {meta}",
        "dump.range_past": "диапазон выходит за 0xFFFF (SPR 16-битный)",
        "dump.header": "порт {port}, 0x{start:04X}..0x{end:04X} ({n} байт), {dur}",
        "dump.dur_sec": "~{n} с",
        "dump.dur_min": "~{n:.1f} мин",
        "dump.id": "ID {ident}",
        "dump.warn_bad":
            "ВНИМАНИЕ: {n} байт не прочитаны (записаны как 0xFF)",

        # set
        "set.enter_code": "\nВведите код региона или имя: ",
        "set.aborted": "отменено",
        "set.unknown_force":
            "0x{code:04X} - неизвестный регион; "
            "используйте --force, чтобы всё равно записать",
        "set.port": "порт       : {port}",
        "set.id": "ID         : {ident}",
        "set.current": "сейчас     : 0x{code:04X}  {name}",
        "set.desc_label": "описание",
        "set.already": "ничего не делаю: регион уже 0x{code:04X} ({name})",
        "set.current_unknown":
            "текущий код 0x{code:04X} неизвестен - отказываюсь писать.\n"
            "Сначала сделайте дамп и повторите с --force.",
        "set.will_be": "станет     : 0x{code:04X}  {name}",
        "set.unknown_name": "<неизвестный>",
        "set.dry_run": "\nпробный запуск (dry run): ничего не записано",
        "set.backup": "\nавтобэкап 0x0000..0x{end:04X}:",
        "set.backup_bad":
            "\nв бэкапе {n} непрочитанных байт - НИЧЕГО не пишу.",
        "set.backup_hint": "Проверьте USB-кабель/порт и повторите команду.",
        "set.backup_mismatch":
            "\nбэкап не совпал с регионом, прочитанным только что "
            "({got} vs {want}) - отмена",
        "set.rollback": "откат: python ftx1_region.py set 0x{code:04X}",
        "set.confirm":
            "\nЗаписать 0x{code:04X} в EEPROM 0x{addr_hi:04X}/0x{addr_lo:04X}? "
            "введите 'yes': ",
        "set.cancelled": "отменено, ничего не записано",
        "set.read_failed":
            "\nзапись отправлена, но прочитать обратно не удалось: {err}",
        "set.read_failed_hint":
            "Выключите и включите радио, затем проверьте 'info'.",
        "set.done":
            "\nГОТОВО: в EEPROM теперь 0x{code:04X} ({name}), флаг 0x{flag:02X}",
        "set.done_hint":
            "Выключите и включите радио (полный цикл), затем снова 'info'.",
        "set.readback_error":
            "\nОШИБКА: прочитано 0x{got:04X}, ожидалось 0x{code:04X}",

        # write
        "write.bad_value": "значение должно быть 0..0xFF",
        "write.confirm": "введите 'yes' для записи: ",
        "write.cancelled": "отменено",
        "write.read_failed":
            "запись отправлена, прочитать не удалось; проверьте позже: read {addr}",
        "write.readback": "прочитано 0x{got:02X} {status}",
        "write.ok": "ОК",
        "write.mismatch": "НЕ СОВПАЛО",

        # menu
        "menu.title": "FTX-1: регионы и EEPROM",
        "menu.author": "by R6AAF",
        "menu.item.info": "  1  info    текущий регион, прошивка, диапазоны",
        "menu.item.dump": "  2  dump    дамп EEPROM (первые 4 КБ, ~50 с)",
        "menu.item.list": "  3  list    таблица регионов",
        "menu.item.set": "  4  set     выбрать и записать регион",
        "menu.item.ports": "  5  ports   список COM-портов",
        "menu.item.lang": "  6  lang    сменить язык / change language",
        "menu.item.exit": "  0  выход",
        "menu.prompt": "\nвыбор> ",
        "menu.bad_choice": "нет такого пункта, введите 1-6 или 0",
        "menu.cat_error": "ошибка CAT: {err}",
        "menu.interrupted": "\nпрервано, возврат в меню",

        # main / argparse
        "main.description":
            "FTX-1: EEPROM и регион через CAT (SPR/SPW). "
            "Запуск без команды открывает меню. by R6AAF",
        "main.epilog": "Коды регионов: python ftx1_region.py list",
        "main.cat_error": "ошибка CAT: {err}",
        "main.interrupted": "\nпрервано",
        "help.port": "порт CAT, например COM14 (по умолчанию: авто)",
        "help.baud": "скорость обмена (по умолчанию: {baud})",
        "help.lang": "язык интерфейса: ru, en или both",
        "help.ports": "список COM-портов",
        "help.list": "таблица известных кодов регионов",
        "help.info": "ID, прошивка и текущий регион",
        "help.dump": "сохранить EEPROM в .bin",
        "help.dump_full": "все 64 КБ (медленно, ~13 мин)",
        "help.dump_double": "прочитать дважды и сравнить",
        "help.set": "сменить код региона",
        "help.set_code":
            "hex-код или имя региона, например B5A7 или \"B2(EU)_MC\"; "
            "без аргумента - выбрать интерактивно",
        "help.yes": "не спрашивать подтверждение",
        "help.force": "разрешить неизвестные коды",
        "help.dry_run": "показать, что изменится, ничего не записывая",
        "help.read": "прочитать один байт EEPROM",
        "help.write": "записать один байт EEPROM (для продвинутых)",
    },
    "en": {
        # dependency bootstrap
        "dep.auto_disabled":
            "pyserial is not installed and auto-install is disabled "
            "(FTX1_NO_AUTOINSTALL).\n"
            "Install it manually:  python -m pip install pyserial\n",
        "dep.installing": "pyserial not found - installing dependencies ...\n",
        "dep.pip_missing": "pip is unavailable - enabling it (ensurepip) ...\n",
        "dep.failed":
            "\nCould not install pyserial automatically.\n"
            "Install it manually:  python -m pip install pyserial\n"
            "Offline: download pyserial and install from the wheel:\n"
            "  python -m pip install pyserial-3.5-py2.py3-none-any.whl\n",
        "dep.not_visible":
            "\npyserial was installed, but this Python run cannot see it yet.\n"
            "Close the window and start the script again.\n",
        "dep.installed": "pyserial installed.\n\n",

        # language selector
        "lang.title":
            "\nВыберите язык / Choose language:\n\n"
            "  1  Русский\n"
            "  2  English\n"
            "  3  Оба / Both\n",
        "lang.prompt": "\nязык / language> ",
        "lang.invalid": "enter 1, 2 or 3 / введите 1, 2 или 3\n",

        # errors
        "err.bad_code": "not a region code or name: {text}  (see 'list')",
        "err.no_port":
            "cannot auto-detect the CAT port.\n"
            "Run 'python ftx1_region.py ports' and pass --port COMx",

        # regions
        "region.unknown": "unknown",
        "describe.unknown": "unknown code",

        # ports
        "ports.title": "COM ports:",
        "ports.not_found":
            "\nNo CP2105 'Enhanced' port seen. "
            "Is the radio on and the USB cable connected?",

        # list
        "list.title": "FTX-1 regions (MAIN V01.13)",
        "list.col.code": "code",
        "list.col.name": "name",
        "list.col.wide": "wide",
        "list.col.desc": "description",
        "list.wide_yes": "yes",
        "list.footnote": "'wide' = continuous HF coverage, i.e. MARS/CAP style.",
        "list.howto": "Change:  python ftx1_region.py set 0xB5A7   (or just set)",

        # info
        "info.port": "port       : {port} @ {baud}",
        "info.id": "ID         : {ident}",
        "info.fw": "firmware   : {fw}",
        "info.region": "region     : 0x{code:04X}  {name}",
        "info.bytes": "bytes      : {hi:02X} {lo:02X}  flag {flag:02X}",
        "info.desc_label": "description",
        "info.unknown_warn":
            "WARNING: this code is not in the table - do not write it blindly.",
        "info.tx_ranges": "TX ranges ({n}):",
        "info.no_tx": "This code has no separate TX table.",

        # dump
        "dump.progress":
            "\r  {pct:5.1f}%  {done:04X}/{total:04X}  {ms:5.1f} ms/byte  "
            "ETA {m:3d}:{s:02d}   ",
        "dump.second_pass": "  second pass for verification ...",
        "dump.mismatch": "  MISMATCH at 0x{addr:04X}: {old:02X} vs {new:02X}",
        "dump.verify_done": "  verification done, differing bytes: {n}",
        "dump.saved": "saved {path} ({n} bytes, bad {bad}) + {meta}",
        "dump.range_past": "range goes past 0xFFFF (SPR is 16-bit)",
        "dump.header":
            "port {port}, 0x{start:04X}..0x{end:04X} ({n} bytes), {dur}",
        "dump.dur_sec": "~{n} s",
        "dump.dur_min": "~{n:.1f} min",
        "dump.id": "ID {ident}",
        "dump.warn_bad": "WARNING: {n} bytes could not be read (stored as 0xFF)",

        # set
        "set.enter_code": "\nEnter region code or name: ",
        "set.aborted": "aborted",
        "set.unknown_force":
            "0x{code:04X} is not a known region; use --force to write it anyway",
        "set.port": "port       : {port}",
        "set.id": "ID         : {ident}",
        "set.current": "current    : 0x{code:04X}  {name}",
        "set.desc_label": "description",
        "set.already": "nothing to do: region already 0x{code:04X} ({name})",
        "set.current_unknown":
            "current code 0x{code:04X} is unknown - refusing to write.\n"
            "Make a dump first and re-run with --force.",
        "set.will_be": "target     : 0x{code:04X}  {name}",
        "set.unknown_name": "<unknown>",
        "set.dry_run": "\ndry run: nothing written",
        "set.backup": "\nautomatic backup 0x0000..0x{end:04X}:",
        "set.backup_bad":
            "\nbackup has {n} unreadable bytes - NOT writing anything.",
        "set.backup_hint": "Check the USB cable/port and run the command again.",
        "set.backup_mismatch":
            "\nbackup does not match the region read a moment ago "
            "({got} vs {want}) - aborting",
        "set.rollback": "rollback: python ftx1_region.py set 0x{code:04X}",
        "set.confirm":
            "\nWrite 0x{code:04X} to EEPROM 0x{addr_hi:04X}/0x{addr_lo:04X}? "
            "type 'yes': ",
        "set.cancelled": "aborted, nothing written",
        "set.read_failed":
            "\nwrite sent, but the readback failed: {err}",
        "set.read_failed_hint":
            "Power the radio off and on, then run 'info'.",
        "set.done":
            "\nOK: region bytes now 0x{code:04X} ({name}), flag 0x{flag:02X}",
        "set.done_hint":
            "Power the radio off and on (full cycle), then run 'info' again.",
        "set.readback_error":
            "\nERROR: readback 0x{got:04X}, expected 0x{code:04X}",

        # write
        "write.bad_value": "value must be 0..0xFF",
        "write.confirm": "type 'yes' to write: ",
        "write.cancelled": "aborted",
        "write.read_failed":
            "write sent, readback failed; check later with 'read {addr}'",
        "write.readback": "readback 0x{got:02X} {status}",
        "write.ok": "OK",
        "write.mismatch": "MISMATCH",

        # menu
        "menu.title": "FTX-1: regions and EEPROM",
        "menu.author": "by R6AAF",
        "menu.item.info": "  1  info    current region, firmware, TX ranges",
        "menu.item.dump": "  2  dump    back up EEPROM (first 4 KiB, ~50 s)",
        "menu.item.list": "  3  list    all known region codes",
        "menu.item.set": "  4  set     choose a region and write it",
        "menu.item.ports": "  5  ports   list serial ports",
        "menu.item.lang": "  6  lang    change language / сменить язык",
        "menu.item.exit": "  0  exit",
        "menu.prompt": "\nchoice> ",
        "menu.bad_choice": "unknown choice, enter 1-6 or 0",
        "menu.cat_error": "CAT error: {err}",
        "menu.interrupted": "\ninterrupted, back to menu",

        # main / argparse
        "main.description":
            "FTX-1 EEPROM / region tool over CAT (SPR/SPW). "
            "Run without a command for the interactive menu. "
            "by R6AAF",
        "main.epilog": "Region codes: run 'python ftx1_region.py list'",
        "main.cat_error": "CAT error: {err}",
        "main.interrupted": "\ninterrupted",
        "help.port": "CAT port, e.g. COM14 (default: auto)",
        "help.baud": "baud rate (default: {baud})",
        "help.lang": "interface language: ru, en or both",
        "help.ports": "list serial ports",
        "help.list": "list known region codes",
        "help.info": "identity and current region",
        "help.dump": "copy EEPROM to a .bin file",
        "help.dump_full": "all 64 KiB (slow, ~13 min)",
        "help.dump_double": "read twice and compare",
        "help.set": "change the region code",
        "help.set_code":
            "hex code or region name, e.g. B5A7 or \"B2(EU)_MC\"; "
            "omit to pick interactively",
        "help.yes": "no confirmation prompt",
        "help.force": "allow unknown codes",
        "help.dry_run": "show what would change, write nothing",
        "help.read": "read one EEPROM byte",
        "help.write": "write one EEPROM byte (advanced)",
    },
}

STRINGS["both"] = dict(STRINGS["ru"])


def T(key: str, **kw) -> str:
    """Look up a UI string in the selected language."""
    table = STRINGS.get(LANG) or STRINGS["ru"]
    text = table.get(key)
    if text is None:
        text = STRINGS["ru"].get(key, key)
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, ValueError, IndexError):
            return text
    return text


# A few strings argparse builds itself.
_ARGPARSE_RU = {
    "positional arguments": "позиционные аргументы",
    "options": "параметры",
    "optional arguments": "параметры",
    "show this help message and exit": "показать эту справку и выйти",
    "usage: ": "использование: ",
}


def _argparse_gettext(message: str) -> str:
    if LANG == "en":
        return message
    return _ARGPARSE_RU.get(message, message)


argparse._ = _argparse_gettext


def _lang_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".ftx1_region_lang")


def _read_saved_lang() -> str | None:
    try:
        with open(_lang_file(), encoding="utf-8") as f:
            v = f.read().strip().lower()
        return v if v in LANG_VALUES else None
    except OSError:
        return None


def _save_lang(value: str) -> None:
    try:
        with open(_lang_file(), "w", encoding="utf-8") as f:
            f.write(value + "\n")
    except OSError:
        pass


def _scan_lang(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--lang" and i + 1 < len(argv):
            v = argv[i + 1].strip().lower()
            if v in LANG_VALUES:
                return v
        elif arg.startswith("--lang="):
            v = arg.split("=", 1)[1].strip().lower()
            if v in LANG_VALUES:
                return v
    return None


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def ask_language(save: bool = True) -> str:
    """Show the RU/EN/Both prompt and store the choice."""
    global LANG
    while True:
        _write(sys.stdout, STRINGS["ru"]["lang.title"])
        _write(sys.stdout, STRINGS["ru"]["lang.prompt"])
        line = sys.stdin.readline()
        if not line:
            break
        v = line.strip().lower()
        if v in ("1", "ru", "r", "рус", "русский"):
            LANG = "ru"
            break
        if v in ("2", "en", "e", "english"):
            LANG = "en"
            break
        if v in ("3", "both", "b", "оба", "оба языка"):
            LANG = "both"
            break
        _write(sys.stdout, STRINGS["ru"]["lang.invalid"])
    if save:
        _save_lang(LANG)
    return LANG


def init_language(argv: list[str] | None = None) -> str:
    """--lang wins, then the saved choice, then an interactive prompt."""
    global LANG
    argv = list(sys.argv[1:] if argv is None else argv)
    explicit = _scan_lang(argv)
    if explicit:
        LANG = explicit          # one-off flag: do not change the saved choice
        return LANG
    saved = _read_saved_lang()
    if saved:
        LANG = saved
        return LANG
    if _is_tty() and not any(a in ("-h", "--help") for a in argv):
        return ask_language(save=True)
    LANG = "ru"
    return LANG


def _pip_install(requirements: list[str]) -> bool:
    """Try a normal install, then a per-user install. True on success."""
    for extra in ([], ["--user"]):
        cmd = [sys.executable, "-m", "pip", "install"] + extra + requirements
        try:
            if subprocess.call(cmd) == 0:
                return True
        except OSError:
            pass
    return False


def _ensure_dependencies() -> None:
    """Install pyserial (and anything in requirements.txt) if it is missing.

    Set FTX1_NO_AUTOINSTALL=1 to disable this and install manually.
    """
    try:
        import serial  # noqa: F401
        return
    except ImportError:
        pass

    if os.environ.get("FTX1_NO_AUTOINSTALL", "").strip() not in ("", "0"):
        _write(sys.stderr, T("dep.auto_disabled"))
        raise SystemExit(2)

    requirements = ["pyserial>=3.5"]
    req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "requirements.txt")
    if os.path.isfile(req_file):
        try:
            with open(req_file, encoding="utf-8") as f:
                parsed = [ln.strip() for ln in f
                          if ln.strip() and not ln.strip().startswith("#")]
            if parsed:
                requirements = parsed
        except OSError:
            pass

    _write(sys.stdout, T("dep.installing"))
    ok = _pip_install(requirements)
    if not ok:
        _write(sys.stdout, T("dep.pip_missing"))
        try:
            subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except OSError:
            pass
        ok = _pip_install(requirements)

    if not ok:
        _write(sys.stderr, T("dep.failed"))
        raise SystemExit(2)

    importlib.invalidate_caches()
    try:
        import serial  # noqa: F401
    except ImportError:
        _write(sys.stderr, T("dep.not_visible"))
        raise SystemExit(2)

    _write(sys.stdout, T("dep.installed"))


init_language()
_ensure_dependencies()

import serial  # noqa: E402
from serial.tools import list_ports  # noqa: E402

# --------------------------------------------------------------------------
# Hardware constants (FTX-1 MAIN V01.13)
# --------------------------------------------------------------------------
REGION_ADDR = 0x0109       # u16 big-endian
REGION_FLAG_ADDR = 0x010B  # 1 byte, meaning unknown - never touched
HEADER_LEN = 0x0200        # bytes backed up automatically before a write
MAX_ADDR = 0xFFFF          # SPR/SPW use a 16-bit address
DEFAULT_BAUD = 38400
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(BASE_DIR, "dumps")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

# Destination codes decoded from the tx_freq_allowed dispatcher
# (MAIN V01.13, compare chain at 0xFFEC7FBB, tables 0xFFE96474..0xFFE967C4).
REGIONS = {
    0x7753: {
        "name": "AUS",
        "wide": False,
        "note": "Australian band plan, 12 narrow rows",
        "rows": [
            (1800000, 1875000), (3500000, 3800000), (7000000, 7300000),
            (10100000, 10150000), (14000000, 14350000), (18068000, 18168000),
            (21000000, 21450000), (24890000, 24990000), (28000000, 29700000),
            (50000000, 54000000), (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0x1CB9: {
        "name": "DEV",
        "wide": True,
        "note": "widest: 30 kHz - 75 MHz continuous",
        "rows": [
            (30000, 75000001), (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0xB5A7: {
        "name": "B2(EU)_MC",
        "wide": True,
        "note": "classic MARS/CAP: 1.8-30 MHz continuous",
        "rows": [
            (1800000, 30000000), (50000000, 54000000), (70000000, 70500000),
            (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0x50E2: {
        "name": "USA_MC",
        "wide": True,
        "note": "same TX table as B2(EU)_MC",
        "rows": [
            (1800000, 30000000), (50000000, 54000000), (70000000, 70500000),
            (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0xA4C5: {
        "name": "C2(UK)_MC",
        "wide": True,
        "note": "same TX table as B2(EU)_MC",
        "rows": [
            (1800000, 30000000), (50000000, 54000000), (70000000, 70500000),
            (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0x315B: {
        "name": "USA",
        "wide": False,
        "note": "23 rows: ham bands + 5 MHz channels",
        "rows": [
            (1800000, 2000000), (3500000, 4000000), (7000000, 7300000),
            (10100000, 10150000), (14000000, 14350000), (18068000, 18168000),
            (21000000, 21450000), (24890000, 24990000), (28000000, 29700000),
            (50000000, 54000000),
            (5167500, 5167501), (5332000, 5332001), (5348000, 5348001),
            (5358500, 5358501), (5373000, 5373001), (5405000, 5405001),
            (5330500, 5330501), (5346500, 5346501), (5357000, 5357001),
            (5371500, 5371501), (5403500, 5403501),
            (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0xA983: {
        "name": "B2(EU)",
        "wide": False,
        "note": "13 rows, includes 5351.5 kHz",
        "rows": [
            (1810000, 2000000), (3500000, 3800000), (5351500, 5366501),
            (7000000, 7200000), (10100000, 10150000), (14000000, 14350000),
            (18068000, 18168000), (21000000, 21450000), (24890000, 24990000),
            (28000000, 29700000), (50000000, 52000000),
            (144000000, 146000001), (430000000, 440000001),
        ],
    },
    0xD950: {
        "name": "C2(UK)",
        "wide": False,
        "note": "14 rows, includes 5250 kHz",
        "rows": [
            (1810000, 2000000), (3500000, 3800000), (5250000, 5406501),
            (7000000, 7200000), (10100000, 10150000), (14000000, 14350000),
            (18068000, 18168000), (21000000, 21450000), (24890000, 24990000),
            (28000000, 29700000), (50000000, 52000000), (70000000, 70500000),
            (144000000, 146000001), (430000000, 440000001),
        ],
    },
    0xD32D: {
        "name": "CHINA",
        "wide": False,
        "note": "12 rows",
        "rows": [
            (1800000, 2000000), (3500000, 3900000), (7000000, 7200000),
            (10100000, 10150000), (14000000, 14350000), (18068000, 18168000),
            (21000000, 21450000), (24890000, 24990000), (28000000, 29700000),
            (50000000, 54000000), (144000000, 148000001), (430000000, 440000001),
        ],
    },
    0x6DA0: {
        "name": "EXP(B3)",
        "wide": False,
        "note": "12 rows",
        "rows": [
            (1800000, 2000000), (3500000, 4000000), (7000000, 7300000),
            (10100000, 10150000), (14000000, 14350000), (18068000, 18168000),
            (21000000, 21450000), (24890000, 24990000), (28000000, 29700000),
            (50000000, 54000000), (144000000, 148000001), (430000000, 450000001),
        ],
    },
    0x4380: {
        "name": "blank/default",
        "wide": False,
        "note": "19 sub-band rows (not continuous)",
        "rows": [
            (1800000, 1875000), (1907501, 1912500), (3500000, 3580000),
            (3599000, 3612000), (3662000, 3687000), (3702000, 3716000),
            (3745000, 3770000), (3791000, 3805000), (4630000, 4630001),
            (7000000, 7200000), (10100000, 10150000), (14000000, 14350000),
            (18068000, 18168000), (21000000, 21450000), (24890000, 24990000),
            (28000000, 29700000), (50000000, 54000000),
            (144000000, 146000001), (430000000, 440000001),
        ],
    },
    0x25BA: {
        "name": "JPN",
        "wide": False,
        "note": "no TX table in the dispatcher (firmware default path)",
        "rows": [],
    },
}

WIDE_CODES = [c for c, r in REGIONS.items() if r["wide"]]

# Russian half of every description.  Shown as "EN \ RU".
NOTES_RU = {
    0x7753: "австралийский план, 12 узких участков",
    0x1CB9: "самый широкий: 30 кГц - 75 МГц сплошняком",
    0xB5A7: "классический MARS/CAP: 1.8-30 МГц сплошняком",
    0x50E2: "та же TX-таблица, что у B2(EU)_MC",
    0xA4C5: "та же TX-таблица, что у B2(EU)_MC",
    0x315B: "23 участка: любительские диапазоны + каналы 5 МГц",
    0xA983: "13 участков, включая 5351.5 кГц",
    0xD950: "14 участков, включая 5250 кГц",
    0xD32D: "12 участков",
    0x6DA0: "12 участков",
    0x4380: "19 поддиапазонов (не сплошняком)",
    0x25BA: "отдельной TX-таблицы нет (ветка прошивки по умолчанию)",
}


def describe(code: int) -> str:
    """Region description in the selected language."""
    info = REGIONS.get(code)
    if info is None:
        if LANG == "both":
            return "unknown code \\ код не найден"
        return T("describe.unknown")
    en = info["note"]
    ru = NOTES_RU.get(code, en)
    if LANG == "en":
        return en
    if LANG == "both":
        return "%s \\ %s" % (en, ru)
    return ru


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def say(text: str = "") -> None:
    """print() that never dies on a narrow console code page"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    line = text + "\n"
    try:
        line.encode(enc)
    except (UnicodeEncodeError, LookupError):
        line = line.encode(enc, "replace").decode(enc, "replace")
    sys.stdout.write(line)
    sys.stdout.flush()


def inline(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def fmt_hz(v: int) -> str:
    if v >= 1000000:
        return "%.4f MHz" % (v / 1e6)
    return "%.1f kHz" % (v / 1e3)


def fmt_range(lo: int, hi: int) -> str:
    end = hi - 1 if hi % 1000 == 1 else hi
    mhz = "MHz" if LANG == "en" else "МГц"
    khz = "kHz" if LANG == "en" else "кГц"
    if hi >= 1000000:
        return "%9.4f - %9.4f %s" % (lo / 1e6, end / 1e6, mhz)
    return "%9.1f - %9.1f %s" % (lo / 1e3, end / 1e3, khz)


# -- pretty output ---------------------------------------------------------
def _enc_ok(text: str) -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def box_chars() -> dict:
    if _enc_ok("─│┌┐└┘├┤┬┴┼"):
        return {"h": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
                "lt": "├", "rt": "┤", "tt": "┬", "bt": "┴", "x": "┼"}
    return {"h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
            "lt": "+", "rt": "+", "tt": "+", "bt": "+", "x": "+"}


def wrap_cell(text: str, width: int) -> list:
    words = text.split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def render_table(headers: list, rows: list, maxw: list | None = None) -> list:
    """box-drawn table; long cells wrap on spaces"""
    maxw = maxw or [10 ** 6] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for j, cell in enumerate(row):
            for line in wrap_cell(str(cell), maxw[j]):
                widths[j] = max(widths[j], len(line))

    c = box_chars()

    def rule(left: str, mid: str, right: str) -> str:
        return "  " + left + mid.join(c["h"] * (w + 2) for w in widths) + right

    def line(cells: list) -> str:
        return "  " + c["v"] + c["v"].join(
            " " + cell.ljust(w) + " " for cell, w in zip(cells, widths)) + c["v"]

    out = [rule(c["tl"], c["tt"], c["tr"]), line(headers),
           rule(c["lt"], c["x"], c["rt"])]
    for row in rows:
        cells = [wrap_cell(str(cell), w) for cell, w in zip(row, widths)]
        for i in range(max(len(x) for x in cells)):
            out.append(line([x[i] if i < len(x) else "" for x in cells]))
        out.append(rule(c["lt"], c["x"], c["rt"]))
    out[-1] = rule(c["bl"], c["bt"], c["br"])
    return out


def boxed(lines: list) -> list:
    c = box_chars()
    w = max(len(x) for x in lines)
    out = ["  " + c["tl"] + c["h"] * (w + 2) + c["tr"]]
    for x in lines:
        out.append("  " + c["v"] + " " + x.ljust(w) + " " + c["v"])
    out.append("  " + c["bl"] + c["h"] * (w + 2) + c["br"])
    return out


def say_field(label: str, value: str, width: int = 74) -> None:
    """'label     : text', wrapping long values with a hanging indent"""
    for i, line in enumerate(wrap_cell(value, width)):
        say(("%-11s: %s" % (label, line)) if i == 0 else ("%11s  %s" % ("", line)))


def checksum(frame: bytes) -> int:
    return sum(frame) & 0xFF


def parse_code(text: str) -> int:
    t = text.strip()
    for code, info in REGIONS.items():
        if info["name"].lower() == t.lower():
            return code
    try:
        return int(t, 16)
    except ValueError:
        raise SystemExit(T("err.bad_code", text=repr(text)))


def find_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    fallback = []
    for p in list_ports.comports():
        d = (p.description or "").lower()
        if "enhanced" in d and ("cp210" in d or "silicon" in d):
            return p.device
        if "cp210" in d or "silicon" in d:
            fallback.append(p.device)
    if len(fallback) == 1:
        return fallback[0]
    raise SystemExit(T("err.no_port"))


def ensure_radio(cat: "FtCat") -> str:
    ident = cat.identify()
    if not ident.startswith("ID"):
        raise CatError("no ID reply (radio off, wrong port or wrong baud?)")
    return ident


# --------------------------------------------------------------------------
# CAT transport
# --------------------------------------------------------------------------
class CatError(RuntimeError):
    pass


class FtCat:
    def __init__(self, port: str, baud: int = DEFAULT_BAUD, timeout: float = 0.6):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser: serial.Serial | None = None

    def __enter__(self) -> "FtCat":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                 write_timeout=1.5, rtscts=False, dsrdtr=False)
        # keep DTR/RTS low: they can key the transmitter on some Yaesu models
        self.ser.rts = False
        self.ser.dtr = False

    def close(self) -> None:
        if self.ser is not None and self.ser.is_open:
            self.ser.close()

    def _txn(self, frame: bytes, nbytes: int, timeout: float) -> bytes:
        """send a frame, read until ';' or nbytes (text replies: ID, VE)"""
        assert self.ser is not None
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        deadline = time.time() + timeout
        buf = bytearray()
        while len(buf) < nbytes and time.time() < deadline:
            b = self.ser.read(1)
            if b:
                buf += b
                if b == b";":
                    break
        return bytes(buf)

    def _txn_exact(self, frame: bytes, nbytes: int, timeout: float) -> bytes:
        """send a frame, read exactly nbytes.

        Binary replies must never be terminated by a byte value: in an SPR
        reply the data byte or the checksum can be 0x3B (';'), and stopping
        there shifts every following reply by one byte."""
        assert self.ser is not None
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        deadline = time.time() + timeout
        buf = bytearray()
        while len(buf) < nbytes and time.time() < deadline:
            chunk = self.ser.read(nbytes - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf)

    def drain(self) -> None:
        """drop anything the radio still has in flight"""
        assert self.ser is not None
        self.ser.reset_input_buffer()
        time.sleep(0.02)
        self.ser.reset_input_buffer()

    # -- identity ---------------------------------------------------------
    def identify(self) -> str:
        r = self._txn(b"ID;", 16, 1.0).decode("ascii", "replace").strip()
        if not r.startswith("ID"):
            raise CatError("unexpected reply to ID; : %r (wrong port or desynced link)"
                           % r)
        return r

    def firmware(self) -> str:
        return self._txn(b"VE0;", 16, 1.0).decode("ascii", "replace").strip()

    # -- EEPROM byte access ----------------------------------------------
    def spr(self, addr: int, retries: int = 5) -> int:
        if not 0 <= addr <= MAX_ADDR:
            raise CatError("address 0x%X outside 16-bit range" % addr)
        frame = b"SPR" + bytes([addr >> 8, addr & 0xFF])
        frame += bytes([checksum(frame)]) + b";"
        last = b""
        for attempt in range(retries):
            if attempt:
                self.drain()                 # drop late/desynced bytes
                time.sleep(0.15 * attempt)   # radio may still be busy
            r = self._txn_exact(frame, 8, self.timeout)
            last = r
            if (len(r) == 8 and r[0:3] == b"SPR" and r[7:8] == b";"
                    and r[3] == (addr >> 8) and r[4] == (addr & 0xFF)
                    and checksum(r[0:6]) == r[6]):
                return r[5]
        raise CatError("SPR 0x%04X failed (reply %s)" % (addr, last.hex(" ") or "empty"))

    def spw(self, addr: int, value: int, retries: int = 2) -> None:
        if not 0 <= addr <= MAX_ADDR:
            raise CatError("address 0x%X outside 16-bit range" % addr)
        frame = b"SPW" + bytes([addr >> 8, addr & 0xFF, value & 0xFF])
        frame += bytes([checksum(frame)]) + b";"
        last = b""
        for _ in range(retries):
            r = self._txn(frame, 4, 0.4)
            last = r
            if not r:
                return                      # silence = accepted
            if r == b"?;":
                time.sleep(0.1)
                continue
            return                          # any other reply: treat as accepted
        raise CatError("SPW 0x%04X <= %02X failed (reply %s)"
                       % (addr, value & 0xFF, last.hex(" ") or "empty"))


# --------------------------------------------------------------------------
# region helpers
# --------------------------------------------------------------------------
def read_region(cat: FtCat) -> tuple[int, int]:
    hi = cat.spr(REGION_ADDR)
    lo = cat.spr(REGION_ADDR + 1)
    flag = cat.spr(REGION_FLAG_ADDR)
    return (hi << 8) | lo, flag


def read_region_settled(cat: FtCat, tries: int = 4) -> tuple[int, int]:
    """read_region() for right after a write: the radio reloads its band
    tables and can answer a stale frame for a moment."""
    last: Exception | None = None
    for attempt in range(tries):
        if attempt:
            time.sleep(0.5)
        try:
            return read_region(cat)
        except CatError as e:
            last = e
    assert last is not None
    raise last


def region_label(code: int) -> str:
    info = REGIONS.get(code)
    if info is None:
        return "unknown code"
    return info["name"]


def write_region(cat: FtCat, code: int) -> None:
    cat.spw(REGION_ADDR, (code >> 8) & 0xFF)
    time.sleep(0.15)
    cat.spw(REGION_ADDR + 1, code & 0xFF)
    # the firmware switches the band table right after the low byte lands
    time.sleep(0.8)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_ports(_args) -> int:
    say(T("ports.title"))
    found = False
    for p in list_ports.comports():
        raw = p.description or ""
        tag = ""
        low = raw.lower()
        if "enhanced" in low and ("cp210" in low or "silicon" in low):
            tag = "   <-- CAT"
            found = True
        say("  %-8s  %-58s %s%s" % (
            p.device, raw[:58],
            ("%04X:%04X" % (p.vid, p.pid)) if p.vid else "      ", tag))
    if not found:
        say(T("ports.not_found"))
    return 0


def cmd_list(_args) -> int:
    say(T("list.title"))
    say("")
    rows = []
    for code in sorted(REGIONS):
        info = REGIONS[code]
        rows.append(["0x%04X" % code, info["name"],
                     T("list.wide_yes") if info["wide"] else "-",
                     describe(code)])
    desc_header = T("list.col.desc")
    if LANG == "both":
        desc_header += " (EN \\ RU)"
    for line in render_table(
            [T("list.col.code"), T("list.col.name"),
             T("list.col.wide"), desc_header],
            rows, maxw=[6, 13, 8, 62]):
        say(line)
    say("")
    say(T("list.footnote"))
    say(T("list.howto"))
    return 0


def cmd_info(args) -> int:
    port = find_port(args.port)
    with FtCat(port, args.baud) as cat:
        ident = cat.identify()
        fw = cat.firmware()
        code, flag = read_region(cat)
        info = REGIONS.get(code)
        say(T("info.port", port=port, baud=args.baud))
        say(T("info.id", ident=ident))
        say(T("info.fw", fw=fw))
        say(T("info.region", code=code,
              name=info["name"] if info else T("region.unknown")))
        say(T("info.bytes", hi=code >> 8, lo=code & 0xFF, flag=flag))
        say_field(T("info.desc_label"), describe(code))
        if info is None:
            say("")
            say(T("info.unknown_warn"))
        elif info["rows"]:
            say("")
            say(T("info.tx_ranges", n=len(info["rows"])))
            for line in boxed([fmt_range(lo, hi) for lo, hi in info["rows"]]):
                say(line)
        else:
            say("")
            say(T("info.no_tx"))
    return 0


def dump_range(cat: FtCat, start: int, length: int, out_path: str,
               double: bool = False) -> tuple[bytes, int]:
    data = bytearray()
    bad = 0
    t0 = time.time()
    for i, addr in enumerate(range(start, start + length)):
        try:
            data.append(cat.spr(addr))
        except CatError:
            data.append(0xFF)
            bad += 1
        if (i & 0x3F) == 0 or i == length - 1:
            count = i + 1
            elapsed = time.time() - t0
            rate = count / elapsed if elapsed else 0
            eta = (length - count) / rate if rate else 0
            inline(T("dump.progress",
                     pct=100.0 * count / length,
                     done=start + count - 1,
                     total=start + length - 1,
                     ms=(elapsed / count * 1000) if count else 0,
                     m=int(eta // 60), s=int(eta % 60)))
    inline("\n")

    if double:
        say(T("dump.second_pass"))
        bad2 = 0
        for i, addr in enumerate(range(start, start + length)):
            try:
                v = cat.spr(addr)
            except CatError:
                bad2 += 1
                continue
            if v != data[i]:
                say(T("dump.mismatch", addr=addr, old=data[i], new=v))
                bad += 1
        say(T("dump.verify_done", n=bad2))

    with open(out_path, "wb") as f:
        f.write(data)

    out_path = os.path.abspath(out_path)
    meta = out_path + ".txt"
    with open(meta, "w", encoding="utf-8") as f:
        f.write("FTX-1 EEPROM dump\n")
        f.write("date    : %s\n" % datetime.now().isoformat(timespec="seconds"))
        f.write("port    : %s\n" % cat.port)
        f.write("ID      : %s\n" % cat.identify())
        f.write("firmware: %s\n" % cat.firmware())
        try:
            code, flag = read_region(cat)
            f.write("region  : 0x%04X (%s), flag 0x%02X\n"
                    % (code, region_label(code), flag))
        except CatError:
            pass
        f.write("start   : 0x%04X\n" % start)
        f.write("length  : 0x%04X (%d bytes)\n" % (length, length))
        f.write("bad     : %d\n" % bad)
        f.write("sha256  : %s\n" % hashlib.sha256(data).hexdigest())
    say(T("dump.saved", path=out_path, n=len(data), bad=bad,
          meta=os.path.basename(meta)))
    return bytes(data), bad


def cmd_dump(args) -> int:
    port = find_port(args.port)
    start = int(args.start, 0)
    if args.full:
        length = MAX_ADDR + 1
    else:
        length = int(args.length, 0)
    if start + length > MAX_ADDR + 1:
        raise SystemExit(T("dump.range_past"))
    est = length * 0.012
    if est < 90:
        dur = T("dump.dur_sec", n=max(1, round(est)))
    else:
        dur = T("dump.dur_min", n=est / 60)
    say(T("dump.header", port=port, start=start, end=start + length - 1,
          n=length, dur=dur))
    out = args.out
    if out is None:
        os.makedirs(DUMP_DIR, exist_ok=True)
        out = os.path.join(DUMP_DIR, "eeprom_%04X_%04X_%s.bin" % (
            start, start + length - 1, datetime.now().strftime("%Y%m%d_%H%M%S")))
    out = os.path.abspath(out)
    with FtCat(port, args.baud) as cat:
        say(T("dump.id", ident=cat.identify()))
        _data, bad = dump_range(cat, start, length, out, double=args.double)
    if bad:
        say(T("dump.warn_bad", n=bad))
        return 1
    return 0


def cmd_set(args) -> int:
    code_text = args.code
    if not code_text:
        cmd_list(args)
        inline(T("set.enter_code"))
        code_text = sys.stdin.readline().strip()
        if not code_text:
            say(T("set.aborted"))
            return 1
    code = parse_code(code_text)
    info = REGIONS.get(code)
    if info is None and not args.force:
        raise SystemExit(T("set.unknown_force", code=code))

    port = find_port(args.port)
    with FtCat(port, args.baud) as cat:
        say(T("set.port", port=port))
        say(T("set.id", ident=cat.identify()))
        cur, flag = read_region(cat)
        say(T("set.current", code=cur, name=region_label(cur)))
        say_field(T("set.desc_label"), describe(cur))

        if cur == code:
            say(T("set.already", code=code,
                  name=info["name"] if info else "?"))
            return 0

        if cur not in REGIONS and not args.force:
            raise SystemExit(T("set.current_unknown", code=cur))

        say("")
        say(T("set.will_be", code=code,
              name=info["name"] if info else T("set.unknown_name")))
        if info:
            say_field(T("set.desc_label"), describe(code))
            if info["rows"]:
                for line in boxed([fmt_range(lo, hi) for lo, hi in info["rows"]]):
                    say(line)

        if args.dry_run:
            say(T("set.dry_run"))
            return 0

        if not args.no_backup:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            name = os.path.join(BACKUP_DIR, "eeprom_head_%s.bin"
                                % datetime.now().strftime("%Y%m%d_%H%M%S"))
            say(T("set.backup", end=HEADER_LEN - 1))
            data, bad = dump_range(cat, 0x0000, HEADER_LEN, name)
            if bad:
                say(T("set.backup_bad", n=bad))
                say(T("set.backup_hint"))
                return 5
            expect = bytes([(cur >> 8) & 0xFF, cur & 0xFF])
            if data[REGION_ADDR:REGION_ADDR + 2] != expect:
                say(T("set.backup_mismatch",
                      got=data[REGION_ADDR:REGION_ADDR + 2].hex(" "),
                      want=expect.hex(" ")))
                return 5
            say(T("set.rollback", code=cur))

        if not args.yes:
            inline(T("set.confirm", code=code,
                     addr_hi=REGION_ADDR, addr_lo=REGION_ADDR + 1))
            answer = sys.stdin.readline().strip().lower()
            if answer != "yes":
                say(T("set.cancelled"))
                return 1

        write_region(cat, code)
        try:
            got, flag2 = read_region_settled(cat)
        except CatError as e:
            say(T("set.read_failed", err=e))
            say(T("set.read_failed_hint"))
            return 0
        if got == code:
            say(T("set.done", code=got, name=region_label(got), flag=flag2))
            say(T("set.done_hint"))
            return 0
        say(T("set.readback_error", got=got, code=code))
        return 3


def cmd_read(args) -> int:
    addr = int(args.addr, 0)
    port = find_port(args.port)
    with FtCat(port, args.baud) as cat:
        v = cat.spr(addr)
        say("0x%04X = 0x%02X (%d)" % (addr, v, v))
    return 0


def cmd_write(args) -> int:
    addr = int(args.addr, 0)
    value = int(args.value, 0)
    if not 0 <= value <= 0xFF:
        raise SystemExit(T("write.bad_value"))
    port = find_port(args.port)
    with FtCat(port, args.baud) as cat:
        old = cat.spr(addr)
        say("0x%04X: 0x%02X -> 0x%02X" % (addr, old, value))
        if not args.yes:
            inline(T("write.confirm"))
            if sys.stdin.readline().strip().lower() != "yes":
                say(T("write.cancelled"))
                return 1
        cat.spw(addr, value)
        got = None
        for _ in range(4):
            time.sleep(0.3)
            try:
                got = cat.spr(addr)
                break
            except CatError:
                continue
        if got is None:
            say(T("write.read_failed", addr=args.addr))
            return 0
        status = T("write.ok") if got == value else T("write.mismatch")
        say(T("write.readback", got=got, status=status))
        return 0 if got == value else 3


# --------------------------------------------------------------------------
# interactive menu (no subcommand on the command line)
# --------------------------------------------------------------------------
def menu_text() -> str:
    return "\n".join([
        "",
        T("menu.title"),
        T("menu.author"),
        "",
        T("menu.item.info"),
        T("menu.item.dump"),
        T("menu.item.list"),
        T("menu.item.set"),
        T("menu.item.ports"),
        T("menu.item.lang"),
        T("menu.item.exit"),
        "",
    ])


def menu_args(base, **over):
    """build a Namespace that any cmd_* function accepts"""
    ns = argparse.Namespace(
        port=base.port, baud=base.baud,
        start="0x0000", length="0x1000", full=False, out=None, double=False,
        code=None, yes=False, force=False, dry_run=False, no_backup=False,
        addr=None, value=None)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def cmd_menu(args) -> int:
    say(menu_text())
    actions = {
        "1": ("info", lambda: cmd_info(menu_args(args))),
        "info": ("info", lambda: cmd_info(menu_args(args))),
        "2": ("dump", lambda: cmd_dump(menu_args(args))),
        "dump": ("dump", lambda: cmd_dump(menu_args(args))),
        "3": ("list", lambda: cmd_list(args)),
        "list": ("list", lambda: cmd_list(args)),
        "4": ("set", lambda: cmd_set(menu_args(args))),
        "set": ("set", lambda: cmd_set(menu_args(args))),
        "5": ("ports", lambda: cmd_ports(args)),
        "ports": ("ports", lambda: cmd_ports(args)),
    }

    while True:
        inline(T("menu.prompt"))
        line = sys.stdin.readline()
        if not line:
            return 0
        choice = line.strip().lower()
        if choice in ("0", "q", "quit", "exit", ""):
            if choice:
                return 0
            continue
        if choice in ("6", "lang", "language", "язык"):
            ask_language(save=True)
            say("")
            say(menu_text())
            continue
        action = actions.get(choice)
        if action is None:
            say(T("menu.bad_choice"))
            continue
        name, func = action
        say("")
        try:
            func()
        except SystemExit as e:          # e.g. no CAT port found
            if e.code:
                say(str(e.code))
        except CatError as e:
            say(T("menu.cat_error", err=e))
        except KeyboardInterrupt:
            say(T("menu.interrupted"))


# --------------------------------------------------------------------------
def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", help=T("help.port"))
    common.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    common.add_argument("--lang", choices=LANG_VALUES,
                        default=argparse.SUPPRESS, help=T("help.lang"))

    ap = argparse.ArgumentParser(
        description=T("main.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=T("main.epilog"))
    ap.add_argument("--port", dest="port_top", metavar="PORT",
                    help=T("help.port"))
    ap.add_argument("--baud", dest="baud_top", type=int, metavar="BAUD",
                    help=T("help.baud", baud=DEFAULT_BAUD))
    ap.add_argument("--lang", choices=LANG_VALUES, default=None,
                    help=T("help.lang"))
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("ports", parents=[common],
                   help=T("help.ports")).set_defaults(func=cmd_ports)
    sub.add_parser("list", parents=[common],
                   help=T("help.list")).set_defaults(func=cmd_list)
    sub.add_parser("info", parents=[common],
                   help=T("help.info")).set_defaults(func=cmd_info)

    p = sub.add_parser("dump", parents=[common], help=T("help.dump"))
    p.add_argument("--start", default="0x0000")
    p.add_argument("--length", default="0x1000")
    p.add_argument("--full", action="store_true", help=T("help.dump_full"))
    p.add_argument("--out")
    p.add_argument("--double", action="store_true", help=T("help.dump_double"))
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("set", parents=[common], help=T("help.set"))
    p.add_argument("code", nargs="?", help=T("help.set_code"))
    p.add_argument("--yes", action="store_true", help=T("help.yes"))
    p.add_argument("--force", action="store_true", help=T("help.force"))
    p.add_argument("--dry-run", action="store_true", help=T("help.dry_run"))
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("read", parents=[common], help=T("help.read"))
    p.add_argument("addr")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("write", parents=[common], help=T("help.write"))
    p.add_argument("addr")
    p.add_argument("value")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_write)

    args = ap.parse_args()
    if args.cmd is None:
        args.port = args.port_top
        args.baud = args.baud_top or DEFAULT_BAUD
        try:
            return cmd_menu(args)
        except KeyboardInterrupt:
            say("")
            return 0
    try:
        return args.func(args)
    except CatError as e:
        say(T("main.cat_error", err=e))
        return 4
    except KeyboardInterrupt:
        say(T("main.interrupted"))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
