# Contributing to MediaRefinery

Thank you for helping improve this project. This file describes the **practical** contribution steps and **GitHub** usage.

## Quick start

1. Read [SECURITY.md](SECURITY.md) and the documents under [docs/v2/](docs/v2/).
2. For issues: use a template under [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE) (see [templates/](templates/) for the same structure in a portable form).
3. For pull requests: use [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).
4. For security issues: do **not** file a public issue—see [SECURITY.md](SECURITY.md).

## Code of conduct

We follow the [Code of Conduct](CODE_OF_CONDUCT.md). Be respectful and constructive.

## Label catalog

When creating or triaging issues, use **labels** so parallel work and routing stay clear.

### Type (prefix `type:`)

| Label | When to use |
|-------|-------------|
| `type:feature` | New user-visible or API capability |
| `type:bug` | Regressions or incorrect behavior |
| `type:chore` | Tooling, refactors, maintenance |
| `type:docs` | Documentation only |
| `type:security` | Security-related change or finding (non-sensitive discussion) |
| `type:question` | Support / clarification |

### Priority (optional, prefix `priority:`)

| Label | When to use |
|-------|-------------|
| `priority:p0` | Blocker / incident |
| `priority:p1` | Next release or sprint |
| `priority:p2` | Normal |
| `priority:p3` | Backlog / nice-to-have |

### Area (prefix `area:`)

| Label | When to use |
|-------|-------------|
| `area:immich` | Immich client / API adapter |
| `area:scanner` | Asset scan selection |
| `area:extractor` | Thumbnails, video frames, ffmpeg path |
| `area:classifier` | Pluggable classifiers, backends, mapping |
| `area:config` | Config schema, validation, CLI for config |
| `area:state` | SQLite / persistence, migrations |
| `area:decision` | Decision engine, policies, thresholds |
| `area:reporting` | Reports, dry-run output |
| `area:docker` | Containers, compose, CI images |
| `area:presets` | Preset docs, example YAML, taxonomy |
| `area:docs` | General documentation |

### Agent role (prefix `agent:`)

Mirror `agents/*.md` ownership for parallel agent/human work:

- `agent:coordinator`, `agent:immich`, `agent:classifier`, `agent:extractor`, `agent:config-cli`, `agent:state`, `agent:docker`, `agent:test-qa`, `agent:security-privacy`, `agent:docs`, `agent:release`, `agent:presets-taxonomy`

(Exact set may grow—see the `agents/` directory.)

### Preset (optional, prefix `preset:`)

Use when the issue or PR is **only** about a named **preset** (e.g. sensitive review):

- `preset:none` / omit when not preset-specific
- `preset:sensitive-content-review` (or your project’s convention)

### Phase / milestone (optional)

- `milestone:blueprint`, `sprint:001`, etc. aligned with [planning/](planning/).

## Branches and commits

- **Branch naming:** `feat/...`, `fix/...`, `docs/...`, or the **suggested branch** in the active `agents/<role>.md` file.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) are encouraged: `type(scope): description`.
- **One logical change** per PR when possible.

## Reviews

- Request review when the PR is ready; address feedback or explain tradeoffs.
- **Do not** commit secrets, API keys, or real end-user media into the repository.

## Planning artifacts

- **Task board (Markdown):** [planning/task-board.md](planning/task-board.md) — keep columns in sync with reality when you use that workflow.
- **Progress log:** [planning/progress-log.md](planning/progress-log.md) — add an entry for substantive work.
- **Handoffs:** [planning/handoff-template.md](planning/handoff-template.md) — for agent-to-agent work.

## Templates

- **Canonical** detailed bodies for some templates may also live in [templates/](templates/). GitHub **forms** in `.github/ISSUE_TEMPLATE` may point here to avoid **drift**; if you change one, update the other or use a single source with a link.

## Questions

- Open a `type:question` issue or a **Discussion** (if enabled on the host) for high-level design—still avoid secrets and sensitive media in public posts.
