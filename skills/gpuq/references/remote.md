# Remote workspaces

Read when using --remote, copying inputs, selecting remote Python, or resuming from a checkpoint.

## Profiles and execution environment

A remote may be an SSH alias, user@host, IP, or a [remotes.NAME] profile. Both ends need compatible gpuq.
--config selects local profiles; --remote-config selects the remote gpuq configuration. Use the existing
SSH identity and host verification. Connection failure does not imply the remote task failed.

```toml
[remotes.train]
host = "user@gpu-host"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
python = "/opt/conda/envs/train/bin/python"
# requirements = "requirements.lock"
```

This is a template, not a host to contact. Use profile.python for an existing interpreter; do not upload
local venv/Conda directories. Optional requirements refers to a project-relative self-contained pip lock
with exact versions and hashes. gpuq caches venvs by lock/interpreter; -r/-c/-e entries are unsupported.
CUDA/driver compatibility still depends on the remote host. A profile's runtime fields apply to --remote;
cluster execution uses each node's own [cluster] runtime settings instead.

## Inputs, caches and outputs

Default submit detects a project root (nearest Git ancestor, then package marker, then cwd), sends only
missing content hashes, and creates a separate writable snapshot. This is not a live mirror. Local edits
after submission do not change that snapshot; outputs are not automatically downloaded.

```bash
gpuq --remote train submit --sync-preview -- python train.py
gpuq --remote train submit --sync-include checkpoints/last.ckpt -- \
  python train.py --resume checkpoints/last.ckpt
```

Preview is local file selection, not a submission or SSH connection. Check the manifest when inputs may
be excluded or when the project contains credentials/large data. Defaults exclude weights/checkpoints,
outputs, venvs, .env files, and caches. --sync-include accepts a project-relative file/directory, not a glob,
and overrides ignore rules; avoid including whole directories containing unrelated secrets or model weights.
Git submodules are not recursively expanded. .gpuqignore/--sync-exclude add exclusions.

Models can be downloaded by the workload on the execution host and cached under HF_HOME/TORCH_HOME;
gpuq itself does not fetch them or copy the local HF cache. Pin model revision when reproducibility matters.
Cache defaults live beneath the persistent workspace (normally ~/.local/share/gpuq/workspaces), unless
explicit environment settings or the remote environment provide paths. Repeated jobs reuse cached content.

For resume, include the intended checkpoint explicitly or point to an existing remote absolute path.
An upstream dependency only waits for completion: use a known shared/output path for its artifacts.
Do not assume one task's relative outputs exist in a different task's snapshot.

For an already-managed remote checkout:

```bash
gpuq --remote train submit --no-sync --remote-cwd /srv/project -- python train.py
```

--no-sync requires a remote cwd (flag or profile), cannot combine with sync include/exclude or managed
requirements, and does not inject the usual workspace model-cache defaults. Changes to that shared
checkout affect subsequent execution. Do not delete snapshots/caches while live or retriable jobs use them.
