# tools/tool.py

Инструмент для работы с ассетами и исходниками проекта DMC3D Engine.
Работает с файлами репозитория, а не с готовым APK. Только стандартная библиотека Python 3.

Бэкапы пишутся в `.tool_backup/<timestamp>/<относительный_путь>`. Эта папка локальная, в коммит не добавляется, `.gitignore` не меняется.

## Команды

Все команды запускаются из корня репозитория.

### list
```bash
python3 tools/tool.py list
python3 tools/tool.py list --type textures
```
Выводит тип, путь и размер каждого ассета в `app/src/main/assets/`.

### add
```bash
python3 tools/tool.py add ./dante.png --type sprites
python3 tools/tool.py add ./yamato.ogg --type sounds --name yamato_slash.ogg
python3 tools/tool.py add ./dante.png --type sprites --force
```
- Проверяет расширение под тип и размер (не больше 5 МБ).
- `--name` не может содержать пути и `..`.
- Без `--force` существующий файл не перезаписывает. С `--force` делает бэкап.

### replace
```bash
python3 tools/tool.py replace app/src/main/assets/sprites/dante.png ./dante_new.png
```
Заменяет только файлы внутри `app/src/main/assets/`. Старый файл сохраняется в бэкап.

### check
```bash
python3 tools/tool.py check
```
Предупреждает о:
- неизвестных типах файлов;
- файлах больше 5 МБ;
- PNG-текстурах больше 2048 px или не по степени двойки (только PNG, для jpg/webp проверяется только размер файла);
- звуках не в `.ogg` или `.mp3`.

Не падает на пустой папке.

### patch-code
Работает только с файлами внутри `app/`.

Создать новый класс. Пакет определяется из пути (`src/main/java/...`), либо задаётся через `--package`:
```bash
python3 tools/tool.py patch-code --template new-class \
  --target app/src/main/java/com/game/engine/CombatSystem.kt \
  --name CombatSystem --dry-run
```

Добавить поле в конец существующего класса:
```bash
python3 tools/tool.py patch-code --template add-field \
  --target app/src/main/java/com/game/engine/CoreRenderer.kt \
  --class-name CoreRenderer \
  --name debugOverlay --field-type Boolean --default false --dry-run
```
- Закрывающая `}` класса ищется с учётом строк, `//`, `/* */`, тройных кавычек и вложенных `{}`.
- Показывает unified diff. Отказывает, если поле с таким именем уже есть или имя некорректно.
- Перед записью делает бэкап.

**`--dry-run` ничего не пишет**: ни целевой файл, ни `.tool_backup/`. Проверка:
```bash
git status --short            # до
python3 tools/tool.py patch-code --template add-field ... --dry-run
git status --short            # после: должно совпадать
ls .tool_backup 2>/dev/null   # не должно появиться новой папки
```

### build
```bash
python3 tools/tool.py build
```
- Если `gradle` есть в PATH, запускает `gradle assembleDebug`.
- Иначе, если есть `./gradlew`, запускает `./gradlew assembleDebug`.
- Иначе печатает команду и ссылку на GitHub Actions.

Код возврата совпадает с кодом Gradle.

## Ограничения
- Не удаляет файлы.
- Не меняет `build.gradle`, `settings.gradle`, `gradle.properties`.
- Не правит APK.
- `replace` и `add` работают только с `app/src/main/assets/`, `patch-code` только с `app/`.
- Валидация OGG/MP3 только по расширению, без чтения содержимого.

## Не реализовано
- Атлас спрайтов (PNG → атлас + JSON).
- Шаблоны под конкретные методы `CoreRenderer`, `SkeletalMesh`, `EnemyDummy`.

## Статус проверки
Проверено запуском: `list`, `check`, `add` (некорректное имя), `replace` (вне assets и внутри assets с бэкапом), `patch-code` (запрет вне `app/`, дубликат поля, некорректное имя, dry-run, реальная запись с бэкапом).
Не проверено: сборка Gradle и компиляция Kotlin после `add-field`. Это проверяется в GitHub Actions.
