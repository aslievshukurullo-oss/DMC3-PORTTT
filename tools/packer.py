#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
packer.py — универсальный упаковщик ассетов для DMC3-PORTTT.

Формат .dmcpack (бинарный):
    [MAGIC 8 байт: b'DMCPACK1']
    [4 байта big-endian: длина манифеста JSON]
    [N байт: манифест JSON в UTF-8]
    [далее: последовательно сжатые данные файлов]

Манифест JSON:
{
  "version": 1,
  "algorithm": "lzma" | "zlib" | "none",
  "files": [
    {"path": "textures/dante.png", "offset": 1234, "size": 5678, "sha256": "..."},
    ...
  ]
}

Только стандартная библиотека Python 3.
"""

import argparse
import re
import hashlib
import json
import lzma
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path

MAGIC = b"DMCPACK1"
FORMAT_VERSION = 1

# Расширения по категориям (для фильтрации и валидации)
CATEGORIES = {
    "textures": {".png", ".jpg", ".jpeg", ".webp", ".ktx", ".pkm"},
    "sounds":   {".ogg", ".mp3", ".wav"},
    "models":   {".obj", ".fbx", ".gltf", ".glb", ".bin"},
    "sprites":  {".png", ".jpg", ".jpeg", ".webp"},
    "data":     {".json", ".txt", ".xml", ".csv"},
}

ALL_KNOWN = set()
for exts in CATEGORIES.values():
    ALL_KNOWN |= exts


# ---------- утилиты ----------

def log(msg: str):
    print(msg)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compress(data: bytes, algorithm: str) -> bytes:
    if algorithm == "lzma":
        return lzma.compress(data, preset=9)
    if algorithm == "zlib":
        return zlib.compress(data, level=9)
    if algorithm == "none":
        return data
    raise ValueError(f"Неизвестный алгоритм: {algorithm}")


def decompress(data: bytes, algorithm: str) -> bytes:
    if algorithm == "lzma":
        return lzma.decompress(data)
    if algorithm == "zlib":
        return zlib.decompress(data)
    if algorithm == "none":
        return data
    raise ValueError(f"Неизвестный алгоритм: {algorithm}")


def detect_category(path: Path) -> str:
    ext = path.suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


# ---------- pack ----------

def cmd_pack(args):
    src = Path(args.input).resolve()
    out = Path(args.output).resolve()

    if not src.is_dir():
        log(f"[!] Входная папка не найдена: {src}")
        return 1

    if out.exists() and not args.force:
        log(f"[!] Файл уже существует: {out}. Используйте --force.")
        return 1

    algorithm = args.algorithm
    log(f"[i] Источник: {src}")
    log(f"[i] Пакет:    {out}")
    log(f"[i] Алгоритм: {algorithm}")

    # Сбор файлов
    files = []
    skipped = []
    total_raw = 0
    total_compressed = 0

    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel_path = p.relative_to(src).as_posix()
        ext = p.suffix.lower()

        if args.only and ext not in args.only:
            skipped.append((rel_path, "не входит в --only"))
            continue
        if ext not in ALL_KNOWN and not args.all:
            skipped.append((rel_path, f"неизвестное расширение {ext}"))
            continue

        try:
            raw = p.read_bytes()
        except Exception as e:
            skipped.append((rel_path, f"ошибка чтения: {e}"))
            continue

        comp = compress(raw, algorithm)
        total_raw += len(raw)
        total_compressed += len(comp)

        files.append({
            "path": rel_path,
            "category": detect_category(p),
            "raw_size": len(raw),
            "comp_size": len(comp),
            "sha256": sha256_bytes(raw),
            "_data": comp,  # временно, не в манифест
        })
        log(f"  + {rel_path:<50} {human_size(len(raw)):>10} -> {human_size(len(comp)):>10}")

    if not files:
        log("[!] Нет файлов для упаковки.")
        return 1

    # Собираем манифест (без _data)
    manifest_files = []
    offset = 0
    for f in files:
        entry = {
            "path": f["path"],
            "category": f["category"],
            "raw_size": f["raw_size"],
            "comp_size": f["comp_size"],
            "offset": offset,
            "sha256": f["sha256"],
        }
        manifest_files.append(entry)
        offset += f["comp_size"]

    manifest = {
        "version": FORMAT_VERSION,
        "algorithm": algorithm,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": manifest_files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    # Запись
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(manifest_bytes)))
        f.write(manifest_bytes)
        for f_data in files:
            f.write(f_data["_data"])

    final_size = out.stat().st_size
    log("")
    log(f"[ok] Упаковано файлов: {len(files)}")
    if skipped:
        log(f"[i] Пропущено: {len(skipped)}")
        for path, reason in skipped:
            log(f"    - {path}: {reason}")
    log(f"[i] Сырой размер:      {human_size(total_raw)}")
    log(f"[i] Сжатый размер:     {human_size(total_compressed)}")
    log(f"[i] Итоговый пакет:    {human_size(final_size)}")
    if total_raw > 0:
        ratio = (1 - final_size / total_raw) * 100
        log(f"[i] Экономия:          {ratio:.1f}%")
    return 0


# ---------- unpack ----------

def read_pack(path: Path):
    """Читает .dmcpack и возвращает (manifest, список (entry, data))."""
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != MAGIC:
            raise ValueError(f"Неверная сигнатура: {magic!r}, ожидалось {MAGIC!r}")
        len_bytes = f.read(4)
        if len(len_bytes) != 4:
            raise ValueError("Обрезанный заголовок")
        manifest_len = struct.unpack(">I", len_bytes)[0]
        manifest_bytes = f.read(manifest_len)
        if len(manifest_bytes) != manifest_len:
            raise ValueError("Обрезанный манифест")
        manifest = json.loads(manifest_bytes.decode("utf-8"))

        # читаем данные последовательно по offset/size
        algorithm = manifest["algorithm"]
        base = f.tell()
        entries = []
        for entry in manifest["files"]:
            f.seek(base + entry["offset"])
            comp = f.read(entry["comp_size"])
            if len(comp) != entry["comp_size"]:
                raise ValueError(f"Обрезанные данные: {entry['path']}")
            raw = decompress(comp, algorithm)
            if sha256_bytes(raw) != entry["sha256"]:
                raise ValueError(f"Хеш не совпадает: {entry['path']}")
            entries.append((entry, raw))
        return manifest, entries


def safe_target(out_dir: Path, rel: str):
    """Возвращает путь внутри out_dir или None, если путь небезопасен."""
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = Path(rel).parts
    if any(p in ("..", "") for p in parts):
        return None
    dst = (out_dir / rel).resolve()
    try:
        dst.relative_to(out_dir.resolve())
    except ValueError:
        return None
    return dst


def cmd_unpack(args):
    pack_path = Path(args.input).resolve()
    out_dir = Path(args.output).resolve()

    if not pack_path.is_file():
        log(f"[!] Пакет не найден: {pack_path}")
        return 1
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        log(f"[!] Папка не пуста: {out_dir}. Используйте --force.")
        return 1

    log(f"[i] Пакет: {pack_path}")
    log(f"[i] Куда:  {out_dir}")

    try:
        manifest, entries = read_pack(pack_path)
    except Exception as e:
        log(f"[!] Ошибка чтения пакета: {e}")
        return 1

    # Сначала проверяем все пути, и только потом пишем что-либо
    targets = []
    for entry, raw in entries:
        dst = safe_target(out_dir, entry["path"])
        if dst is None:
            log(f"[!] Небезопасный путь в пакете, распаковка отменена: {entry['path']!r}")
            return 1
        targets.append((dst, entry, raw))

    out_dir.mkdir(parents=True, exist_ok=True)
    for dst, entry, raw in targets:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        log(f"  - {entry['path']:<50} {human_size(len(raw)):>10}")

    log("")
    log(f"[ok] Распаковано файлов: {len(entries)}")
    log(f"[i] Алгоритм: {manifest.get('algorithm')}")
    log(f"[i] Создан:   {manifest.get('created_at')}")
    return 0


# ---------- info ----------

def cmd_info(args):
    pack_path = Path(args.input).resolve()
    if not pack_path.is_file():
        log(f"[!] Пакет не найден: {pack_path}")
        return 1

    try:
        manifest, entries = read_pack(pack_path)
    except Exception as e:
        log(f"[!] Ошибка: {e}")
        return 1

    log(f"[i] Пакет: {pack_path}")
    log(f"[i] Размер файла: {human_size(pack_path.stat().st_size)}")
    log(f"[i] Версия формата: {manifest.get('version')}")
    log(f"[i] Алгоритм: {manifest.get('algorithm')}")
    log(f"[i] Создан: {manifest.get('created_at')}")
    log(f"[i] Файлов: {len(entries)}")
    log("")
    for entry in manifest["files"]:
        log(f"  {entry['category']:<9} {entry['path']:<50} "
            f"{human_size(entry['raw_size']):>10} -> {human_size(entry['comp_size']):>10}")
    return 0


# ---------- main ----------

def parse_only(value):
    if not value:
        return None
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    return set("." + p.lstrip(".") for p in parts)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="packer.py",
        description="Универсальный упаковщик ассетов DMC3-PORTTT (.dmcpack)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pack", help="упаковать папку в .dmcpack")
    pp.add_argument("--input", required=True, help="папка с ассетами")
    pp.add_argument("--output", required=True, help="путь к .dmcpack")
    pp.add_argument("--algorithm", choices=["lzma", "zlib", "none"], default="lzma",
                    help="алгоритм сжатия (по умолчанию lzma — максимальное сжатие)")
    pp.add_argument("--only", type=parse_only, default=None,
                    help="только указанные расширения, через запятую: png,ogg")
    pp.add_argument("--all", action="store_true",
                    help="включать файлы с неизвестными расширениями")
    pp.add_argument("--force", action="store_true", help="перезаписать выходной файл")
    pp.set_defaults(func=cmd_pack)

    up = sub.add_parser("unpack", help="распаковать .dmcpack")
    up.add_argument("--input", required=True, help="путь к .dmcpack")
    up.add_argument("--output", required=True, help="папка для распаковки")
    up.add_argument("--force", action="store_true", help="писать в непустую папку")
    up.set_defaults(func=cmd_unpack)

    ip = sub.add_parser("info", help="показать содержимое .dmcpack")
    ip.add_argument("--input", required=True, help="путь к .dmcpack")
    ip.set_defaults(func=cmd_info)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
