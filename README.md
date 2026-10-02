# Discovery CatalogProbe

Automated **functional** testing for Microsoft Discovery agents and starter kits. The CLI is `dft`.

Sister tool: [Discovery SciProbe](https://github.com/faranm-stack/discovery-sciprobe) checks that answers are
scientifically right; CatalogProbe checks that agents and kits work.

The repo's existing checks (#132: schema, YAML, policy) prove an agent *loads*. They
don't prove Discovery can *use* it. `dft` answers the second question:

- Does the agent's prompt tell the LLM to call functions/actions that don't exist or have a
  different signature? *(offline, no Azure)*
- Deployed, does it answer, or say "I don't know how to do this"?
- Did it actually run its tool, or just make up an answer?
- Did the tool run cleanly, and does the answer contain what the kit promises?
- What changed since the last catalog drop or service release?

## Quick start

```bash
pip install -e ".[dev]"
git clone --depth 1 https://github.com/microsoft/discovery catalog   # GIT_LFS_SKIP_SMUDGE=1 to skip weights

dft static --catalog catalog --out out                  # offline F0 checks, all 91 agents + 4 kits
dft plan   --catalog catalog --kits all --out out/plan.json
dft diff   baselines/static.json out/static-results.json
```

Live run against a project where the agents are deployed (spends tokens and node time,
so it needs `--yes`):

```bash
pip install -e ".[live]"
az login --scope https://discovery.azure.com/.default
dft live --catalog catalog --agents cantera,mol-toolkit --kits physics-cfd-sim \
  --workspace-api-url https://<ws>.workspace.discovery.azure.com --project <project> \
  --client-dir <discovery checkout>/utilities/agent-evaluation/evaluators \
  --agent-map maps/<project>.map --out out/live --yes
```

## Checks

| ID | Where | Detects | Category on failure |
|---|---|---|---|
| F0.actions | offline | Instructions describe an action or parameter that `tool.yaml` doesn't define | definition |
| F0.imports | offline | Instructions import a helper module/function the tool image doesn't ship | definition |
| F0.calls | offline | Example calls in instructions that would raise `TypeError` against the real signature | definition |
| F0.bindings | offline | Tool placeholders that can't be bound; hard-coded foreign `toolId`s | definition |
| F0.model | offline | Hard-coded model instead of `{{CHAT-MODEL}}` | definition |
| F0.deprecated | offline | Deprecated identifiers from `config/deprecated.yaml` | definition |
| F0.kit | offline | Kit `agentRefs` that don't resolve; sample prompts with no `expectedOutput` | definition |
| F1.invoke | live | Agent missing, auth/infra failure, timeout | deploy / platform / timeout |
| F2.capability | live | Empty answer or "I can't / don't know how" | capability |
| F3.tool-used | live | Answered without running its own tool | tool-not-used |
| F3.tool-ok | live | Tool ran but errored (traceback, failed status, image pull) | tool-error / platform |
| F4.output | live | Answer misses the kit's `expectedOutput` terms | wrong-output |

Live checks are deterministic (no LLM judge). Quality and safety scoring is PR #100's job.

## How it fits with PR #100

[microsoft/discovery#100](https://github.com/microsoft/discovery/pull/100) (`utilities/agent-evaluation`)
invokes an agent, captures the Responses object and scores it with Foundry evaluators. `dft`
reuses it rather than duplicating it:

- `dft live` invokes agents through PR #100's `DiscoveryAgentClient` (`--client-dir`).
- `dft plan --emit-pr100 DIR` writes the same test prompts as PR #100 `<suite>-evaluators.json`
  datasets, so a pass/fail functional run and a Foundry quality run use one set of prompts.

## Test cases

1. **Starter kits:** every `samplePrompt`, checked against its `expectedOutput`. Services deploy
   a kit as separate agents, so each prompt is routed to the agent it names, or to a route in
   `smoke/kits/<kit>.yaml`, or else to the kit's primary agent.
2. **Curated agents:** `smoke/<agent>.yaml`, one small checkable task that needs the tool.
3. **Auto:** a generic "run your tool once" prompt for everything else (`origin: auto`).

## Results

- `out/*-results.json`: findings and per-case results. Catalog commit and service version are
  recorded, so `dft diff` gives regression (new drop) and back-compat (new service release) reports.
- `out/*-report.md`: human report with GitHub links to the exact line.
- Structured JSON events on stderr (`run.start`, `case.end`, ...). No tokens are ever logged.
- `out/live/raw/*.json`: the captured Responses objects, for triage.

## CI

- `.github/workflows/static.yml`: weekly and on demand. Checks out `microsoft/discovery` as data,
  runs `dft static`, and fails only on regressions against `baselines/static.json`. No secrets.
- `.github/workflows/live.yml`: manual only, behind a `discovery-live` environment approval.
  Uses OIDC (same variables as PR #100 Option B). The PR #100 client is pinned to a reviewed
  commit so unreviewed code never runs with credentials.

## Safety

- Catalog content is parsed, never imported or executed (`ast.parse` only).
- Live runs require `--yes`, run in their own investigation (`dft-<run-id>`) and only in the
  project you name.
- Auth is `az login`/OIDC via `azure-identity`. No keys in code, results or reports.
