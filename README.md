# tn-venv-create-activate

![Official](https://img.shields.io/badge/official%20authorized-C0C0C0)

A [tn-venv](https://github.com/tokenoodle-everything/tn-venv) plugin that
activates the new environment when creating a new one.

```console
$ tn-venv .venv
==> creating virtual environment at C:\project\.venv
==> activating C:\project\.venv in a new powershell shell — type 'exit' to leave
(.venv) PS C:\project>          # ← you land here, already activated
(.venv) PS C:\project> exit     # ← back to your original shell, untouched
```

## Install

```bash
pip install tn-venv-create-activate
```

## Why a subshell?

A child process cannot rewrite the environment block of its parent shell —
that is why plain `tn-venv` can only *print* an activation hint. The
standard workaround (used by `pipenv shell` and `poetry shell`) is to spawn
a fresh interactive shell with the environment already activated, which is
exactly what this plugin does at the end of every successful creation.
Exiting the subshell returns you to your original, unmodified shell.

## Installation

```console
$ pip install tn-venv-create-activate
```

The plugin registers itself through the `tn_venv.plugins` entry-point
group, so any `tn-venv` invocation in the same Python environment picks it
up automatically — no configuration needed.

## How it works

After the environment has been created, the activation scripts written, and
pip seeded (`session_end` hook), the plugin detects the shell you are
typing in and spawns it with the environment activated:

| shell      | mechanism                                                     |
| ---------- | ------------------------------------------------------------- |
| cmd.exe    | `cmd.exe /K activate.bat`                                     |
| PowerShell | `powershell`/`pwsh -NoExit -File Activate.ps1`                |
| bash       | `bash --rcfile <temp rc that sources activate> -i`            |
| zsh        | `ZDOTDIR` temp dir whose `.zshrc` sources yours, then `activate` |
| fish       | `fish -i -C "source activate.fish"`                           |
| other      | `PATH`/`VIRTUAL_ENV` set directly, shell spawned interactively |

Script-based modes get the full feature set of the generated activation
scripts (`deactivate`, prompt prefix, `PYTHONHOME` bookkeeping). The
generic fallback still gives you a working prompt where `python`/`pip`
resolve into the new environment, but without a `deactivate` helper — use
`exit`, which always works.

The plugin stays out of the way when it should:

- non-interactive sessions (pipelines, CI, the tn-venv GUI host) are left
  alone — stdin/stdout must both be TTYs;
- if the target environment is already the active one, nothing happens;
- if the shell cannot be detected, a warning with the manual activation
  path is printed instead.

## Configuration

| Variable                       | Effect                                                        |
| ------------------------------ | ------------------------------------------------------------- |
| `TN_VENV_CREATE_ACTIVATE`      | `0` / `no` / `false` / `off` disables the plugin (e.g. in CI) |
| `TN_VENV_CREATE_ACTIVATE_SHELL` | Force the shell: a name (`cmd`, `powershell`, `pwsh`, `bash`, `zsh`, `fish`, …) or a full path |

Shell detection without the override walks the process tree to find the
nearest ancestor that is a known shell, then falls back to `$SHELL`
(POSIX), `%ComSpec%` (Windows), and finally `/bin/sh`.

## License

Tokenoodle-Everything License 1.0 — see [License.txt](License.txt).
This is **not** an OSI-approved license; commercial use is prohibited.
