# TracePatch-CLI — rules for Claude Code

- Verified OOM repair for pandas scripts: Gate 1 trace -> Gate 2 AST allowlist -> rewrite -> Gate 3 sandbox+oracle -> full-scale re-trace (max 3 attempts).
- Small modules, plain code, short "why" comments. Authors must be able to explain every file.
- Run `python3 -m pytest` before claiming anything works.
- Never widen `CHUNK_SAFE_METHODS`/`SAFE_BUILTINS`, loosen oracle tolerance, or raise the retry cap to make a test pass — ask first.
- Never commit secrets, `.env`, or data files. API key only from `ANTHROPIC_API_KEY`.
- Rewritten code runs only inside the Gate 3 sandbox.
- Python 3.9 compatible; cross-platform (no Unix-only `resource`).
- Gate boundaries use the dict shapes in CONTRACT.md.
