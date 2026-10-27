# kindleone

ePub delivery to a jailbroken Kindle running KOReader. `kindle_mcp/server.py` is a
zero-dependency stdio MCP server; `README.md` is the authoritative device + install
reference. Secrets live in `.env` (never committed); `.env.example` documents its shape.

No format conversion anywhere — KOReader reads ePub natively.

## Agent skills

### Issue tracker

GitHub Issues on `bluehawana/kindle-koreader-mcp-dropbox`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
