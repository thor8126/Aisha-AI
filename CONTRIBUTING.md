# Contributing to Aisha AI

Thank you for your interest in contributing! Whether it's a bug fix, a new automation tool, a UI improvement, avatar art, or better docs — all contributions are welcome.

---

## How to Contribute

1. **Fork** the repository
2. **Clone your fork** and create a branch:
   ```bash
   git clone https://github.com/YOUR_USERNAME/Aisha-AI.git
   cd Aisha-AI
   git checkout -b feature/your-feature-name
   ```
3. **Make your changes** — follow the code style below
4. **Test** — run `python doctor.py` and make sure all checks pass
5. **Commit** with a clear message:
   ```bash
   git commit -m "Add: brief description of your change"
   ```
6. **Push and open a Pull Request**:
   ```bash
   git push origin feature/your-feature-name
   ```

---

## Code Style

- **Language**: Python 3.10+ with type hints where practical
- **Pattern**: Follow existing structure — tools in `assistant_tools.py`, GUI in `aisha_gui.py`, brain logic in `agent_brain.py`, orchestration in `aisha.py`
- **Docstrings**: Every public function/class gets a one-line docstring
- **Error handling**: Wrap external calls in try/except, return error dicts — never crash the agent loop
- **No secrets in code**: API keys stay in `.env` or Windows Credential Manager

---

## Adding a New Tool

The tool registry (`assistant_tools.py`) makes it easy to add capabilities:

1. Write a handler method on `ToolRegistry` that takes `(self, args: dict) -> dict`
2. Register it with a `ToolSpec(name, description, schema, handler)`
3. The schema uses `object_schema({field: {type, description, default}}, required_fields)`
4. Add a Hindi activity-feed label in `aisha.py`'s `_tool_step_label`

```python
ToolSpec(
    "my_new_tool",
    "One-line description of what it does and when to use it.",
    object_schema(
        {
            "param_name": {"type": "string", "description": "What this param controls"},
        },
        ["param_name"],  # required fields
    ),
    self._my_new_tool,
)
```

---

## Adding Avatar Sprites

Drop PNG files into `assets/avatar/`. The avatar engine picks them up automatically:

- `neutral.png` — required (default face, mouth closed)
- `neutral_open.png` — required for lip-sync (mouth open)
- `blink.png` — required for blinking (eyes closed)
- `happy.png`, `thinking.png`, `listening.png`, `sad.png`, `surprised.png` — expressions
- `<name>_open.png` — open-mouth variant for better lip-sync in that mood

**Consistency tip**: Generate one base portrait first, then use image-to-image with the same seed, changing only mouth/eyes. Keep the head in the same position/size across all files.

---

## Good First Issues

Looking for something to start with? These are great entry points:

- [ ] Add real anime sprite art for the avatar (`assets/avatar/`)
- [ ] Improve the procedural avatar face with more expressions and smoother animations
- [ ] Add more voice presets and emotion-to-voice mappings
- [ ] Write unit tests for `web_research` and screen vision
- [ ] Build a Windows installer (.exe) with auto-update support
- [ ] Add email tool (read inbox, draft replies)
- [ ] Better error messages when API keys are missing or invalid
- [ ] Keyboard shortcuts for common actions in the HUD
- [ ] Add Ollama support for fully offline local LLM mode

---

## Development Setup

```bash
# 1. Clone
git clone https://github.com/thor8126/Aisha-AI.git
cd Aisha-AI

# 2. Install deps (creates .venv)
install.bat

# 3. Configure keys
copy .env.example .env

# 4. Run
start.bat
```

### Running Tests

```bash
python -m pytest tests/ -q
```

### Health Check

```bash
python doctor.py
```

---

## Reporting Bugs

Open a [GitHub Issue](https://github.com/thor8126/Aisha-AI/issues) with:

1. What you were trying to do
2. What happened (error message, screenshot, log excerpt from `logs/aisha.log`)
3. Your setup (Windows version, Python version, which AI provider you're using)

---

## Questions?

Open a [GitHub Discussion](https://github.com/thor8126/Aisha-AI/discussions) or check existing issues before opening a new one.

---

<p align="center">
  Built with love for the Hindi-speaking developer community 💜
</p>
