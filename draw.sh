#!/bin/bash
# Генерация картинки локально через Flux (mflux, MLX).
#   ./draw.sh "промпт"                     — быстро, 768x768
#   ./draw.sh "промпт" fullhd              — пресет размера
#   ./draw.sh "промпт" 1024x768 4          — свой размер и число шагов
# Пресеты: square, portrait, landscape, hd, fullhd, story, big
# Русский промпт автоматически переводится на английский через Ollama —
# Flux заметно точнее понимает английские описания.
#   RAW=1 ./draw.sh "..."                  — отправить промпт как есть, без перевода
# Результат кладётся в out/ с именем по времени, путь печатается в конце.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPT="${1:?укажи промпт первым аргументом}"
SIZE="${2:-square}"
STEPS="${3:-2}"

# Размер: либо название пресета, либо WxH, либо просто ширина (тогда квадрат).
case "$(echo "$SIZE" | tr '[:upper:]' '[:lower:]')" in
  square|sq)          W=768;  H=768 ;;
  portrait|vert)      W=768;  H=1024 ;;
  landscape|horiz)    W=1024; H=768 ;;
  hd|720p)            W=1280; H=720 ;;
  fullhd|1080p|fhd)   W=1920; H=1080 ;;
  story|reels|9x16)   W=1080; H=1920 ;;
  big|1k)             W=1024; H=1024 ;;
  2k|1440p)           W=2560; H=1440 ;;
  4k|uhd)             W=3840; H=2160 ;;
  *x*)                W="${SIZE%x*}"; H="${SIZE#*x}" ;;
  ''|*[!0-9]*)
      echo "Не понял размер '$SIZE'." >&2
      echo "Пресеты: square, portrait, landscape, hd, fullhd, story, big" >&2
      echo "Или явно: 1024x768   Или одно число: 1024 (квадрат)" >&2
      exit 1 ;;
  *)                  W="$SIZE"; H="$SIZE" ;;
esac

# Flux требует размеры, кратные 16.
W=$(( (W / 16) * 16 ))
H=$(( (H / 16) * 16 ))

# Выше ~1 Мпикс памяти перестаёт хватать с запасом — включаем экономный режим.
LOWRAM=""
if [ $((W * H)) -gt 1100000 ]; then
  LOWRAM="--low-ram"
  echo "Размер ${W}x${H} большой: включён экономный режим, будет медленнее." >&2
fi
if [ $((W * H)) -gt 4000000 ]; then
  echo "ВНИМАНИЕ: выше ~4 Мпикс Flux начинает дублировать объекты (две плиты," >&2
  echo "лишние окна и т.п.) и уходит в своп. Лучше сгенерировать в fullhd и" >&2
  echo "увеличить апскейлером. Продолжаю через 5 сек, Ctrl+C чтобы отменить." >&2
  sleep 5
fi

TRANSLATOR="qwen2.5:7b-instruct"
SYS='You rewrite image descriptions into English prompts for a text-to-image model.
Output ONLY the final English prompt: no quotes, no explanations, no preamble.
Keep every detail from the original. Use concrete visual language and keep the
described style, lighting and composition exactly as given. Do not invent a new subject.'

# Переводим только если в промпте есть кириллица и перевод не отключён явно.
if [ -z "$RAW" ] && printf '%s' "$PROMPT" | grep -q '[А-Яа-яЁё]'; then
  echo "Перевод через $TRANSLATOR ..." >&2
  REQ=$(jq -n --arg m "$TRANSLATOR" --arg s "$SYS" --arg p "$PROMPT" \
    '{model:$m, stream:false, keep_alive:"30m", options:{temperature:0},
      messages:[{role:"system",content:$s},{role:"user",content:$p}]}')
  EN=$(curl -s --max-time 120 http://localhost:11434/api/chat -d "$REQ" \
       | jq -r '.message.content // empty' | tr -d '"' | tr '\n' ' ' | sed 's/  */ /g;s/^ *//;s/ *$//')
  if [ -n "$EN" ]; then
    PROMPT="$EN"
    echo "→ $PROMPT" >&2
  else
    echo "Перевод не удался (Ollama не отвечает?), беру исходный промпт." >&2
  fi
fi

mkdir -p "$DIR/out"
OUT="$DIR/out/$(date +%Y%m%d-%H%M%S).png"

echo "Генерация ${W}x${H}, шагов: $STEPS" >&2
"$DIR/.venv/bin/mflux-generate" \
  --model "$DIR/models/schnell-4bit" \
  --base-model schnell \
  --prompt "$PROMPT" \
  --steps "$STEPS" \
  --width "$W" \
  --height "$H" \
  $LOWRAM \
  --output "$OUT"

# Апскейл до 4K отдельным проходом: качественнее, чем генерировать 4K напрямую.
if [ -n "$UPSCALE" ]; then
  RES="${UPSCALE_RES:-2160}"
  BIG="${OUT%.png}-${RES}p.png"
  echo "Апскейл до ${RES}p ..." >&2
  if "$DIR/.venv/bin/mflux-upscale-seedvr2" \
       --image-path "$OUT" \
       --resolution "$RES" \
       --low-ram \
       --output "$BIG" >&2; then
    OUT="$BIG"
  else
    echo "Апскейл не удался, оставляю исходный файл." >&2
  fi
fi

echo "$OUT"
open "$OUT" 2>/dev/null || true
