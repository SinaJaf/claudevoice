#!/usr/bin/env bash
# Optional one-shot setup for claudevoice on macOS or Linux: system deps, Python env, model
# downloads. The Claude Code plugin does the Python part on first use by itself; run this to
# front-load the downloads or to use the terminal tools (`claudevoice chat`, always-on hooks).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if ! command -v uv >/dev/null; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ (macOS: brew install uv)"; exit 1
fi
if ! command -v espeak-ng >/dev/null; then
  echo "▸ espeak-ng (Kokoro's pronunciation fallback) is missing"
  if command -v brew >/dev/null; then brew install espeak-ng
  elif command -v apt-get >/dev/null; then sudo apt-get install -y espeak-ng libportaudio2
  elif command -v dnf >/dev/null; then sudo dnf install -y espeak-ng portaudio
  else echo "  install espeak-ng with your package manager, then re-run"; exit 1
  fi
fi

echo "▸ installing python deps (first time pulls torch; ~1 GB)"
uv sync --quiet

echo "▸ downloading Kokoro weights and warming the model"
uv run --quiet python -c "
import warnings; warnings.filterwarnings('ignore')
from kokoro import KPipeline
p = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
list(p('Kokoro is ready.', voice='af_heart'))
print('  kokoro ok')
" 2>&1 | grep -v Redirects || true

if [[ "${CLAUDEVOICE_SKIP_WHISPER:-}" == "" ]]; then
  echo "▸ downloading the local Whisper model (set CLAUDEVOICE_SKIP_WHISPER=1 to skip)"
  uv run --quiet python -m claudevoice.listen --warm 2>&1 | grep -v Redirects | tail -1 || true
fi

if [[ "${CLAUDEVOICE_ALWAYS_ON_HOOKS:-}" != "" ]]; then
  echo "▸ registering the optional always-on Claude Code hooks in ~/.claude/settings.json"
  uv run --quiet python "$HERE/scripts/install_hooks.py"
fi

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claudevoice" <<WRAP
#!/usr/bin/env bash
exec uv run --quiet --project "$HERE" python -m claudevoice "\$@"
WRAP
chmod +x "$HOME/.local/bin/claudevoice"

echo
echo "done. the voice controls appear in Claude Code's prompt footer once the plugin is installed:"
echo "  /plugin install claudevoice --marketplace <owner>/claudevoice"
echo "other tools:  claudevoice say \"hello\"   ·   claudevoice chat   ·   claudevoice config"
