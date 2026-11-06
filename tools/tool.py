#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool.py — CLI-инструмент для работы с ассетами и кодом проекта DMC3-PORTTT.

Только стандартная библиотека Python 3.
Бэкапы: .tool_backup/<timestamp>/<относительный_путь> (не коммитятся).

Команды:
    list       — список ассетов
    add        — добавить/заменить ассет
    replace    — заменить ассет
    check      — проверка ассетов
    patch-code — правка кода по шаблону (new-class, add-field)
    build      — сборка APK
"""

import argparse
import difflib
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
ASSETS_DIR = ROOT / "app" / "src" / "main" / "assets"
BACKUP_ROOT = ROOT / ".tool_backup"

ASSET_TYPES = {
    "sprites": {".png", ".jpg", ".jpeg", ".webp"},
    "textures": {".png", ".jpg", ".jpeg", ".webp", ".ktx", ".pkm"},
    "sounds": {".ogg", ".mp3", ".wav"},
    "models": {".obj", ".fbx", ".gltf", ".glb", ".bin"},
}

MAX_TEXTURE_SIDE = 2048
MAX_FILE_MB = 5.0


# ---------- утилиты ----------

def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def rel(path) -> str:
    if path is None:
        return "<none>"
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.<>?, ]*$")


def inside(path: Path, base: Path) -> bool:
    """True, если path (после разрешения ссылок и ..) лежит внутри base."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def backup(path: Path):
    """Копирует файл в .tool_backup/<timestamp>/<отн.путь>. Возвращает путь бэкапа или None."""
    if not path.exists():
        return None
    dst = BACKUP_ROOT / ts() / path.resolve().relative_to(ROOT.resolve())
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    return dst


def ensure_assets_dir() -> bool:
    if not ASSETS_DIR.exists():
        print(f"[!] Папка ассетов не найдена: {rel(ASSETS_DIR)}")
        return False
    return True


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def detect_type(path: Path):
    ext = path.suffix.lower()
    for t, exts in ASSET_TYPES.items():
        if ext in exts:
            return t
    return None


# ---------- list ----------

def cmd_list(args):
    if not ensure_assets_dir():
        return 1
    items = []
    for p in sorted(ASSETS_DIR.rglob("*")):
        if p.is_file():
            t = detect_type(p) or "other"
            if args.type and t != args.type:
                continue
            items.append((t, rel(p), human_size(p.stat().st_size)))
    if not items:
        print("[i] Ассетов не найдено.")
        return 0
    width = max(len(i[1]) for i in items)
    for t, name, size in items:
        print(f"{t:<9} {name:<{width}}  {size}")
    print(f"\nВсего: {len(items)}")
    return 0


# ---------- add ----------

def cmd_add(args):
    src = Path(args.file).resolve()
    if not src.is_file():
        print(f"[!] Файл не найден: {src}")
        return 1

    t = args.type
    if t not in ASSET_TYPES:
        print(f"[!] Неизвестный тип: {t}. Допустимо: {', '.join(ASSET_TYPES)}")
        return 1

    ext = src.suffix.lower()
    if ext not in ASSET_TYPES[t]:
        print(f"[!] Расширение {ext} не подходит для типа {t}. Допустимо: {sorted(ASSET_TYPES[t])}")
        return 1

    size_mb = src.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        print(f"[!] Файл слишком большой: {size_mb:.2f} MB > {MAX_FILE_MB} MB")
        return 1

    if not ASSETS_DIR.exists():
        print(f"[!] Внимание: папка {rel(ASSETS_DIR)} отсутствует, будет создана.")

    name = args.name if args.name else src.name
    if not FILENAME_RE.match(name) or ".." in name:
        print(f"[!] Недопустимое имя файла: {name!r}")
        return 1
    dst = ASSETS_DIR / t / name
    if not inside(dst, ASSETS_DIR):
        print(f"[!] Назначение вне папки ассетов: {dst}")
        return 1

    print(f"[i] Тип: {t}")
    print(f"[i] Источник: {src}")
    print(f"[i] Назначение: {rel(dst)}")

    if dst.exists() and not args.force:
        print("[!] Файл уже существует. Используйте --force для перезаписи.")
        return 1

    if dst.exists():
        b = backup(dst)
        if b:
            print(f"[i] Бэкап: {rel(b)}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[ok] Скопировано: {rel(dst)}")
    return 0


# ---------- replace ----------

def cmd_replace(args):
    old = Path(args.old).resolve()
    new = Path(args.new).resolve()

    if not old.is_file():
        print(f"[!] Старый файл не найден: {old}")
        return 1
    if not inside(old, ASSETS_DIR):
        print(f"[!] replace работает только с ассетами в {rel(ASSETS_DIR)}: {old}")
        return 1
    if not new.is_file():
        print(f"[!] Новый файл не найден: {new}")
        return 1

    print(f"[i] Замена: {rel(old)} <- {new}")
    b = backup(old)
    if b:
        print(f"[i] Бэкап: {rel(b)}")
    shutil.copy2(new, old)
    print(f"[ok] Заменено: {rel(old)}")
    return 0


# ---------- check ----------

def _png_size(path: Path):
    """Возвращает (w, h) для PNG через заголовок IHDR, без Pillow. Или None."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if len(head) < 24:
            return None
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if head[12:16] != b"IHDR":
            return None
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return w, h
    except Exception:
        return None


def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def cmd_check(args):
    if not ensure_assets_dir():
        return 0

    warnings = []
    total = 0
    for p in sorted(ASSETS_DIR.rglob("*")):
        if not p.is_file():
            continue
        total += 1
        t = detect_type(p)
        size_mb = p.stat().st_size / (1024 * 1024)

        if t is None:
            warnings.append(f"неизвестный тип файла: {rel(p)}")

        if size_mb > MAX_FILE_MB:
            warnings.append(f"большой файл ({size_mb:.2f} MB): {rel(p)}")

        if t == "textures" and p.suffix.lower() == ".png":
            wh = _png_size(p)
            if wh:
                w, h = wh
                if w > MAX_TEXTURE_SIDE or h > MAX_TEXTURE_SIDE:
                    warnings.append(f"текстура {w}x{h} > {MAX_TEXTURE_SIDE}px: {rel(p)}")
                if not (_is_pow2(w) and _is_pow2(h)):
                    warnings.append(f"не степень двойки {w}x{h} (предупреждение): {rel(p)}")
            else:
                warnings.append(f"не удалось прочитать размеры PNG: {rel(p)}")

        if t == "sounds" and p.suffix.lower() not in (".ogg", ".mp3"):
            warnings.append(f"звук в неподдерживаемом формате {p.suffix}: {rel(p)}")

    print(f"[i] Проверено файлов: {total}")
    print("[i] Проверка сторон — только для PNG; для jpg/webp проверяется только вес.")
    if not warnings:
        print("[ok] Предупреждений нет.")
    else:
        print(f"[!] Предупреждений: {len(warnings)}")
        for w in warnings:
            print(f"  - {w}")
    return 0


# ---------- patch-code ----------

NEW_CLASS_TEMPLATE = """package {package}

/**
 * {name}
 * Сгенерировано tools/tool.py
 */
class {name} {{
    // TODO: реализация
}}
"""


def _find_class_closing_brace(text: str, class_name: str):
    """
    Находит позицию закрывающей '}' класса class_name.
    Учитывает строки ("...", '...'), raw-строки (\"\"\"...\"\"\"), блочные /* */,
    строчные // комментарии и вложенные { }.
    Возвращает индекс '}' или -1.
    """
    m = re.search(rf"\bclass\s+{re.escape(class_name)}\b", text)
    if not m:
        return -1

    i = m.end()
    n = len(text)
    while i < n and text[i] != "{":
        i += 1
    if i >= n:
        return -1

    depth = 0
    in_str = None
    in_raw = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        nxt3 = text[i:i + 3]

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_raw:
            if nxt3 == '"""':
                in_raw = False
                i += 3
                continue
            i += 1
            continue

        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue

        if nxt3 == '"""':
            in_raw = True
            i += 3
            continue
        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue

        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1


def package_from_path(path: Path):
    """com/game/engine/X.kt под src/main/java или src/main/kotlin -> com.game.engine"""
    parts = path.resolve().parts
    for marker in (("src", "main", "java"), ("src", "main", "kotlin")):
        for i in range(len(parts) - len(marker)):
            if parts[i:i + len(marker)] == marker:
                pkg_parts = parts[i + len(marker):-1]
                return ".".join(pkg_parts) if pkg_parts else None
    return None


def cmd_patch_code(args):
    template = args.template
    target = Path(args.target).resolve()

    if template not in ("new-class", "add-field"):
        print(f"[!] Неизвестный шаблон: {template}. Допустимо: new-class, add-field")
        return 1
    if not inside(target, APP_DIR):
        print(f"[!] patch-code работает только с файлами внутри {rel(APP_DIR)}: {target}")
        return 1

    if template == "new-class":
        if target.suffix != ".kt":
            print("[!] new-class ожидает путь к .kt файлу")
            return 1
        if target.exists():
            print(f"[!] Файл уже существует: {rel(target)}")
            return 1
        name = args.name or target.stem
        if not IDENT_RE.match(name):
            print(f"[!] Недопустимое имя класса: {name!r}")
            return 1
        package = args.package or package_from_path(target)
        if not package:
            print("[!] Не удалось определить пакет из пути. Укажите --package явно.")
            return 1
        if not all(IDENT_RE.match(part) for part in package.split(".")):
            print(f"[!] Недопустимый пакет: {package!r}")
            return 1
        content = NEW_CLASS_TEMPLATE.format(package=package, name=name)

        print(f"[i] Шаблон: new-class")
        print(f"[i] Файл: {rel(target)}")
        print("--- diff ---")
        for line in content.splitlines():
            print("+ " + line)
        print("------------")

        if args.dry_run:
            print("[dry-run] Запись не выполнена.")
            return 0

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"[ok] Создан: {rel(target)}")
        return 0

    if not target.is_file():
        print(f"[!] Файл не найден: {rel(target)}")
        return 1
    if not args.name or not args.field_type:
        print("[!] Для add-field нужны --name и --field-type")
        return 1
    if not IDENT_RE.match(args.name):
        print(f"[!] Недопустимое имя поля: {args.name!r}")
        return 1
    if not TYPE_RE.match(args.field_type):
        print(f"[!] Недопустимый тип поля: {args.field_type!r}")
        return 1

    text = target.read_text(encoding="utf-8")
    class_name = args.class_name
    if not class_name:
        print("[!] Для add-field нужен --class-name (имя класса в файле)")
        return 1

    if not IDENT_RE.match(class_name):
        print(f"[!] Недопустимое имя класса: {class_name!r}")
        return 1
    if re.search(rf"\b(val|var)\s+{re.escape(args.name)}\b", text):
        print(f"[!] Поле {args.name} уже объявлено в {rel(target)}")
        return 1

    pos = _find_class_closing_brace(text, class_name)
    if pos == -1:
        print(f"[!] Не найден класс {class_name} в {rel(target)}")
        return 1

    default = args.default if args.default is not None else "TODO()"
    field_line = f"    val {args.name}: {args.field_type} = {default}\n"
    new_text = text[:pos] + field_line + text[pos:]

    print(f"[i] Шаблон: add-field")
    print(f"[i] Файл: {rel(target)}")
    print(f"[i] Класс: {class_name}")
    print(f"[i] Поле: {args.name}: {args.field_type} = {default}")
    print("--- diff ---")
    diff = difflib.unified_diff(
        text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=rel(target),
        tofile=rel(target) + " (patched)",
    )
    for line in diff:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
    print("------------")

    if args.dry_run:
        print("[dry-run] Запись не выполнена.")
        return 0

    b = backup(target)
    if b:
        print(f"[i] Бэкап: {rel(b)}")
    target.write_text(new_text, encoding="utf-8")
    print(f"[ok] Изменён: {rel(target)}")
    return 0


# ---------- build ----------

def cmd_build(args):
    gradle_in_path = shutil.which("gradle") is not None
    gradlew = ROOT / "gradlew"

    if gradle_in_path:
        print("[i] Найден gradle в PATH. Запуск: gradle assembleDebug")
        return subprocess.call(["gradle", "assembleDebug"], cwd=str(ROOT))

    if gradlew.is_file():
        print("[i] Найден ./gradlew. Запуск: ./gradlew assembleDebug")
        return subprocess.call([str(gradlew), "assembleDebug"], cwd=str(ROOT))

    print("[i] gradle не найден ни в PATH, ни как ./gradlew.")
    print("[i] Локально можно запустить:")
    print("    ./gradlew assembleDebug")
    print("[i] Или через GitHub Actions:")
    print("    https://github.com/aslievshukurullo-oss/DMC3-PORTTT/actions")
    return 0


# ---------- main ----------

def main(argv=None):
    p = argparse.ArgumentParser(prog="tool.py", description="DMC3-PORTTT asset/code tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="список ассетов")
    lp.add_argument("--type", choices=list(ASSET_TYPES), default=None)
    lp.set_defaults(func=cmd_list)

    ap = sub.add_parser("add", help="добавить/заменить ассет")
    ap.add_argument("file")
    ap.add_argument("--type", required=True, choices=list(ASSET_TYPES))
    ap.add_argument("--name", default=None)
    ap.add_argument("--force", action="store_true")
    ap.set_defaults(func=cmd_add)

    rp = sub.add_parser("replace", help="заменить ассет")
    rp.add_argument("old")
    rp.add_argument("new")
    rp.set_defaults(func=cmd_replace)

    cp = sub.add_parser("check", help="проверка ассетов")
    cp.set_defaults(func=cmd_check)

    pp = sub.add_parser("patch-code", help="правка кода по шаблону")
    pp.add_argument("--template", required=True, choices=["new-class", "add-field"])
    pp.add_argument("--target", required=True)
    pp.add_argument("--name", default=None)
    pp.add_argument("--package", default=None)
    pp.add_argument("--class-name", default=None)
    pp.add_argument("--field-type", default=None)
    pp.add_argument("--default", default=None)
    pp.add_argument("--dry-run", action="store_true")
    pp.set_defaults(func=cmd_patch_code)

    bp = sub.add_parser("build", help="сборка APK")
    bp.set_defaults(func=cmd_build)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
