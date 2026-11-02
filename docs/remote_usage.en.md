# Unified local and remote use

[Engineering overview](README.md) · [中文](remote_usage.md) · [CLI reference](cli_reference.en.md)

Install compatible gpuq versions on both hosts and configure OpenSSH keys and host verification:

```bash
gpuq --remote user@gpu-server daemon --background --max-running-jobs 2
gpuq --remote user@gpu-server submit --project-limit 2 -- python3 train.py
gpuq --remote user@gpu-server list --json
gpuq --remote user@gpu-server logs 1 -f
gpuq --remote user@gpu-server cancel 1
```

Global options precede the subcommand. --remote accepts an IP, user@host, SSH alias or configured profile.
Job IDs, dependencies, fairness and total concurrency are local to the selected queue; multiple GPUs belong to
that remote host. Commands/environment travel over stdin without an implicit shell. Explicit bash -lc scripts
must handle their own embedded paths. Connection failures never fall back to local execution or automatically
resubmit. If the submission response is lost, inspect the remote queue before retrying.

## Persistent workspace, incremental code and model caches

By default gpuq discovers the local Git root (then pyproject.toml/package.json, otherwise cwd), hashes selected
files with SHA-256 and uploads only content absent from the remote cache. Each submission gets a separate writable
snapshot: later edits/deletions cannot change submitted code. Identical content, including renamed files and
permission changes, reuses uploaded bytes. This is not a live two-way mirror; outputs are not downloaded automatically.

The default remote root is ~/.local/share/gpuq/workspaces; override with --remote-workdir or profile.work_dir.
It holds job-* snapshots and relative outputs, blobs, environments and cache. Explicit --env values for
HF_HOME/TORCH_HOME/PIP_CACHE_DIR take precedence, followed by the remote process environment, then workspace/cache
subdirectories huggingface, torch and pip. Training libraries download models on the remote host and reuse their
caches. Pin model revisions for reproducibility. gpuq does not download models itself or copy the local model cache.

Git ignore applies to untracked files. Additional defaults exclude .git, .venv, venv, __pycache__, *.pyc, .env/.env.*,
.ssh, .aws, .cache, *.safetensors, *.bin, *.pt, *.pth, *.ckpt, *.onnx, *.h5, checkpoints, outputs, wandb and runs.
Add exclusion globs via .gpuqignore or repeated --sync-exclude. .gpuqignore supports blank lines and # comments,
not ! re-inclusion. Exclude large data explicitly and reference remote absolute dataset paths.

```bash
# Inspect selected files without connecting
gpuq --remote train submit --sync-preview -- python3 train.py
# Explicit project-relative checkpoint inclusion overrides ignore rules
gpuq --remote train submit --sync-include checkpoints/last.ckpt -- \
  python3 train.py --resume checkpoints/last.ckpt
# A checkpoint already on the server needs no transfer
gpuq --remote train submit -- python3 train.py --resume /srv/checkpoints/last.ckpt
```

--sync-include accepts a project-relative file/directory, not a glob; including a directory includes ignored files
within it. Use preview to check the result; exclusion rules are not a secret scanner. Only in-project relative symlinks
without .. are supported. Git submodules are not expanded recursively and directory entries are rejected;
exclude them explicitly or use an existing remote checkout. Empty directories are omitted except the submitted cwd. Snapshots and caches have no
automatic garbage collection. Do not delete anything referenced by active, retried or dependent jobs.

## Remote environments

Local CLI overrides take precedence over matching profile fields. Without a profile, --remote is the literal SSH target.

| Profile field | Default / owning host |
|---|---|
| host | Profile name itself, or the literal --remote target |
| port / identity | OpenSSH configuration when omitted; identity is a local path |
| command | Remote PATH gpuq, falling back to ~/.local/bin/gpuq |
| config / state_dir | Remote configuration loader resolves its environment/defaults when omitted |
| work_dir | Remote ~/.local/share/gpuq/workspaces |
| cwd | No default; only used with --no-sync |
| python | No command replacement without managed dependencies; remote python3 for requirements if omitted |
| requirements | Disabled by default; otherwise a lockfile path relative to the synced project root |


Example local ~/.config/gpuq/config.toml (or --config/GPUQ_CONFIG):

```toml
[remotes.train]
host = "user@gpu-server"
# port = 22
# identity = "~/.ssh/id_ed25519"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
state_dir = "~/.local/state/gpuq"
work_dir = "/srv/my-gpuq-workspace"
python = "/opt/conda/envs/training/bin/python"
# requirements = "requirements.lock"
```

Without requirements, use the existing remote environment. profile.python replaces the first python/python3.x
command and prepends its bin directory to PATH unless --env PATH is supplied, also making torchrun discoverable.
Do not upload local venv/Conda directories. Without python, commands use the remote PATH.
Local --config selects connection profiles; --remote-config selects a remote configuration file; --state-dir is a
remote path in remote mode. Local [gpuq] settings are not silently copied to the remote queue.

With requirements enabled, sync a self-contained lock with exact versions and content hashes, such as output from
pip-compile --generate-hashes. gpuq creates a remote venv with the selected interpreter and runs pip install
--require-hashes. Identical locks and interpreter identities reuse environments; changed locks create new ones
without modifying running tasks' environments. Setup errors abort before enqueueing. Includes/constraints/editables
(-r/-c/-e) are unsupported. This does not manage NVIDIA drivers, system libraries or CUDA wheel compatibility.
Managed environments set VIRTUAL_ENV/PATH (overriding matching --env values) and replace the first Python command; hardcoded interpreters inside scripts
are not rewritten. Dependency installation always uses workspace/cache/pip. Cached environments remain writable by their owner and are not immutable containers.

Use an existing remote checkout without syncing:

```bash
gpuq --remote train submit --no-sync --remote-cwd /srv/project -- python3 train.py
```

Alternatively set profile.cwd. A profile with requirements is rejected in no-sync mode; use a python-only profile.
No-sync does not inject workspace model-cache defaults: use the remote environment or explicit --env.
Users maintain the shared checkout and control edits during training.

## Daemon and fairness

--background explicitly starts a daemon detached from SSH, with output in remote state_dir/daemon.log. An existing
daemon is reused with its original settings; new arguments do not restart or reconfigure it. Restart an existing
service through its normal manager after configuration changes. Lock readiness does not verify GPU health.
Closing SSH does not cancel queued/running training.

Ordinary jobs default to fair project admission, one active job per project; --project-limit 2 persists a higher cap.
With total concurrency 2, A alone may run A+A; competing A/B get A+B. If B arrives after A+A started, it receives the
next slot without interrupting either A task. Positive --priority wins admission but never bypasses the total or
GPU constraints. See [scheduling](scheduling.en.md).
