# tools/packer.py — упаковщик ассетов (.dmcpack)

Собирает ассеты в один пакет и распаковывает его обратно. Только стандартная библиотека Python 3.

## Формат .dmcpack
- 8 байт сигнатуры `DMCPACK1`
- 4 байта (big-endian) — длина JSON-манифеста
- JSON-манифест: версия, алгоритм, список файлов (путь, категория, размеры, смещение, sha256)
- сжатые данные файлов подряд

## Команды (из корня репозитория)

```bash
python3 tools/packer.py pack --input app/src/main/assets --output /tmp/game.dmcpack
python3 tools/packer.py pack --input app/src/main/assets --output /tmp/game.dmcpack --algorithm zlib --only png,ogg
python3 tools/packer.py info --input /tmp/game.dmcpack
python3 tools/packer.py unpack --input /tmp/game.dmcpack --output /tmp/extracted
python3 tools/packer.py unpack --input /tmp/game.dmcpack --output /tmp/extracted --force
```

- `--algorithm`: `lzma` (по умолчанию, сильнее сжатие), `zlib` (быстрее распаковка и без зависимостей в Android), `none` (для отладки).
- `--only`: только указанные расширения. `--all`: включать файлы с неизвестными расширениями.
- `unpack` проверяет sha256 каждого файла и не пишет данные при несовпадении.
- `unpack` отклоняет пути с `..`, абсолютные пути и пути с диском. При такой записи вся распаковка отменяется, ничего не пишется.

## Ограничения
- Сжатие без потерь. Конвертации в WebP нет, она потребует Pillow.
- Для lzma в Android нужна зависимость `org.tukaani:xz`. Без неё используйте `--algorithm zlib`.
- Движок пока не читает `.dmcpack`: загрузчика в проекте нет.

## Статус проверки
Проверено запуском: pack → info → unpack с совпадением файлов, и три попытки распаковать пакет с небезопасными путями (`../evil.txt`, абсолютный путь, `a/../../x`): все отклонены, файлы не созданы.
