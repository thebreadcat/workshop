#!/usr/bin/env python3
"""
Workshop — natural language app builder powered by Tortoise.
Zero dependencies beyond Python stdlib.

Usage:
    python3 workshop.py              # start on port 7700
    python3 workshop.py --port 8080  # custom port
    python3 workshop.py --host 0.0.0.0  # expose on network (for Pi)
"""

import os, sys, json, re, threading, queue, shutil, socket, importlib.util
import urllib.request, urllib.error, subprocess, argparse, socketserver
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERSION              = "0.1.0"
TORTOISE_MIN_VERSION = "0.2.0"
DEFAULT_PORT         = 7700

# ── Paths ──────────────────────────────────────────────────────────────────────

WORKSHOP_DIR  = Path.home() / ".workshop"
CONFIG_FILE   = WORKSHOP_DIR / "config.json"
DEFAULT_APPS  = Path.home() / "workshop-apps"

DEFAULT_CONFIG = {
    "endpoint": None,
    "model":    None,
    "api_key":  None,
    "apps_dir": str(DEFAULT_APPS),
    "users":    [],
}

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg

def save_config(cfg):
    WORKSHOP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def apps_dir(cfg=None):
    return Path((cfg or load_config()).get("apps_dir") or str(DEFAULT_APPS))

# ── Tortoise loading ───────────────────────────────────────────────────────────

_tortoise_module = None

TORTOISE_INSTALL_HINT = (
    "Tortoise not found. Install via:\n"
    "  git submodule add https://github.com/thebreadcat/tortoise.git vendor/tortoise\n"
    "  git submodule update --init\n"
    "Or clone https://github.com/thebreadcat/tortoise next to this repo, or set TORTOISE_PATH."
)

def _tortoise_candidates():
    """Paths to tortoise.py, in priority order (Tortoise is never bundled in Workshop)."""
    root = Path(__file__).resolve().parent
    env  = os.environ.get("TORTOISE_PATH", "").strip()
    if env:
        yield Path(env)
    yield root / "vendor" / "tortoise" / "tortoise.py"
    yield root.parent / "tortoise" / "tortoise.py"

def load_tortoise():
    global _tortoise_module
    if _tortoise_module:
        return _tortoise_module
    try:
        import tortoise as t
        _tortoise_module = t
        return t
    except ImportError:
        pass
    for path in _tortoise_candidates():
        if path.is_file():
            spec = importlib.util.spec_from_file_location("tortoise", path)
            m    = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _tortoise_module = m
            return m
    return None

def tortoise_script():
    """Path to tortoise.py for subprocess calls."""
    for path in _tortoise_candidates():
        if path.is_file():
            return str(path)
    t = load_tortoise()
    return getattr(t, "__file__", None) if t else None

def tortoise_version():
    t = load_tortoise()
    return getattr(t, "VERSION", "unknown") if t else None

# ── Model detection ────────────────────────────────────────────────────────────

KNOWN_ENDPOINTS = [
    ("Ollama — this machine",   "http://localhost:11434/v1"),
    ("LM Studio — this machine","http://localhost:1234/v1"),
    ("Ollama — Pi",             "http://pi.local:11434/v1"),
    ("LiteLLM — Pi",            "http://pi.local:4000/v1"),
]

def probe_endpoint(url, api_key=None):
    """Returns list of model ids if endpoint responds, else None."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=2) as r:
            return [m["id"] for m in json.loads(r.read()).get("data", [])][:8]
    except Exception:
        return None

def detect_models():
    """Probe all known endpoints in parallel (max 3s total)."""
    results, lock = [], threading.Lock()

    def probe_one(label, url):
        models = probe_endpoint(url)
        if models is not None:
            with lock:
                for m in models:
                    results.append({"label": label, "url": url, "model": m})

    threads = [threading.Thread(target=probe_one, args=(l, u), daemon=True)
               for l, u in KNOWN_ENDPOINTS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=3)
    return results

# ── App registry ───────────────────────────────────────────────────────────────

def app_meta(app_dir: Path, owner: str, scope: str) -> dict:
    td   = app_dir / ".tortoise"
    brain = (td / "BRAIN.md").read_text() if (td / "BRAIN.md").exists() else ""
    prog  = td / "PROGRESS.md"
    last  = (datetime.fromtimestamp(prog.stat().st_mtime).strftime("%b %d, %Y")
             if prog.exists() else None)
    desc  = next(
        (l.strip() for l in brain.split("\n")
         if l.strip() and not l.startswith("#") and not l.startswith("[")),
        ""
    )[:120]
    has_html = (app_dir / "index.html").exists()
    return {
        "name":      app_dir.name,
        "owner":     owner,
        "scope":     scope,
        "path":      str(app_dir),
        "desc":      desc,
        "last_used": last,
        "has_app":   has_html or (app_dir / "main.py").exists(),
        "url":       f"/apps/{owner}/{app_dir.name}/" if has_html else None,
    }

def list_apps(cfg, user=None):
    base = apps_dir(cfg)
    out  = []
    if user:
        ud = base / "users" / user
        if ud.exists():
            for d in sorted(ud.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    out.append(app_meta(d, user, "personal"))
    sd = base / "shared"
    if sd.exists():
        for d in sorted(sd.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                out.append(app_meta(d, "shared", "shared"))
    return out

# ── Decision engine ────────────────────────────────────────────────────────────

INTENT_RULES = [
    (r"forget|remind|medic|alarm|alert|notif",          "remind",   "Reminder"),
    (r"shop|groceri|\bbuy\b|purchase|\bcart\b|pantry",   "shopping", "Shopping list"),
    (r"todo|task|\blist\b|check|errand|chore",           "todo",     "To-do list"),
    (r"\bchart\b|\bgraph\b|visual|analyt|dashboard",     "chart",    "Dashboard"),
    (r"habit|\blog\b|weight|mood|exercise|\bspend\b|trac","tracker", "Tracker"),
    (r"note|journal|diary|write|idea|thought",           "notes",    "Notes app"),
    (r"calculat|convert|measur|unit|\btip\b",            "calc",     "Calculator"),
    (r"game|quiz|puzzle|\bplay\b|trivia|flash",          "game",     "Game"),
    (r"script|automat|batch|schedul|rename|\bsort\b",    "script",   "Script"),
    (r"timer|stopwatch|countdown|\bclock\b|pomodoro",    "timer",    "Timer"),
]

STACKS = {
    "html": {
        "tech":  "Single HTML file, vanilla JS, localStorage",
        "file":  "index.html",
        "rules": [
            "Single HTML file — no separate JS or CSS files",
            "Zero external dependencies",
            "localStorage for all data persistence",
            "Mobile-first layout — 390px base, 44px minimum touch targets",
            "Never break existing saved data when the app is updated",
        ],
    },
    "html_chart": {
        "tech":  "Single HTML file, Chart.js via CDN, localStorage",
        "file":  "index.html",
        "rules": [
            "Single HTML file — no separate JS or CSS files",
            "Chart.js from cdnjs is the only permitted external library",
            "localStorage for all data persistence",
            "Mobile-first layout — 390px base",
        ],
    },
    "python": {
        "tech":  "Python script, stdlib only, no pip installs",
        "file":  "main.py",
        "rules": [
            "Python stdlib only — no pip installs",
            "Single script file",
            "Print clear progress and a friendly completion message",
            "Friendly, readable error messages for non-technical users",
        ],
    },
}

def pick_stack(intent: str) -> str:
    if intent == "chart":  return "html_chart"
    if intent == "script": return "python"
    return "html"

def slugify(text: str) -> str:
    stop = {"a","an","the","to","for","my","i","want","build","make","create",
            "need","help","just","me","us","always","forget","like","would",
            "could","app","tool","something","can","get","have","some"}
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    clean = [w for w in words if w not in stop][:3]
    return "-".join(clean) or "my-app"

def todo_tasks(intent: str, stack: str, desc: str, plan: dict = None) -> list:
    if stack == "python":
        return [
            "Create main.py with argparse CLI, --help text, and usage examples",
            f"Implement core logic: {desc[:70]}",
            "Add error handling with clear, friendly messages",
            "Add progress output and a clear completion message",
        ]
    title = (plan or {}).get("title", "the app")
    tasks = [
        f"Create index.html skeleton — doctype, viewport, <title>{title}</title>, "
        "empty <style>, body with <h1> for app name, <main id=\"app-main\">, closing </body></html>. "
        "Under 80 lines. No JavaScript yet.",
        "Add complete CSS in the <style> block — mobile-first, 44px touch targets, forms and lists. "
        "Keep all HTML structure; end with </html>.",
        "Add <script> with localStorage helpers and data model objects for this app's entities",
        "Wire up the UI — forms submit, list renders, mark-done and delete all work with localStorage",
        "Add empty state, friendly copy, and polish — match acceptance criteria from BRAIN.md",
        "VERIFY: index.html must be complete (</html> present), all features work, "
        "no 'Project Brain' in visible UI, title and h1 match App name",
    ]
    if intent == "chart":
        tasks.insert(3, "Load Chart.js from cdnjs CDN and wire up the chart display")
    return tasks

def make_brain_files(app_name, desc, intent, stack_key, scope, extra):
    stack     = STACKS[stack_key]
    scope_note = ("Shared family app." if scope == "shared" else "Personal app.")
    plan      = extra.get("plan") or {}
    features  = extra.get("features", "").strip()
    app_title = plan.get("title") or extra.get("app_title") or "My App"
    acceptance = plan.get("acceptance") or []

    brain = f"""# Project Brain

## App name (user-facing — use for <title> and <h1>, NEVER "Project Brain")
{app_title}

## What this project is
{plan.get('summary') or desc}. {scope_note}

## Architecture
{stack['tech']}.
Main file: {stack['file']}

## Current State
Not started.
"""
    feats = plan.get("features") or []
    if isinstance(feats, str):
        feats = [feats]
    if feats or features:
        brain += "\n## Requested Features\n"
        for f in (feats if feats else [features]):
            if f:
                brain += f"- {f}\n" if not str(f).startswith("-") else f"{f}\n"

    if acceptance:
        brain += "\n## Acceptance criteria (must all work when done)\n"
        for a in acceptance:
            brain += f"- {a}\n" if not str(a).startswith("-") else f"{a}\n"

    brain += f"\n## Key Constraints\n"
    brain += "\n".join(f"- {r}" for r in stack["rules"][:3]) + "\n"

    const  = "# Constitution\n# These rules apply to every single chunk.\n\n"
    const += "\n".join(f"- {r}" for r in stack["rules"]) + "\n"

    tasks  = todo_tasks(intent, stack_key, desc, plan)
    todo   = "## NOW\n[Tortoise moves the active task here]\n\n## NEXT\n"
    todo  += "\n".join(f"- [ ] {t}" for t in tasks)
    todo  += "\n\n## LATER\n\n## BLOCKED\n\n## DONE\n"

    return brain, const, todo

# ── Agent (planning chat via local model) ──────────────────────────────────────

AGENT_SYSTEM = """You are Workshop, a friendly assistant that helps people design simple personal web apps before any code is written.

Your job:
1. Understand what problem they want to solve (ask clarifying questions if needed).
2. Suggest a concrete, simple app design (not over-engineered).
3. When you have enough detail, propose a build plan for the user to confirm.

Rules:
- Keep replies short (2-4 sentences) unless explaining a plan.
- One question at a time when clarifying.
- Recommend simple single-page apps (HTML + localStorage) unless they need a script.
- Do NOT say you are building yet — the user must confirm the plan first.

When ready to propose a build, put a JSON block at the very END of your message (after your friendly summary).
The user never sees this JSON — it is parsed automatically. Use a markdown code fence:

```json
{"ready": true, "plan": {"title": "Short App Name", "summary": "one sentence", "scope": "personal", "features": ["feature 1", "feature 2"], "acceptance": ["user can ...", "data persists after refresh"]}}
```

Use scope "shared" only if they want family/household sharing.
Until ready, omit the JSON block entirely. Never output raw JSON outside the fence.
"""

AGENT_UPDATE_SYSTEM = """You are Workshop, helping improve an EXISTING app the user already built.

The app context (name, description, files) is provided below. Do NOT suggest rebuilding from scratch unless they ask.

Your job:
1. Ask what bugs they noticed or what they want added/changed.
2. Propose a focused UPDATE plan — specific fixes and features only.
3. When ready, output the plan JSON for them to confirm before any code runs.

Rules:
- Keep replies short (2-4 sentences) unless explaining the plan.
- One question at a time when clarifying.
- Preserve existing behavior and localStorage data unless they want a reset.
- Do NOT say you are building yet — they must confirm first.

EXISTING APP:
{app_context}

When ready, end with:

```json
{{"ready": true, "plan": {{"title": "App Name", "summary": "what we're changing", "scope": "personal", "changes": ["fix X", "add Y"], "acceptance": ["user can ..."], "update_mode": true}}}}
```

Use "changes" for bugs + new features. Omit JSON until ready.
"""

def call_llm(cfg: dict, messages: list, max_tokens: int = 1200, system: str = None) -> str:
    url = cfg["endpoint"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system or AGENT_SYSTEM}] + messages,
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def _extract_json_plan(text: str):
    """Pull plan JSON out of model text; return (plan, ready, stripped_text)."""
    plan, ready = None, False
    display = text

    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            data = json.loads(block)
            if data.get("ready") and data.get("plan"):
                return data["plan"], True, text[: text.find("```")].strip()
            if "ready" in data:
                display = text[: text.find("```")].strip()
        except json.JSONDecodeError:
            pass

    # Brace-match from last "ready" or "plan" key (handles nested plan object)
    for anchor in ('"ready"', '"plan"'):
        pos = text.rfind(anchor)
        if pos == -1:
            continue
        start = text.rfind("{", 0, pos)
        if start == -1:
            continue
        depth, end = 0, -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            try:
                data = json.loads(text[start:end])
                if data.get("ready") and data.get("plan"):
                    return data["plan"], True, (text[:start] + text[end:]).strip()
                if "ready" in data:
                    display = (text[:start] + text[end:]).strip()
            except json.JSONDecodeError:
                pass

    return plan, ready, display

def parse_agent_response(text: str) -> dict:
    """Extract visible reply and optional plan JSON from model output."""
    plan, ready, display = _extract_json_plan(text)
    # Strip any remaining fenced JSON or trailing raw objects
    display = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", display, flags=re.S).strip()
    display = re.sub(r"^\s*\{[^{}]*\"ready\"[^{}]*\}\s*$", "", display, flags=re.M).strip()
    if not display:
        display = "Here's what I suggest:"
    if plan and plan.get("changes") and not plan.get("features"):
        plan["features"] = plan["changes"]
    return {"reply": display, "ready": ready, "plan": plan}

def _brain_field(brain: str, heading_prefix: str) -> str:
    """Read first line of a ## section whose heading contains heading_prefix."""
    in_sec = False
    for line in brain.split("\n"):
        if line.startswith("## "):
            in_sec = heading_prefix.lower() in line[3:].lower()
            continue
        if in_sec:
            if line.startswith("## "):
                break
            if line.strip() and not line.startswith("-"):
                return line.strip()[:200]
    return ""

def app_context(app_dir: Path) -> dict:
    """Context for agent chat when editing an existing app."""
    td = app_dir / ".tortoise"
    brain = (td / "BRAIN.md").read_text(encoding="utf-8", errors="replace") if (td / "BRAIN.md").exists() else ""
    title = _brain_field(brain, "app name")
    if not title:
        title = app_dir.name.replace("-", " ").title()
    summary = _brain_field(brain, "What this project is")
    files = [str(f.relative_to(app_dir)) for f in sorted(app_dir.rglob("*"))
             if f.is_file() and ".tortoise" not in f.parts]
    html_snip = ""
    hp = app_dir / "index.html"
    if hp.exists():
        html_snip = hp.read_text(encoding="utf-8", errors="replace")[:4000]
    return {
        "name": app_dir.name,
        "title": title,
        "summary": summary,
        "files": files,
        "html_snippet": html_snip,
        "brain_excerpt": brain[:2500],
    }

def append_brain_update(app_path: Path, plan: dict):
    bp = app_path / ".tortoise" / "BRAIN.md"
    if not bp.exists():
        return
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    txt = bp.read_text(encoding="utf-8")
    txt += f"\n\n## Update ({ts})\n{plan.get('summary', '')}\n"
    for c in (plan.get("changes") or plan.get("features") or []):
        txt += f"- {c}\n"
    bp.write_text(txt)

def queue_update_tasks(app_path: Path, plan: dict):
    """Add Tortoise chunk tasks for an app update."""
    tp = app_path / ".tortoise" / "TODO.md"
    if not tp.exists():
        return
    changes = plan.get("changes") or plan.get("features") or []
    summary = (plan.get("summary") or "Apply requested updates").strip()
    tasks = [f"Implement update: {summary[:100]}"]
    for c in changes[:8]:
        c = str(c).strip()
        if c:
            tasks.append(f"Add or fix: {c[:95]}")
    accept = plan.get("acceptance") or []
    if accept:
        tasks.append(
            "VERIFY: " + "; ".join(str(a) for a in accept[:4])[:200]
            + " — preserve localStorage data, complete index.html, no regressions"
        )
    else:
        tasks.append(
            "VERIFY: all requested changes work, localStorage preserved, "
            "index.html complete with closing tags and working script"
        )
    text = tp.read_text(encoding="utf-8")
    block = "".join(f"- [ ] {t}\n" for t in tasks)
    if block.strip() not in text:
        text = text.replace("## NEXT\n", f"## NEXT\n{block}", 1)
        tp.write_text(text)

def verify_app_html(app_path: Path) -> list:
    """Run tortoise HTML + JS validation on built app."""
    t = load_tortoise()
    html = app_path / "index.html"
    if not t or not html.exists():
        return []
    content = html.read_text(encoding="utf-8", errors="replace")
    issues = []
    validate_html = getattr(t, "validate_html_content", None)
    if validate_html:
        issues.extend(msg for level, msg in validate_html(content, "VERIFY")
                      if level == "severe")
    validate_js = getattr(t, "validate_js_content", None)
    if validate_js:
        issues.extend(msg for level, msg in validate_js(content, "VERIFY")
                      if level == "severe")
    return issues

def _brain_acceptance(brain: str) -> list:
    in_sec, items = False, []
    for line in brain.split("\n"):
        if re.match(r"##\s+Acceptance", line, re.I):
            in_sec = True
            continue
        if line.startswith("## ") and in_sec:
            break
        if in_sec and line.strip().startswith("- "):
            items.append(line.strip()[2:].strip())
    return items

SELF_REVIEW_SYSTEM = """You review finished single-page web apps against acceptance criteria.
List concrete bugs or missing features only. If everything works, respond with exactly NONE.
Do not suggest refactors or style tweaks unless they break functionality."""

def self_review_app(app_path: Path, cfg: dict) -> list:
    """One model pass over the finished app; returns issue strings or []."""
    html = app_path / "index.html"
    brain_path = app_path / ".tortoise" / "BRAIN.md"
    if not html.exists() or not cfg.get("endpoint") or not cfg.get("model"):
        return []
    brain = (brain_path.read_text(encoding="utf-8", errors="replace")
             if brain_path.exists() else "")
    acceptance = _brain_acceptance(brain)
    if not acceptance:
        summary = _brain_field(brain, "What this project is") or "App works as described in BRAIN"
        acceptance = [summary, "All interactive features work; data persists after refresh"]
    content = html.read_text(encoding="utf-8", errors="replace")
    crit = "\n".join(f"- {a}" for a in acceptance[:12])
    prompt = (
        f"ACCEPTANCE CRITERIA (must all pass):\n{crit}\n\n"
        f"PROJECT BRAIN (excerpt):\n{brain[:2500]}\n\n"
        f"COMPLETE index.html:\n{content[:12000]}\n\n"
        "List bugs or missing features. One per line after ISSUES: "
        "If everything works, respond with exactly: NONE"
    )
    try:
        resp = call_llm(cfg, [{"role": "user", "content": prompt}],
                        max_tokens=600, system=SELF_REVIEW_SYSTEM)
    except Exception:
        return []
    text = resp.strip()
    if re.match(r"^NONE\b", text, re.I) and "ISSUES:" not in text.upper():
        return []
    issues = []
    in_issues = False
    for line in text.split("\n"):
        if re.match(r"^ISSUES:\s*", line, re.I):
            in_issues = True
            rest = re.sub(r"^ISSUES:\s*", "", line, flags=re.I).strip()
            if rest and rest.upper() != "NONE":
                issues.append(rest.lstrip("- ").strip())
            continue
        if in_issues:
            t = line.strip().lstrip("- ").strip()
            if t and t.upper() != "NONE":
                issues.append(t)
    if not issues and text and text.upper() != "NONE":
        for line in text.split("\n"):
            t = line.strip().lstrip("- ").strip()
            if t and len(t) > 8 and t.upper() != "NONE":
                issues.append(t)
    return issues[:6]

def inject_review_tasks(app_path: Path, issues: list):
    tp = app_path / ".tortoise" / "TODO.md"
    if not tp.exists() or not issues:
        return
    block = "".join(
        f"- [ ] Fix index.html — {i[:95]}\n" for i in issues[:5]
    )
    text = tp.read_text()
    if "Fix index.html —" not in text:
        text = text.replace("## NEXT\n", f"## NEXT\n{block}", 1)
        tp.write_text(text)

def inject_verify_task(app_path: Path, issues: list):
    tp = app_path / ".tortoise" / "TODO.md"
    if not tp.exists():
        return
    task = ("VERIFY FIX: " + "; ".join(issues[:3])
            + " — complete index.html with working script, all tags closed, "
            "match acceptance criteria, no 'Project Brain' in UI")
    text = tp.read_text()
    if task not in text:
        text = text.replace("## NEXT\n", f"## NEXT\n- [ ] {task}\n", 1)
        tp.write_text(text)

def replace_now_task(app_path: Path, task: str):
    """Replace the active NOW task so a failed chunk does not loop forever."""
    tp = app_path / ".tortoise" / "TODO.md"
    if not tp.exists():
        return
    text = tp.read_text()
    if task in text:
        return
    text = re.sub(
        r"## NOW\n.*?(?=\n## )",
        f"## NOW\n- [ ] {task}\n[Tortoise moves the active task here]\n",
        text,
        count=1,
        flags=re.S,
    )
    tp.write_text(text)

# ── Build runner ───────────────────────────────────────────────────────────────

_sse_queues: dict = {}
_build_lock  = threading.Lock()
_build_state = {
    "status":   "idle",   # idle | starting | building | done | error
    "app_name": None,
    "app_path": None,
    "chunk":    0,
}
_ANSI = re.compile(r"\033\[[0-9;]*m")

def _clean(line: str) -> str:
    return _ANSI.sub("", line).strip()

def _translate(line: str):
    l = _clean(line)
    if not l: return None
    if re.search(r"Calling .+ at ", l):
        return {"t": "thinking", "msg": "Thinking..."}
    if "Written:" in l:
        f = l.split("Written:")[-1].strip()
        return {"t": "ok", "msg": f"Wrote {Path(f).name}"}
    m = re.search(r"Chunk \d+.*?:\s*(.+)", l)
    if m and "complete" in l.lower():
        return {"t": "step", "msg": f"✓ {m.group(1).strip()}"}
    if "Queue empty" in l or "No tasks" in l:
        return {"t": "done", "msg": "Build complete!"}
    # Real Tortoise errors use ✗ — not diff lines like "+ --error-color: …"
    stripped = l.lstrip()
    if stripped.startswith("✗"):
        return {"t": "error", "msg": stripped}
    if re.search(r"cannot reach|Connection refused|timed out|URLError", l, re.I):
        return {"t": "error", "msg": l}
    if re.match(r"Error:", stripped, re.I):
        return {"t": "error", "msg": stripped}
    return None

def _emit(session: str, obj: dict):
    q = _sse_queues.get(session)
    if q:
        q.put(json.dumps(obj))

def _write_tortoise_config(app_path: Path, cfg: dict):
    t_cfg = {
        "endpoint":      cfg.get("endpoint", "http://localhost:11434/v1"),
        "model":         cfg.get("model",    "phi3"),
        "confirm_writes": False,
        "backup":         True,
        "max_tokens":     8192,
        "context_limit":  12000,
        "max_files":      3,
        "exclude":        ["node_modules",".git","venv","__pycache__",
                           ".tortoise","dist","build",".DS_Store"],
    }
    if cfg.get("api_key"):
        t_cfg["api_key"] = cfg["api_key"]
    (app_path / ".tortoise" / "config.json").write_text(json.dumps(t_cfg, indent=2))

def _sanitize_todo(app_path: Path):
    """Fix NOW tasks that are descriptions (old bug), not real build steps."""
    tp = app_path / ".tortoise" / "TODO.md"
    if not tp.exists():
        return
    t = load_tortoise()
    is_chunk = getattr(t, "_is_chunk_task", None) if t else None
    if not is_chunk:
        return
    lines = tp.read_text().split("\n")
    out, in_now, changed = [], False, False
    for line in lines:
        if line.strip() == "## NOW":
            in_now = True
            out.append(line)
            continue
        if line.startswith("## ") and in_now:
            in_now = False
        if in_now and line.strip().startswith("- [ ]"):
            task = line.strip()[5:].strip()
            if task and not is_chunk(task):
                changed = True
                continue  # drop junk from NOW
        out.append(line)
    if changed:
        tp.write_text("\n".join(out))

def _tortoise_run_cmd(app_path: Path, ts: str) -> list:
    """Use resume when a chunk was interrupted; otherwise run next task."""
    if (app_path / ".tortoise" / "CURRENT.md").exists():
        return [sys.executable, ts, "resume", "--yes"]
    return [sys.executable, ts, "run", "--yes"]

def _has_pending_tasks(app_path: Path, ts: str) -> bool:
    """True if Tortoise still has tasks in NOW or NEXT."""
    r = subprocess.run([sys.executable, ts, "status"],
                       cwd=str(app_path), capture_output=True, text=True)
    out = r.stdout + r.stderr
    if "No tasks queued" in out:
        return False
    return "Next up:" in out or "Active task" in out

def _run_build(app_path: str, cfg: dict, session: str):
    ap = Path(app_path)
    if not _build_lock.acquire(blocking=False):
        _emit(session, {"t": "error", "msg": "A build is already running — wait for it to finish."})
        return
    try:
        _build_state["status"] = "building"
        ts = tortoise_script()

        if not ts:
            _emit(session, {"t": "error",
                         "msg": TORTOISE_INSTALL_HINT})
            _build_state["status"] = "error"
            return

        _sanitize_todo(ap)
        _write_tortoise_config(ap, cfg)
        _emit(session, {"t": "status", "msg": "Starting build..."})

        verify_passes = 0
        review_passes = 0
        last_lines = []
        for iteration in range(30):  # safety cap
            chk = subprocess.run([sys.executable, ts, "status"],
                                 cwd=app_path, capture_output=True, text=True)
            if not _has_pending_tasks(ap, ts):
                issues = verify_app_html(ap)
                if issues and verify_passes < 2:
                    verify_passes += 1
                    _emit(session, {"t": "status", "msg": "Quality check found issues — fixing…"})
                    inject_verify_task(ap, issues)
                    continue
                if review_passes < 2:
                    _emit(session, {"t": "status", "msg": "Reviewing finished app…"})
                    review_issues = self_review_app(ap, cfg)
                    if review_issues:
                        review_passes += 1
                        _emit(session, {"t": "status",
                                         "msg": f"Review found {len(review_issues)} issue(s) — fixing…"})
                        inject_review_tasks(ap, review_issues)
                        continue
                _emit(session, {"t": "done", "msg": "Your app is ready!"})
                _build_state["status"] = "done"
                return

            cmd = _tortoise_run_cmd(ap, ts)
            if cmd[-2] == "resume":
                _emit(session, {"t": "status", "msg": "Resuming interrupted step…"})

            proc = subprocess.Popen(
                cmd,
                cwd=app_path,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            chunk_failed = False
            for raw in proc.stdout:
                line = _clean(raw)
                if line:
                    last_lines.append(line)
                    if len(last_lines) > 30:
                        last_lines.pop(0)
                msg = _translate(raw)
                if msg:
                    _emit(session, msg)
                elif line and any(line.startswith(p) for p in (
                    "Running Chunk", "Task:", "Plan:", "Changes", "Written:",
                    "Chunk ", "Calling ",
                )):
                    _emit(session, {"t": "status", "msg": line[:120]})
                if msg and msg["t"] == "error":
                    chunk_failed = True
                    if any(k in msg["msg"] for k in ("HTML check", "JS check", "Write blocked")):
                        _emit(session, {"t": "status",
                                         "msg": "Incomplete output — queuing a fix step…"})
                        replace_now_task(
                            ap,
                            "Fix index.html — output was incomplete. Close every open tag "
                            "(</form></main></body></html>), finish any half-written sections, "
                            "and keep all existing content that already works.",
                        )
                    else:
                        proc.wait()
                        detail = msg["msg"]
                        _emit(session, {"t": "error", "msg": detail})
                        _build_state["status"] = "error"
                        return
            rc = proc.wait()
            if chunk_failed:
                _build_state["chunk"] += 1
                continue
            if rc != 0:
                hint = next((l for l in reversed(last_lines)
                             if l.startswith("✗") or "Error" in l or "Cannot reach" in l),
                            last_lines[-1] if last_lines else "unknown error")
                _emit(session, {"t": "error",
                                 "msg": f"Build step failed: {hint[:200]}"})
                _build_state["status"] = "error"
                return
            _emit(session, {"t": "step",
                            "msg": f"✓ Step {_build_state.get('chunk', 0) + 1} complete"})
            _build_state["chunk"] += 1

        _emit(session, {"t": "done", "msg": "Build complete!"})
        _build_state["status"] = "done"
    finally:
        _build_lock.release()

# ── HTTP handler ───────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    # helpers
    def js(self, data, code=200):
        b = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def html_file(self, path: Path):
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # CORS preflight
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        p, qs  = parsed.path, parse_qs(parsed.query)

        # UI
        if p == "/":
            f = Path(__file__).parent / "workshop.html"
            if f.exists(): self.html_file(f)
            else:
                b = b"<h1>workshop.html not found next to workshop.py</h1>"
                self.send_response(200); self.send_header("Content-Type","text/html")
                self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b)
            return

        # Config / status
        if p == "/api/config":
            cfg  = load_config()
            safe = {k: v for k, v in cfg.items() if k != "api_key"}
            safe.update({
                "has_api_key":    bool(cfg.get("api_key")),
                "configured":     bool(cfg.get("endpoint") and cfg.get("model")),
                "tortoise_found": bool(tortoise_script()),
                "tortoise_ver":   tortoise_version(),
            })
            self.js(safe); return

        if p == "/api/detect":
            self.js({"models": detect_models()}); return

        if p == "/api/apps":
            user = qs.get("user", [None])[0]
            self.js({"apps": list_apps(load_config(), user)}); return

        if p == "/api/build/status":
            self.js(_build_state); return

        if p == "/api/build/stream":
            sid = qs.get("session", ["default"])[0]
            self._sse(sid); return

        # App file listing / serving
        if re.match(r"^/api/app/[^/]+/[^/]+/context$", p):
            self._api_app_context(p); return

        if re.match(r"^/api/app/[^/]+/[^/]+/files$", p):
            self._api_app_files(p); return

        if re.match(r"^/api/app/[^/]+/[^/]+/file$", p):
            self._api_app_file(p, qs); return

        if p.startswith("/apps/"):
            self._serve_app(p); return

        self.js({"error": "not found"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────────
    def do_POST(self):
        p = urlparse(self.path).path
        b = self.body()

        if p == "/api/setup":
            cfg = load_config()
            for k in ("endpoint", "model", "api_key", "apps_dir"):
                if k in b:
                    cfg[k] = b[k] or None
            save_config(cfg)
            self.js({"ok": True}); return

        if p == "/api/user":
            cfg  = load_config()
            name = b.get("name", "").strip()
            if name and name not in cfg.get("users", []):
                cfg.setdefault("users", []).append(name)
                save_config(cfg)
            if name:
                (apps_dir(cfg) / "users" / name).mkdir(parents=True, exist_ok=True)
            self.js({"ok": True, "user": name}); return

        if p == "/api/build/start":
            self._start_build(b); return

        if p == "/api/build/resume":
            self._resume_build(b); return

        if p == "/api/build/update":
            self._update_app(b); return

        if p == "/api/fix":
            self._fix_app(b); return

        if p == "/api/agent/chat":
            self._agent_chat(b); return

        self.js({"error": "not found"}, 404)

    # ── DELETE ─────────────────────────────────────────────────────────────────
    def do_DELETE(self):
        parts = urlparse(self.path).path.split("/")
        # /api/apps/{owner}/{name}
        if len(parts) >= 5 and parts[2] == "apps":
            owner, name = parts[3], parts[4]
            cfg  = load_config()
            base = apps_dir(cfg)
            ap   = (base / "shared" / name if owner == "shared"
                    else base / "users" / owner / name)
            if ap.exists():
                shutil.rmtree(ap); self.js({"ok": True})
            else:
                self.js({"error": "not found"}, 404)
        else:
            self.js({"error": "not found"}, 404)

    # ── SSE ────────────────────────────────────────────────────────────────────
    def _sse(self, sid: str):
        q = queue.Queue()
        _sse_queues[sid] = q
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                    if json.loads(msg).get("t") in ("done", "error"):
                        break
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _sse_queues.pop(sid, None)

    # ── agent ──────────────────────────────────────────────────────────────────
    def _agent_chat(self, b: dict):
        cfg = load_config()
        if not cfg.get("endpoint") or not cfg.get("model"):
            self.js({"error": "not configured"}, 400); return
        messages = b.get("messages", [])
        if not messages:
            self.js({"error": "messages required"}, 400); return
        user = b.get("user", "")
        app_ctx = b.get("app_context")
        try:
            if app_ctx:
                ctx_text = (
                    f"Name: {app_ctx.get('title') or app_ctx.get('name')}\n"
                    f"Folder: {app_ctx.get('name')}\n"
                    f"Description: {app_ctx.get('summary', '')}\n"
                    f"Files: {', '.join(app_ctx.get('files', [])[:12])}\n"
                )
                if app_ctx.get("html_snippet"):
                    ctx_text += f"\nindex.html (excerpt):\n{app_ctx['html_snippet'][:3500]}\n"
                system = AGENT_UPDATE_SYSTEM.format(app_context=ctx_text)
                raw = call_llm(cfg, messages, max_tokens=1400, system=system)
            else:
                extra = []
                if user:
                    extra.append({"role": "system", "content": f"The user's name is {user}."})
                raw = call_llm(cfg, messages + extra)
            out = parse_agent_response(raw)
            self.js({"ok": True, **out})
        except Exception as e:
            self.js({"error": str(e)}, 500)

    # ── build ──────────────────────────────────────────────────────────────────
    def _start_build(self, b: dict):
        cfg   = load_config()
        desc  = b.get("description", "").strip()
        user  = b.get("user",  "").strip()
        scope = b.get("scope", "personal")
        sid   = b.get("session", "default")
        extra = b.get("extra", {})
        plan  = b.get("plan") or extra.get("plan")
        if plan:
            extra["plan"] = plan
            if plan.get("summary"):
                desc = plan["summary"]
            if plan.get("scope"):
                scope = plan["scope"]

        if not desc:
            self.js({"error": "description required"}, 400); return
        if not cfg.get("endpoint") or not cfg.get("model"):
            self.js({"error": "not configured — complete setup first"}, 400); return

        # Classify
        intent, label = "general", "App"
        for pat, tag, lbl in INTENT_RULES:
            if re.search(pat, desc, re.I):
                intent, label = tag, lbl; break

        stack_key = pick_stack(intent)
        app_name  = slugify(desc)
        base      = apps_dir(cfg)
        ap        = (base / "shared" / app_name if scope == "shared"
                     else base / "users" / user / app_name)

        update_mode = (ap / ".tortoise").exists()

        if not update_mode:
            ap.mkdir(parents=True, exist_ok=True)
            td = ap / ".tortoise"
            for sub in ("", "chunks", "backups"):
                (td / sub).mkdir(exist_ok=True)
            brain, const, todo = make_brain_files(
                app_name, desc, intent, stack_key, scope, extra)
            (td / "BRAIN.md").write_text(brain)
            (td / "CONSTITUTION.md").write_text(const)
            (td / "TODO.md").write_text(todo)
            (td / "PROGRESS.md").write_text("# Progress\n")
            (td / "DECISIONS.md").write_text("# Decisions\n")
        self._launch_build(ap, app_name, cfg, sid, reset_chunk=not update_mode)
        self.js({
            "ok": True, "app_name": app_name, "app_path": str(ap),
            "stack": stack_key, "label": label, "update_mode": update_mode,
        })

    def _launch_build(self, ap: Path, app_name: str, cfg: dict, sid: str, reset_chunk=True):
        _build_state.update({
            "status": "starting", "app_name": app_name,
            "app_path": str(ap), "chunk": 0 if reset_chunk else _build_state.get("chunk", 0),
        })
        threading.Thread(
            target=_run_build, args=(str(ap), cfg, sid), daemon=True
        ).start()

    def _api_app_context(self, path: str):
        parts = path.split("/")
        owner, name = parts[3], parts[4]
        cfg  = load_config()
        base = (apps_dir(cfg) / "shared" / name if owner == "shared"
                else apps_dir(cfg) / "users" / owner / name)
        if not base.exists():
            self.js({"error": "not found"}, 404); return
        ctx = app_context(base)
        meta = app_meta(base, owner, "shared" if owner == "shared" else "personal")
        ctx.update({"path": str(base), "owner": owner, "scope": meta["scope"], "url": meta.get("url")})
        self.js({"ok": True, "context": ctx})

    def _update_app(self, b: dict):
        cfg  = load_config()
        ap   = Path(b.get("app_path", ""))
        plan = b.get("plan") or {}
        sid  = b.get("session", "default")
        if not ap.exists() or not (ap / ".tortoise").exists():
            self.js({"error": "app not found"}, 404); return
        if not cfg.get("endpoint") or not cfg.get("model"):
            self.js({"error": "not configured"}, 400); return
        if _build_state.get("status") == "building":
            self.js({"error": "build already running"}, 409); return
        append_brain_update(ap, plan)
        queue_update_tasks(ap, plan)
        current = ap / ".tortoise" / "CURRENT.md"
        if current.exists():
            current.unlink()
        _sanitize_todo(ap)
        owner = "shared" if "shared" in ap.parts else ap.parent.name
        self._launch_build(ap, ap.name, cfg, sid, reset_chunk=False)
        self.js({
            "ok": True, "app_name": ap.name, "app_path": str(ap),
            "owner": owner, "update_mode": True,
        })

    def _resume_build(self, b: dict):
        cfg = load_config()
        ap  = Path(b.get("app_path", ""))
        sid = b.get("session", "default")
        if not ap.exists() or not (ap / ".tortoise").exists():
            self.js({"error": "app not found"}, 404); return
        if not cfg.get("endpoint") or not cfg.get("model"):
            self.js({"error": "not configured"}, 400); return
        if _build_state.get("status") == "building":
            self.js({"error": "build already running"}, 409); return
        _sanitize_todo(ap)
        self._launch_build(ap, ap.name, cfg, sid)
        self.js({"ok": True, "app_path": str(ap), "app_name": ap.name})

    def _fix_app(self, b: dict):
        cfg = load_config()
        ap  = Path(b.get("app_path", ""))
        sid = b.get("session", "default")
        if not ap.exists():
            self.js({"error": "app not found"}, 404); return
        tp = ap / ".tortoise" / "TODO.md"
        if tp.exists():
            text = tp.read_text()
            task = "- [ ] Review all functionality and fix any broken features so the app works correctly end to end"
            text = text.replace("## NEXT\n", f"## NEXT\n{task}\n", 1)
            tp.write_text(text)
        self._launch_build(ap, ap.name, cfg, sid)
        self.js({"ok": True, "app_path": str(ap)})

    # ── static app serving ─────────────────────────────────────────────────────
    def _serve_app(self, path: str):
        parts = path.lstrip("/").split("/")  # apps / owner / name / ...
        if len(parts) < 3:
            self.js({"error": "bad path"}, 400); return
        owner, name = parts[1], parts[2]
        tail  = "/".join(parts[3:]) or "index.html"
        cfg   = load_config()
        base  = (apps_dir(cfg) / "shared" / name if owner == "shared"
                 else apps_dir(cfg) / "users" / owner / name)
        full  = base / tail
        if not full.exists() or not full.is_file():
            self.js({"error": "not found"}, 404); return
        TYPES = {".html":"text/html",".css":"text/css",
                 ".js":"application/javascript",".json":"application/json",
                 ".png":"image/png",".jpg":"image/jpeg",".svg":"image/svg+xml"}
        ct = TYPES.get(full.suffix.lower(), "application/octet-stream")
        b  = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",   ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _api_app_files(self, path: str):
        parts = path.split("/")  # /api/app/owner/name/files
        owner, name = parts[3], parts[4]
        cfg  = load_config()
        base = (apps_dir(cfg) / "shared" / name if owner == "shared"
                else apps_dir(cfg) / "users" / owner / name)
        files = [str(f.relative_to(base))
                 for f in sorted(base.rglob("*"))
                 if f.is_file() and ".tortoise" not in f.parts]
        self.js({"files": files})

    def _api_app_file(self, path: str, qs):
        parts = path.split("/")  # /api/app/owner/name/file
        owner, name = parts[3], parts[4]
        fp   = qs.get("path", [None])[0]
        if not fp:
            self.js({"error": "path required"}, 400); return
        cfg  = load_config()
        base = (apps_dir(cfg) / "shared" / name if owner == "shared"
                else apps_dir(cfg) / "users" / owner / name)
        full = base / fp
        if full.exists() and full.is_file():
            self.js({"content": full.read_text(errors="replace"), "path": fp})
        else:
            self.js({"error": "not found"}, 404)

# ── Entry ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="workshop")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Use 0.0.0.0 to expose on local network (Pi)")
    args = ap.parse_args()

    cfg = load_config()
    apps_dir(cfg).mkdir(parents=True, exist_ok=True)

    tv     = tortoise_version()
    ts_ok  = bool(tortoise_script())
    print(f"\n  Workshop {VERSION}")
    ts_path = tortoise_script()
    if ts_ok:
        print(f"  tortoise : v{tv} ✓  ({ts_path})")
    else:
        print("  tortoise : ✗ NOT FOUND")
        for line in TORTOISE_INSTALL_HINT.splitlines():
            print(f"    {line}")
    print(f"  apps dir : {apps_dir(cfg)}")
    print(f"  config   : {CONFIG_FILE}")
    if not cfg.get("endpoint"):
        print(f"\n  ⚠  Not configured yet — open the browser and complete setup.")
    print(f"\n  Open: http://localhost:{args.port}\n")

    server = ThreadedHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")

if __name__ == "__main__":
    main()
