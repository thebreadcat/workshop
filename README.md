# Workshop

Natural-language app builder powered by [Tortoise](https://github.com/thebreadcat/tortoise) — a chunk-based harness for local LLMs.

**Zero dependencies** beyond Python 3.10+ stdlib. Tortoise is **not** bundled in this repo — install it yourself (sibling clone, local `vendor/`, or `TORTOISE_PATH`).

## Requirements

- Python 3.10+
- A running LLM endpoint (Ollama, LM Studio, etc.)
- [Tortoise](https://github.com/thebreadcat/tortoise) v0.2.0+

## Setup

### Option A — Local `vendor/` clone (optional, not tracked by git)

```bash
git clone https://github.com/thebreadcat/workshop.git
cd workshop
git clone https://github.com/thebreadcat/tortoise.git vendor/tortoise
python3 workshop.py
# Open http://localhost:7700
```

### Option B — Sibling directories

```bash
git clone https://github.com/thebreadcat/workshop.git
git clone https://github.com/thebreadcat/tortoise.git
# Layout:
#   prototypes/workshop/      (this repo)
#   prototypes/tortoise/
cd workshop
python3 workshop.py
```

Workshop looks for Tortoise at `../tortoise/tortoise.py` or `vendor/tortoise/tortoise.py`.

### Option C — Explicit path

```bash
export TORTOISE_PATH=/path/to/tortoise/tortoise.py
python3 workshop.py
```

## Local config (not in git)

Workshop stores your model endpoint in `~/.workshop/config.json` (may include `api_key`). See [config.example.json](config.example.json). Built apps default to `~/workshop-apps/`.

## Related repo

| Repo | Role |
|------|------|
| [thebreadcat/workshop](https://github.com/thebreadcat/workshop) | This project — web UI and build loop |
| [thebreadcat/tortoise](https://github.com/thebreadcat/tortoise) | Chunk harness Workshop drives per app |

## License

[Tortoise License](LICENSE) — same terms as [Tortoise](https://github.com/thebreadcat/tortoise/blob/main/LICENSE). Free to use and modify; you may not sell the software or offer paid support for the codebase itself.
