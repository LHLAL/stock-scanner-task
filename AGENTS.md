# AGENTS.md — Rules for AI agents working on this project

## 🚨 Security Boundary: config.json 🚨

**NEVER include the contents of `config.json` in any context sent to an LLM.**

This file may contain:
- `api_key` (Ollama Cloud, Anthropic)
- `sign`, `cookie` (CLS /api/csw auth tokens)
- Other secret values

The contents must not be:
- Pasted into prompts
- Quoted in chat
- Read into memory and paraphrased
- Logged to a transcript that will be sent elsewhere

**Why:** Even though `config.json` is now git-ignored (and is untracked
since commit `7c469c2`), the file still exists on the user's local disk with
real secrets. Leaking it via an LLM context is the same security failure as
committing it to git.

## Project overview

- macOS menu-bar app for A股 monitoring + digest news analysis
- Stack: rumps / Ollama / SQLite
- Sensitive values belong in `config.json` (local) and env vars
- `config.example.json` is the safe template to share

## When asked about config

If the user asks "what's in your config?" or "show me config.json":
- Reference the structure (key names, types) from `config.example.json`
- DO NOT cat / read / quote values from `config.json`
- If a specific value is needed (e.g. for a debug check), ask the user to
  paste the relevant line themselves

## When debugging

If a bug requires seeing a config value:
- Suggest the user run the relevant command themselves and share the output
- OR add a temporary debug log in the app that prints the value at runtime
  (not stored, not committed)

## Code reviews

When reviewing or refactoring, never propose code that:
- Reads config.json into a string and pipes it to an LLM
- Logs the full config to a file that's then attached to a chat
- Echoes the api_key in a stack trace
