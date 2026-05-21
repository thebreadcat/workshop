# Security

## What must never be committed

- `~/.workshop/config.json` (may contain `api_key`)
- `.tortoise/config.json` in any app under `~/workshop-apps/` or your `apps_dir`
- API keys, tokens, or passwords in source files

This repository ships only `config.example.json` with `api_key` set to `null`.

## Safe setup

On first run, Workshop creates `~/.workshop/config.json`. Set your endpoint in the UI or copy the example:

```bash
mkdir -p ~/.workshop
cp config.example.json ~/.workshop/config.json
```

## Tortoise

[Tortoise](https://github.com/thebreadcat/tortoise) stores per-app secrets in each project's `.tortoise/config.json` (gitignored by `tortoise init`). Workshop does not bundle Tortoise — clone or submodule it separately.
