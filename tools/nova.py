"""
NovaCompress — мета-архиватор с выбором алгоритма, контейнером,
hardlinks, симлинками, шифрованием, тестером, ремонтом и bench.

Формат контейнера:
    MAGIC(4)=b'NOVA' + TYPE(1)='F'|'D' + MLEN(8,>Q) + MANIFEST(JSON) + DATA

Файловый манифест:
    {"kind":"file","name","size","sha256","algo","data_start"}
Каталоговый манифест:
    {"kind":"dir","root","data_start",
     "dirs":[{"path","mode"}],
     "links":[{"path","target"}],
     "entries":[{"path","size","sha256","mode","algo","offset","clen",
                 "hardlink_to"?}]}

algo ∈ {"lzma","bz2","store","hardlink"}; для hardlink clen=0, sha256=None.

Ограничения (осознанные):
- Python 3.10+
- старый формат без TYPE не читается
- encrypt/decrypt читают файл в RAM (не для гигабайтов)
- _repair_dir читает блок в RAM (не для гигабайтных записей)
- ECC нет; порча без .bak не восстанавливается
"""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import lzma
import math
import os
import shutil
import struct
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    _CRYPTO_OK = True
except BaseException:  # ImportError или сбой нативной библиотеки (pyo3 PanicException не наследует Exception)
    _CRYPTO_OK = False


class NovaCompress:
    MAGIC = b'NOVA'
    ENC_MAGIC = b'NOVE'
    ENC_VERSION = b'\x01'
    SCRYPT_N = 2 ** 15
    SCRYPT_R = 8
    SCRYPT_P = 1
    HEADER_OVERHEAD = 4 + 1 + 8 + 256  # консервативная оценка для STORE-проверки

    _SIGNATURES = (
        (b'\xff\xd8\xff', 'jpeg'),
        (b'\x89PNG\r\n\x1a\n', 'png'),
        (b'GIF8', 'gif'),
        (b'PK\x03\x04', 'zip'),
        (b'PK\x05\x06', 'zip-empty'),
        (b'\x1f\x8b', 'gzip'),
        (b'BZh', 'bzip2'),
        (b'\xfd7zXZ\x00', 'xz'),
        (b'7z\xbc\xaf\x27\x1c', '7z'),
        (b'Rar!\x1a\x07', 'rar'),
        (b'\x28\xb5\x2f\xfd', 'zstd'),
        (b'\x1a\x45\xdf\xa3', 'mkv/webm'),
        (b'%PDF-', 'pdf'),
        (b'\x7fELF', 'elf'),
        (b'MZ', 'pe/exe'),
    )

    # ------------------------------------------------------------------ utils

    def _calculate_sha256(self, file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    def _estimate_entropy(self, file_path: Path) -> float:
        total = 0
        counts = [0] * 256
        with open(file_path, 'rb') as f:
            while chunk := f.read(1 << 20):
                total += len(chunk)
                for byte, cnt in Counter(chunk).items():
                    counts[byte] += cnt
        if total == 0:
            return 0.0
        ent = 0.0
        for cnt in counts:
            if cnt:
                p = cnt / total
                ent -= p * math.log2(p)
        return ent

    def _looks_compressed(self, file_path: Path) -> str | None:
        with open(file_path, 'rb') as f:
            head = f.read(16)
        for sig, name in self._SIGNATURES:
            if head.startswith(sig):
                return name
        return None

    def _choose_algo_for(self, src: Path, entropy: float) -> str:
        if self._looks_compressed(src):
            return 'store'
        if entropy > 7.95 and src.stat().st_size > 4096:
            return 'store'
        if entropy < 4.0:
            return 'bz2'
        return 'lzma'

    @staticmethod
    def _safe_rel(rel: str) -> Path | None:
        parts = [p for p in Path(rel).parts if p not in ('..', '.', '')]
        if not parts:
            return None
        return Path(*parts)

    # -------------------------------------------------------------- compressors

    def _compress_one_to(self, src: Path, dst_path: Path, algo: str) -> int:
        if algo == 'lzma':
            with lzma.open(dst_path, 'wb', preset=6) as f_comp, open(src, 'rb') as f_in:
                shutil.copyfileobj(f_in, f_comp)
        elif algo == 'bz2':
            with bz2.open(dst_path, 'wb', compresslevel=9) as f_comp, open(src, 'rb') as f_in:
                shutil.copyfileobj(f_in, f_comp)
        elif algo == 'store':
            with open(dst_path, 'wb') as f_out, open(src, 'rb') as f_in:
                shutil.copyfileobj(f_in, f_out)
        else:
            raise ValueError(f"Неизвестный алгоритм сжатия: {algo}")
        return dst_path.stat().st_size

    def _decompress_one_stream(self, f_in, f_out, algo: str, clen: int,
                                chunk: int = 1 << 20) -> None:
        remaining = clen
        if algo == 'store':
            while remaining > 0:
                buf = f_in.read(min(chunk, remaining))
                if not buf:
                    raise ValueError("Неожиданный конец данных (store)")
                f_out.write(buf)
                remaining -= len(buf)
            return
        if algo == 'lzma':
            dec = lzma.LZMADecompressor()
        elif algo == 'bz2':
            dec = bz2.BZ2Decompressor()
        else:
            raise ValueError(f"Неизвестный алгоритм распаковки: {algo}")
        while remaining > 0:
            buf = f_in.read(min(chunk, remaining))
            if not buf:
                raise ValueError("Неожиданный конец данных")
            remaining -= len(buf)
            out = dec.decompress(buf)
            if out:
                f_out.write(out)
        if not dec.eof:
            out = dec.decompress(b'')
            if out:
                f_out.write(out)
        if not dec.eof:
            raise ValueError("Поток не завершён (eof=False): данные обрезаны")

    # -------------------------------------------------------------- round-trip

    def _roundtrip_ok(self, src: Path, compressed_path: Path, algo: str,
                       expected_sha: str) -> bool:
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tmp_out = Path(tf.name)
            try:
                with open(compressed_path, 'rb') as f_in, open(tmp_out, 'wb') as f_out:
                    if algo == 'lzma':
                        with lzma.open(f_in, 'rb') as f_dec:
                            shutil.copyfileobj(f_dec, f_out)
                    elif algo == 'bz2':
                        with bz2.open(f_in, 'rb') as f_dec:
                            shutil.copyfileobj(f_dec, f_out)
                    else:
                        shutil.copyfileobj(f_in, f_out)
                return self._calculate_sha256(tmp_out) == expected_sha
            finally:
                if tmp_out.exists():
                    tmp_out.unlink()
        except Exception:
            return False

    def _try_compress_variants(self, src: Path, candidates: list[str],
                                tmp_dir: Path) -> tuple[str, Path] | None:
        expected_sha = self._calculate_sha256(src)
        orig_size = src.stat().st_size
        for idx, algo in enumerate(candidates):
            fd, tmp_name = tempfile.mkstemp(prefix="try_", suffix=f"_{algo}.bin", dir=tmp_dir)
            os.close(fd)
            buf = Path(tmp_name)
            try:
                self._compress_one_to(src, buf, algo)
                comp_size = buf.stat().st_size
                if comp_size + self.HEADER_OVERHEAD >= orig_size:
                    buf.unlink(missing_ok=True)
                    continue
                if not self._roundtrip_ok(src, buf, algo, expected_sha):
                    buf.unlink(missing_ok=True)
                    continue
                return algo, buf
            except Exception:
                buf.unlink(missing_ok=True)
                continue
        return None

    # ------------------------------------------------------------------ header

    def _write_header(self, f_out, kind: str, manifest_in: dict) -> int:
        manifest = dict(manifest_in)
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        prev = None
        while prev != len(manifest_bytes):
            prev = len(manifest_bytes)
            manifest["data_start"] = 4 + 1 + 8 + prev
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        f_out.write(self.MAGIC)
        f_out.write(kind.encode('ascii'))
        f_out.write(struct.pack('>Q', len(manifest_bytes)))
        f_out.write(manifest_bytes)
        return 4 + 1 + 8 + len(manifest_bytes)

    def _read_header(self, f_in) -> tuple[str, dict, int]:
        magic = f_in.read(4)
        if magic != self.MAGIC:
            raise ValueError("Нет сигнатуры NOVA")
        kind_byte = f_in.read(1)
        if kind_byte not in (b'F', b'D'):
            raise ValueError(f"Неизвестный TYPE: {kind_byte!r}")
        kind = 'file' if kind_byte == b'F' else 'dir'
        mlen = struct.unpack('>Q', f_in.read(8))[0]
        manifest = json.loads(f_in.read(mlen).decode('utf-8'))
        return kind, manifest, f_in.tell()

    # --------------------------------------------------------------- file mode

    def compress(self, source_path: str, keep_backup: bool = False) -> str:
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"Файл не найден: {source_path}")

        print(f"🔍 Анализ: {src.name}...")
        orig_size = src.stat().st_size
        orig_sha = self._calculate_sha256(src)
        sig = self._looks_compressed(src)
        entropy = self._estimate_entropy(src)

        if sig:
            primary = 'store'
            print(f"📦 Сигнатура: {sig} — файл уже сжат, STORE.")
        elif entropy > 7.95 and orig_size > 4096:
            primary = 'store'
        elif entropy < 4.0:
            primary = 'bz2'
        else:
            primary = 'lzma'
        print(f"📊 Энтропия: {entropy:.2f}/8.00 | Приоритет: {primary.upper()}")

        output_path = src.parent / (src.name + '.nova')

        if keep_backup and orig_size < 256 * 1024 * 1024:
            bak = output_path.parent / (output_path.name + '.bak')
            shutil.copy2(src, bak)
            print(f"💾 Резервная копия: {bak}")

        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_try_", dir=src.parent))
        try:
            if primary == 'store':
                chosen = 'store'
                payload = tmp_dir / "store.bin"
                with open(src, 'rb') as f_in, open(payload, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            else:
                order = {'bz2': ['bz2', 'lzma'],
                         'lzma': ['lzma', 'bz2']}[primary]
                result = self._try_compress_variants(src, order, tmp_dir)
                if result is None:
                    chosen = 'store'
                    payload = tmp_dir / "store.bin"
                    with open(src, 'rb') as f_in, open(payload, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                else:
                    chosen, payload = result

            manifest = {"kind": "file", "name": src.name,
                        "size": orig_size, "sha256": orig_sha, "algo": chosen}
            with open(output_path, 'wb') as f_final, open(payload, 'rb') as f_data:
                self._write_header(f_final, 'F', manifest)
                shutil.copyfileobj(f_data, f_final)

            saved = orig_size - output_path.stat().st_size
            if saved > 0:
                print(f"🎉 Экономия: {saved} байт ({(saved/orig_size)*100:.2f}%)")
            else:
                print(f"ℹ️ Контейнер без раздувания (режим: {chosen.upper()})")
            return str(output_path)
        except Exception as e:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"Сбой сжатия: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def decompress(self, archive_path: str) -> str:
        arch = Path(archive_path)
        if not arch.is_file():
            raise FileNotFoundError(f"Архив не найден: {archive_path}")

        output_path = None
        try:
            with open(arch, 'rb') as f_in:
                kind, manifest, data_start = self._read_header(f_in)
                if kind != 'file' or manifest.get("kind") != "file":
                    raise ValueError("Это не файловый контейнер")
                safe = Path(manifest["name"]).name or "restored_file"
                output_path = arch.parent / f"restored_{safe}"
                print(f"🔓 Извлечение: {safe} [{manifest['algo'].upper()}]")
                clen = arch.stat().st_size - data_start
                with open(output_path, 'wb') as f_out:
                    self._decompress_one_stream(
                        f_in, f_out, manifest["algo"], clen)

            if self._calculate_sha256(output_path) != manifest["sha256"]:
                output_path.unlink()
                raise ValueError("🚨 SHA-256 не совпадает — данные повреждены.")
            print("✅ Целостность подтверждена.")
            return str(output_path)
        except Exception as e:
            if output_path and output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"Сбой распаковки: {e}")

    # ---------------------------------------------------------------- dir mode

    def _pack_one(self, src: Path, rel: str, st, tmp_dir: Path, idx: int,
                  hardlink_to: str | None = None) -> dict:
        if hardlink_to is not None:
            return {"path": rel, "size": st.st_size, "sha256": None,
                    "mode": st.st_mode & 0o777, "algo": "hardlink",
                    "clen": 0, "_buf": None, "_idx": idx,
                    "hardlink_to": hardlink_to}
        sha = self._calculate_sha256(src)
        entropy = self._estimate_entropy(src)
        primary = self._choose_algo_for(src, entropy)
        if primary == 'store':
            buf = tmp_dir / f"payload_{idx:08d}.bin"
            with open(src, 'rb') as f_in, open(buf, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            algo = 'store'
        else:
            order = {'bz2': ['bz2', 'lzma'],
                     'lzma': ['lzma', 'bz2']}[primary]
            result = self._try_compress_variants(src, order, tmp_dir)
            if result is None:
                buf = tmp_dir / f"payload_{idx:08d}.bin"
                with open(src, 'rb') as f_in, open(buf, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
                algo = 'store'
            else:
                algo, buf = result
                canonical = tmp_dir / f"payload_{idx:08d}.bin"
                buf.replace(canonical)
                buf = canonical
        return {"path": rel, "size": st.st_size, "sha256": sha,
                "mode": st.st_mode & 0o777, "algo": algo,
                "clen": buf.stat().st_size, "_buf": buf, "_idx": idx}

    def compress_dir(self, source_dir: str, workers: int | None = None,
                     keep_backup: bool = False) -> str:
        root = Path(source_dir).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Не каталог: {source_dir}")

        output_path = root.parent / (root.name + '.novadir')
        backup_root = None
        if keep_backup:
            total = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            if total > 512 * 1024 * 1024:
                print(f"⚠️ Бэкап {total/1e9:.2f} ГБ — займёт место. Ctrl+C чтобы отменить.")
            backup_root = output_path.parent / (output_path.name + '.bak')
            if backup_root.exists():
                shutil.rmtree(backup_root)
            backup_root.mkdir(parents=True)
            print(f"💾 Резервная копия дерева: {backup_root}")

        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_tmp_", dir=root.parent))
        data_path = tmp_dir / "data.bin"

        try:
            dirs_meta, links_meta, files = [], [], []
            seen_inodes = {}
            for path in sorted(root.rglob('*')):
                rel = path.relative_to(root).as_posix()
                if path.is_symlink():
                    links_meta.append({"path": rel,
                                       "target": os.readlink(path)})
                    continue
                if path.is_dir():
                    st = path.stat()
                    dirs_meta.append({"path": rel, "mode": st.st_mode & 0o777})
                    continue
                if not path.is_file():
                    continue
                st = path.stat()
                key = (st.st_dev, st.st_ino)
                hl = None
                if st.st_nlink > 1 and key in seen_inodes:
                    hl = seen_inodes[key]
                elif st.st_nlink > 1:
                    seen_inodes[key] = rel
                files.append((path, rel, st, hl))
                if keep_backup:
                    dst_bak = backup_root / rel
                    dst_bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dst_bak)

            workers = max(1, workers or (os.cpu_count() or 4))
            entries_tmp = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._pack_one, src, rel, st,
                                       tmp_dir, i, hl)
                           for i, (src, rel, st, hl) in enumerate(files)]
                for fut in as_completed(futures):
                    entries_tmp.append(fut.result())
            entries_tmp.sort(key=lambda e: e["_idx"])

            offset = 0
            entries = []
            with open(data_path, 'wb') as f_data:
                for e in entries_tmp:
                    if e["_buf"] is not None:
                        with open(e["_buf"], 'rb') as f_buf:
                            shutil.copyfileobj(f_buf, f_data)
                        e["_buf"].unlink(missing_ok=True)
                    out = {"path": e["path"], "size": e["size"],
                           "sha256": e["sha256"], "mode": e["mode"],
                           "algo": e["algo"], "offset": offset,
                           "clen": e["clen"]}
                    if e.get("hardlink_to"):
                        out["hardlink_to"] = e["hardlink_to"]
                    entries.append(out)
                    offset += e["clen"]

            manifest = {"kind": "dir", "root": root.name,
                        "dirs": dirs_meta, "links": links_meta,
                        "entries": entries}
            with open(output_path, 'wb') as f_final, open(data_path, 'rb') as f_data:
                self._write_header(f_final, 'D', manifest)
                shutil.copyfileobj(f_data, f_final)

            print(f"🎉 Каталог упакован: {output_path} "
                  f"({len(entries)} записей, {len(dirs_meta)} каталогов, "
                  f"{len(links_meta)} ссылок)")
            return str(output_path)
        except Exception as e:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"Сбой упаковки каталога: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def decompress_dir(self, archive_path: str,
                        only: list[str] | None = None) -> str:
        arch = Path(archive_path)
        if not arch.is_file():
            raise FileNotFoundError(f"Архив не найден: {archive_path}")

        only_set = set(only) if only else None
        if only_set:
            for rel in only_set:
                if self._safe_rel(rel) is None:
                    raise ValueError(f"Небезопасный путь в only: {rel}")

        out_root = arch.parent / (arch.stem + "_extracted")
        try:
            with open(arch, 'rb') as f_in:
                kind, manifest, data_start = self._read_header(f_in)
                if kind != 'dir' or manifest.get("kind") != "dir":
                    raise ValueError("Это не каталоговый контейнер")

            try:
                out_root.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise RuntimeError(
                    f"Каталог уже существует: {out_root}. Удалите или укажите другое место.")

            dirs_to_make = manifest.get("dirs", [])
            if only_set is not None:
                needed = set()
                for rel in only_set:
                    parts = Path(rel).parts
                    for i in range(1, len(parts)):
                        needed.add('/'.join(parts[:i]))
                dirs_to_make = [d for d in dirs_to_make if d["path"] in needed]

            for d in sorted(dirs_to_make, key=lambda x: x["path"].count('/')):
                safe = self._safe_rel(d["path"])
                if safe is None:
                    continue
                target_dir = out_root / safe
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(target_dir, d.get("mode", 0o755))
                except OSError:
                    pass

            for link in manifest.get("links", []):
                if only_set is not None and link["path"] not in only_set:
                    continue
                safe = self._safe_rel(link["path"])
                if safe is None:
                    continue
                target_link = out_root / safe
                target_link.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if target_link.exists() or target_link.is_symlink():
                        target_link.unlink()
                    os.symlink(link["target"], target_link)
                except OSError as e:
                    print(f"⚠️ Симлинк {link['path']}: {e}")

            failed = []
            pending_links = []
            with open(arch, 'rb') as f_in:
                f_in.seek(data_start)
                for entry in manifest["entries"]:
                    if entry["algo"] == "hardlink":
                        pending_links.append(entry)
                        continue
                    if only_set is not None and entry["path"] not in only_set:
                        f_in.seek(entry["clen"], io.SEEK_CUR)
                        continue
                    safe = self._safe_rel(entry["path"])
                    if safe is None:
                        continue
                    target = out_root / safe
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with open(target, 'wb') as f_out:
                            self._decompress_one_stream(
                                f_in, f_out, entry["algo"], entry["clen"])
                        if self._calculate_sha256(target) != entry["sha256"]:
                            target.unlink()
                            raise ValueError("SHA-256 не совпадает")
                        try:
                            os.chmod(target, entry.get("mode", 0o644))
                        except OSError:
                            pass
                    except Exception as e:
                        print(f"  ⚠️ {entry['path']}: {e}")
                        failed.append(entry["path"])

            for entry in pending_links:
                if only_set is not None and entry["path"] not in only_set:
                    continue
                safe = self._safe_rel(entry["path"])
                src_safe = self._safe_rel(entry["hardlink_to"])
                if safe is None or src_safe is None:
                    continue
                target = out_root / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                src_path = out_root / src_safe
                if not src_path.exists():
                    print(f"⚠️ Цель хардлинка отсутствует: {entry['hardlink_to']}")
                    failed.append(entry["path"])
                    continue
                if target.exists() or target.is_symlink():
                    target.unlink()
                try:
                    os.link(src_path, target)
                except OSError:
                    shutil.copy2(src_path, target)

            if only_set is not None:
                restored = {e["path"] for e in manifest["entries"]} | \
                           {l["path"] for l in manifest.get("links", [])}
                missing = only_set - restored
                if missing:
                    print(f"⚠️ Запрошены, но отсутствуют: {sorted(missing)}")

            if failed:
                print(f"⚠️ Не восстановлено: {failed}")
            else:
                print(f"✅ Каталог восстановлен: {out_root}")
            return str(out_root)

        except Exception as e:
            raise RuntimeError(f"Сбой распаковки каталога: {e}")

    # -------------------------------------------------------------------- test

    def test(self, archive_path: str, workers: int | None = None) -> dict:
        arch = Path(archive_path)
        report = {"ok": True, "errors": [], "checked": 0,
                  "total": 0, "kind": None}
        if not arch.is_file():
            report["ok"] = False
            report["errors"].append(f"Файл не найден: {arch}")
            return report
        try:
            with open(arch, 'rb') as f_in:
                kind, manifest, data_start = self._read_header(f_in)
                report["kind"] = kind
                if kind == 'file':
                    self._test_file_container(arch, manifest, data_start, report)
                else:
                    self._test_dir_container(arch, manifest, data_start,
                                             report, workers)
        except Exception as e:
            report["ok"] = False
            report["errors"].append(f"Исключение: {e}")
        return report

    def _test_file_container(self, arch, manifest, data_start, report):
        report["total"] = 1
        with open(arch, 'rb') as f_in:
            f_in.seek(data_start)
            clen = arch.stat().st_size - data_start
            ok = self._verify_stream(f_in, manifest["algo"], clen,
                                     manifest["sha256"])
        if ok:
            report["checked"] = 1
        else:
            report["ok"] = False
            report["errors"].append(
                f"{manifest.get('name', arch.name)}: SHA-256 не сошёлся")

    def _verify_stream(self, f_in, algo: str, clen: int, expected_sha: str) -> bool:
        try:
            h = hashlib.sha256()
            remaining = clen
            if algo == 'store':
                while remaining > 0:
                    buf = f_in.read(min(1 << 20, remaining))
                    if not buf:
                        return False
                    h.update(buf)
                    remaining -= len(buf)
            elif algo == 'lzma':
                dec = lzma.LZMADecompressor()
                while remaining > 0:
                    buf = f_in.read(min(1 << 20, remaining))
                    if not buf:
                        return False
                    remaining -= len(buf)
                    out = dec.decompress(buf)
                    if out:
                        h.update(out)
                if not dec.eof:
                    out = dec.decompress(b'')
                    if out:
                        h.update(out)
                if not dec.eof:
                    return False
            elif algo == 'bz2':
                dec = bz2.BZ2Decompressor()
                while remaining > 0:
                    buf = f_in.read(min(1 << 20, remaining))
                    if not buf:
                        return False
                    remaining -= len(buf)
                    out = dec.decompress(buf)
                    if out:
                        h.update(out)
                if not dec.eof:
                    out = dec.decompress(b'')
                    if out:
                        h.update(out)
                if not dec.eof:
                    return False
            else:
                return False
            return h.hexdigest() == expected_sha
        except Exception:
            return False

    def _verify_blob(self, blob: bytes, algo: str, expected_sha: str) -> bool:
        try:
            with io.BytesIO(blob) as bio:
                return self._verify_stream(bio, algo, len(blob), expected_sha)
        except Exception:
            return False

    def _verify_one_entry(self, arch: Path, data_start: int,
                           entry: dict) -> tuple[str, bool, str | None]:
        if entry.get("algo") == "hardlink":
            return entry["path"], True, None
        try:
            with open(arch, 'rb') as f_in:
                f_in.seek(data_start + entry["offset"])
                ok = self._verify_stream(f_in, entry["algo"],
                                         entry["clen"], entry["sha256"])
            return entry["path"], ok, None if ok else "SHA-256 не сошёлся"
        except Exception as e:
            return entry["path"], False, f"исключение: {e}"

    def _test_dir_container(self, arch, manifest, data_start, report,
                             workers: int | None = None):
        entries = manifest.get("entries", [])
        report["total"] = len(entries)
        offset = 0
        for e in entries:
            if e.get("algo") == "hardlink":
                continue
            if e.get("offset", offset) != offset:
                report["errors"].append(
                    f"Непоследовательный offset у {e['path']}")
                report["ok"] = False
            offset += e["clen"]
        if data_start + offset != arch.stat().st_size:
            report["errors"].append(
                f"Размер не сходится: data_start({data_start}) + "
                f"sum(clen)({offset}) != file_size({arch.stat().st_size})")
            report["ok"] = False

        workers = max(1, workers or (os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._verify_one_entry,
                                   arch, data_start, e) for e in entries]
            for fut in as_completed(futures):
                path, ok, err = fut.result()
                if ok:
                    report["checked"] += 1
                else:
                    report["ok"] = False
                    report["errors"].append(f"{path}: {err}")

    # ------------------------------------------------------------------ repair

    def repair(self, archive_path: str) -> dict:
        arch = Path(archive_path)
        report = {"ok": True, "repaired": [], "unrecoverable": [],
                  "skipped": [], "errors": []}
        if not arch.is_file():
            report["ok"] = False
            report["errors"].append(f"Архив не найден: {arch}")
            return report
        try:
            with open(arch, 'rb') as f_in:
                kind, manifest, data_start = self._read_header(f_in)
        except Exception as e:
            report["ok"] = False
            report["errors"].append(f"Заголовок не читается: {e}")
            return report
        if kind == 'file':
            self._repair_file(arch, manifest, data_start, report)
        else:
            self._repair_dir(arch, manifest, data_start, report)
        return report

    def _repair_file(self, arch, manifest, data_start, report):
        name = manifest.get("name", arch.name)
        safe = self._safe_rel(name) or Path("restored_file")
        bak = arch.parent / (arch.name + '.bak')

        if manifest.get("algo") == "hardlink":
            report["ok"] = False
            report["unrecoverable"].append(
                {"path": str(safe), "reason": "hardlink в файловом режиме невозможен"})
            return
        if not bak.is_file():
            report["unrecoverable"].append(
                {"path": str(safe), "reason": "нет .bak"})
            report["ok"] = False
            return
        if self._calculate_sha256(bak) != manifest["sha256"]:
            report["unrecoverable"].append(
                {"path": str(safe), "reason": "SHA-256 .bak не совпадает"})
            report["ok"] = False
            return

        with open(arch, 'rb') as f_in:
            f_in.seek(data_start)
            ok = self._verify_stream(f_in, manifest["algo"],
                                     arch.stat().st_size - data_start,
                                     manifest["sha256"])
        if ok:
            report["skipped"].append(
                {"path": str(safe), "reason": "контейнер уже цел"})
            return

        tmp_arch = arch.parent / (arch.name + '.repair_tmp')
        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_repair_", dir=arch.parent))
        try:
            payload = tmp_dir / "payload.bin"
            self._compress_one_to(bak, payload, manifest["algo"])
            with open(tmp_arch, 'wb') as f_out, open(payload, 'rb') as f_data:
                self._write_header(f_out, 'F', manifest)
                shutil.copyfileobj(f_data, f_out)
            tmp_arch.replace(arch)
            report["repaired"].append({"path": str(safe), "from": str(bak)})
        except Exception as e:
            tmp_arch.unlink(missing_ok=True)
            report["ok"] = False
            report["unrecoverable"].append(
                {"path": str(safe), "reason": f"сбой пересборки: {e}"})
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _repair_dir(self, arch, manifest, data_start, report):
        bak_root = arch.parent / (arch.name + '.bak')
        entries = manifest.get("entries", [])

        if not bak_root.is_dir():
            with open(arch, 'rb') as f_in:
                for e in entries:
                    if e.get("algo") == "hardlink":
                        continue
                    f_in.seek(data_start + e["offset"])
                    ok = self._verify_stream(
                        f_in, e["algo"], e["clen"], e["sha256"])
                    if not ok:
                        report["unrecoverable"].append(
                            {"path": e["path"], "reason": "нет .bak"})
                        report["ok"] = False
            return

        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_repair_", dir=arch.parent))
        try:
            buf_map = {}
            new_entries = []
            new_offset = 0
            with open(arch, 'rb') as f_in:
                for e in entries:
                    if e.get("algo") == "hardlink":
                        new_entries.append({**e})
                        continue

                    f_in.seek(data_start + e["offset"])
                    blob = f_in.read(e["clen"])
                    if len(blob) != e["clen"]:
                        report["ok"] = False
                        report["unrecoverable"].append(
                            {"path": e["path"],
                             "reason": "недостаточно данных в архиве"})
                        continue

                    if self._verify_blob(blob, e["algo"], e["sha256"]):
                        buf = tmp_dir / f"ok_{new_offset:016d}.bin"
                        buf.write_bytes(blob)
                        buf_map[new_offset] = buf
                        new_entries.append({**e, "offset": new_offset,
                                            "clen": len(blob)})
                        new_offset += len(blob)
                        continue

                    safe = self._safe_rel(e["path"])
                    if safe is None:
                        report["ok"] = False
                        report["unrecoverable"].append(
                            {"path": e["path"], "reason": "небезопасный путь"})
                        buf = tmp_dir / f"bad_{new_offset:016d}.bin"
                        buf.write_bytes(blob)
                        buf_map[new_offset] = buf
                        new_entries.append({**e, "offset": new_offset,
                                            "clen": len(blob)})
                        new_offset += len(blob)
                        continue

                    bak = bak_root / safe
                    if not bak.is_file() or \
                            self._calculate_sha256(bak) != e["sha256"]:
                        report["unrecoverable"].append(
                            {"path": e["path"], "reason": "нет валидного .bak"})
                        report["ok"] = False
                        buf = tmp_dir / f"bad_{new_offset:016d}.bin"
                        buf.write_bytes(blob)
                        buf_map[new_offset] = buf
                        new_entries.append({**e, "offset": new_offset,
                                            "clen": len(blob)})
                        new_offset += len(blob)
                        continue

                    buf = tmp_dir / f"fix_{new_offset:016d}.bin"
                    try:
                        self._compress_one_to(bak, buf, e["algo"])
                        buf_map[new_offset] = buf
                        new_entries.append({**e, "offset": new_offset,
                                            "clen": buf.stat().st_size})
                        new_offset += buf.stat().st_size
                        report["repaired"].append(
                            {"path": e["path"], "from": str(bak)})
                    except Exception as ex:
                        report["ok"] = False
                        report["unrecoverable"].append(
                            {"path": e["path"],
                             "reason": f"сбой пересборки: {ex}"})
                        buf = tmp_dir / f"bad_{new_offset:016d}.bin"
                        buf.write_bytes(blob)
                        buf_map[new_offset] = buf
                        new_entries.append({**e, "offset": new_offset,
                                            "clen": len(blob)})
                        new_offset += len(blob)

            new_manifest = {**manifest, "entries": new_entries}
            new_manifest.pop("data_start", None)

            tmp_arch = arch.parent / (arch.name + '.repair_tmp')
            with open(tmp_arch, 'wb') as f_out:
                self._write_header(f_out, 'D', new_manifest)
                for e in new_entries:
                    if e.get("algo") == "hardlink":
                        continue
                    buf = buf_map[e["offset"]]
                    with open(buf, 'rb') as f_buf:
                        shutil.copyfileobj(f_buf, f_out)
            tmp_arch.replace(arch)
        except Exception as e:
            report["ok"] = False
            report["errors"].append(f"Сбой ремонта: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ bench

    def bench(self, target: str) -> dict:
        p = Path(target)
        if p.is_file():
            return self._bench_file(p)
        if p.is_dir():
            return self._bench_dir(p)
        raise FileNotFoundError(target)

    def _bench_file(self, src: Path) -> dict:
        results = []
        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_bench_", dir=src.parent))
        try:
            expected_sha = self._calculate_sha256(src)
            for algo in ("store", "bz2", "lzma"):
                buf = tmp_dir / f"{algo}.bin"
                t0 = time.perf_counter()
                try:
                    size = self._compress_one_to(src, buf, algo)
                except Exception as e:
                    results.append({"algo": algo, "error": str(e)})
                    continue
                t_comp = time.perf_counter() - t0
                t0 = time.perf_counter()
                ok = self._roundtrip_ok(src, buf, algo, expected_sha)
                t_rt = time.perf_counter() - t0
                results.append({
                    "algo": algo, "payload": size,
                    "container_est": size + self.HEADER_OVERHEAD,
                    "comp_s": round(t_comp, 4),
                    "roundtrip_ok": ok,
                    "roundtrip_s": round(t_rt, 4),
                })
                buf.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        orig = src.stat().st_size
        for r in results:
            if "container_est" in r:
                r["saved"] = orig - r["container_est"]
                r["saved_pct"] = round(100 * r["saved"] / orig, 2) if orig else 0.0
        return {"kind": "file", "name": src.name, "orig": orig, "results": results}

    def _bench_dir(self, root: Path) -> dict:
        files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
        orig = sum(p.stat().st_size for p in files)
        tmp_dir = Path(tempfile.mkdtemp(prefix="nova_bench_", dir=root.parent))
        summary = {}
        try:
            for algo in ("store", "bz2", "lzma"):
                total = 0
                failed = 0
                for p in files:
                    buf = tmp_dir / f"{algo}_{p.name}.bin"
                    try:
                        total += self._compress_one_to(p, buf, algo)
                    except Exception:
                        failed += 1
                    finally:
                        buf.unlink(missing_ok=True)
                summary[algo] = {"payload": total, "failed": failed,
                                 "saved": orig - total,
                                 "saved_pct": round(100*(orig-total)/orig, 2) if orig else 0.0}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return {"kind": "dir", "root": root.name, "orig": orig,
                "files": len(files), "summary": summary}

    # ----------------------------------------------------------------- crypto

    def encrypt_file(self, src_path: str, password: str,
                     dst_path: str | None = None) -> str:
        if not _CRYPTO_OK:
            raise RuntimeError("Установите cryptography: pip install cryptography")
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(src_path)
        dst = Path(dst_path) if dst_path else src.parent / (src.name + '.enc')

        salt = os.urandom(16)
        nonce = os.urandom(12)
        kdf = Scrypt(salt=salt, length=32,
                     n=self.SCRYPT_N, r=self.SCRYPT_R, p=self.SCRYPT_P)
        key = kdf.derive(password.encode('utf-8'))
        header = self.ENC_MAGIC + self.ENC_VERSION + salt + nonce
        aes = AESGCM(key)
        plaintext = src.read_bytes()
        ct = aes.encrypt(nonce, plaintext, associated_data=header)
        dst.write_bytes(header + ct)
        return str(dst)

    def decrypt_file(self, enc_path: str, password: str,
                     dst_path: str | None = None) -> str:
        if not _CRYPTO_OK:
            raise RuntimeError("Установите cryptography: pip install cryptography")
        src = Path(enc_path)
        if not src.is_file():
            raise FileNotFoundError(enc_path)
        if dst_path:
            dst = Path(dst_path)
        elif src.name.endswith('.enc'):
            dst = src.parent / src.name[:-4]
        else:
            dst = src.parent / (src.name + '.dec')

        raw = src.read_bytes()
        if raw[:4] != self.ENC_MAGIC:
            raise ValueError("Не .enc файл")
        if raw[4:5] != self.ENC_VERSION:
            raise ValueError(f"Неизвестная версия: {raw[4:5]!r}")
        salt = raw[5:21]
        nonce = raw[21:33]
        header = raw[:33]
        ct = raw[33:]

        kdf = Scrypt(salt=salt, length=32,
                     n=self.SCRYPT_N, r=self.SCRYPT_R, p=self.SCRYPT_P)
        key = kdf.derive(password.encode('utf-8'))
        try:
            plaintext = AESGCM(key).decrypt(nonce, ct, associated_data=header)
        except Exception as e:
            raise ValueError("Неверный пароль или повреждённый файл") from e
        dst.write_bytes(plaintext)
        return str(dst)


# ---------------------------------------------------------------------- CLI

def _cli() -> int:
    nova = NovaCompress()
    if len(sys.argv) < 3:
        print("Использование:\n"
              "  python nova.py test <archive>\n"
              "  python nova.py repair <archive>\n"
              "  python nova.py bench <file_or_dir>\n"
              "  python nova.py pack <file> [--backup]\n"
              "  python nova.py packdir <dir> [--backup]\n"
              "  python nova.py unpack <archive>\n"
              "  python nova.py unpackdir <archive> [--only path1,path2]\n"
              "  python nova.py encrypt <file> <password>\n"
              "  python nova.py decrypt <file.enc> <password>")
        return 1
    cmd = sys.argv[1]
    path = sys.argv[2]
    backup = '--backup' in sys.argv
    try:
        if cmd == 'test':
            rep = nova.test(path)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0 if rep["ok"] else 2
        if cmd == 'repair':
            rep = nova.repair(path)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0 if rep["ok"] else 2
        if cmd == 'bench':
            rep = nova.bench(path)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0
        if cmd == 'pack':
            print(nova.compress(path, keep_backup=backup))
        elif cmd == 'packdir':
            print(nova.compress_dir(path, keep_backup=backup))
        elif cmd == 'unpack':
            print(nova.decompress(path))
        elif cmd == 'unpackdir':
            only = None
            for i, a in enumerate(sys.argv):
                if a == '--only' and i + 1 < len(sys.argv):
                    only = [x for x in sys.argv[i + 1].split(',') if x]
            print(nova.decompress_dir(path, only=only))
        elif cmd == 'encrypt':
            args = [a for a in sys.argv[2:] if not a.startswith('--')]
            if len(args) < 2:
                print("Нужен пароль")
                return 1
            print(nova.encrypt_file(args[0], args[1]))
        elif cmd == 'decrypt':
            args = [a for a in sys.argv[2:] if not a.startswith('--')]
            if len(args) < 2:
                print("Нужен пароль")
                return 1
            print(nova.decrypt_file(args[0], args[1]))
        else:
            print(f"Неизвестная команда: {cmd}")
            return 1
        return 0
    except Exception as e:
        print(f"❌ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
