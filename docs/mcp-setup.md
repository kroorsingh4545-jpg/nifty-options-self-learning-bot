# MCP Server Setup

MCP servers give Claude Code extra tools. They are configured in `.mcp.json` at the project root. Claude Code asks you to approve project servers the first time it sees them.

**Always start Claude Code from the project root** (`nifty-options-self-learning-bot/`). Paths in `.mcp.json` such as `.` and `./data/...` are relative to that folder.

## Summary

| Server | Purpose | Data access | Can write? |
|---|---|---|---|
| `filesystem` | Read/search project files | This project folder only | Yes, but each write asks you first |
| `playwright` | Test the Streamlit dashboard in a browser | Web pages it opens (e.g. `localhost:8501`) | No files |
| `github` | Branches, commits, PRs, issues, code reviews | Repos your token allows | Yes, risky actions ask first |
| `sqlite` | Inspect trading / learning databases | `data/trading.db` only | **No** — writes blocked |
| `sqlite-dev` | Dev/test database work | `data/dev/dev.db` only | Yes, each write asks first |

Permission rules live in `.claude/settings.json`.

## 1. filesystem

- **Package:** `@modelcontextprotocol/server-filesystem`
- **Purpose:** Read, list, and search files inside the project.
- **Scope:** Only the folder passed as its argument: `.` (the project root). It cannot see your home folder or anything outside the project.
- **Restrictions:** Write, edit, and move tools require your approval each time. Reading `.env` is denied.

## 2. playwright

- **Package:** `@playwright/mcp@latest` (run with `npx -y`)
- **Purpose:** Open the Streamlit dashboard in a browser and test it (PAPER mode).
- **Scope:** Only pages it is told to open. Use it on `http://localhost:8501`.
- **Restrictions:** Use only in PAPER mode. Never use it to log in to Upstox or any broker website.

## 3. github

- **Server:** GitHub's official remote MCP server (`https://api.githubcopilot.com/mcp/`).
- **Purpose:** Branches, commits, pull requests, issues, code reviews.
- **Token:** A **fine-grained personal access token**, limited to this one repository, with only:
  - Contents: Read and write
  - Pull requests: Read and write
  - Issues: Read and write
  - Metadata: Read
- **Where the token lives:** In your shell environment variable `GITHUB_PERSONAL_ACCESS_TOKEN`. `.mcp.json` only contains the placeholder `${GITHUB_PERSONAL_ACCESS_TOKEN}` — never the real token.
- **Restrictions:** Pushing files, merging PRs, and deleting files ask for approval.

### Setting the token (do this yourself — never paste it into chat)

Create the token at GitHub → Settings → Developer settings → Fine-grained tokens. Then, in the terminal you start Claude Code from:

```bash
export GITHUB_PERSONAL_ACCESS_TOKEN=paste_your_token_here
```

(Windows PowerShell: `$env:GITHUB_PERSONAL_ACCESS_TOKEN="paste_your_token_here"`)

Note: Claude Code reads this from your shell, not from the project `.env` file.

## 4. sqlite (read-only) and sqlite-dev

- **Package:** `mcp-server-sqlite` (run with `uvx`, which comes with `uv`).
- **Purpose:** Inspect trade, order, position, feature, model, and learning-loop databases.
- **`sqlite`:** Points at `data/trading.db`. Its `write_query` and `create_table` tools are **denied** in `.claude/settings.json`, so it is read-only for Claude.
- **`sqlite-dev`:** Points at `data/dev/dev.db`, for development/test only. Writes are allowed but ask first.
- **Restrictions:** Never point `sqlite-dev` at a PAPER or LIVE trading database. All `*.db` files are git-ignored.

## How to disable or remove a server

- **Turn off for now:** inside Claude Code, type `/mcp`, pick the server, choose disable.
- **Remove permanently:** delete its block from `.mcp.json`, save, and restart Claude Code.
- **See what's active:** run `claude mcp list` in the project folder.
- **Reset your approval choices:** `claude mcp reset-project-choices`.

## Why Upstox order placement is NOT exposed to Claude

- Claude is a development assistant, not a trader (see `docs/safety-policy.md`).
- An order tool in Claude's hands could send real orders from a misunderstanding, a bad prompt, or text hidden in a web page or file (prompt injection).
- Real orders must pass the bot's risk checks, rate limiter, kill switch, reconciliation, and PAPER/LIVE guards. Those live only in `execution/` and `risk/`. A direct MCP order tool would skip all of them.
- Therefore: **no Upstox MCP server and no order-placing MCP tool will be added to this project.**
