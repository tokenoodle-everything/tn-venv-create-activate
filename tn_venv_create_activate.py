"""tn-venv plugin that activates the new environment right after creating it.

A child process cannot rewrite the environment block of its parent shell,
so "activation" here means the plugin *spawns a fresh interactive subshell*
with the environment activated, the way ``pipenv shell`` and ``poetry
shell`` do. The user lands at a prompt where ``python`` and ``pip`` already
resolve into the new environment; typing ``exit`` (or pressing Ctrl-D)
closes the subshell and drops back to the original, unmodified shell.

Shell-specific integration
--------------------------

When the current shell can be identified, the plugin uses the activation
script tn-venv already generated for it, so the full feature set
(``deactivate``, prompt prefix, ``PYTHONHOME`` bookkeeping) is available:

============  =============================================================
shell         mechanism
============  =============================================================
cmd.exe       ``cmd.exe /K activate.bat``
PowerShell    ``powershell/pwsh -NoExit -File Activate.ps1``
bash          ``bash --rcfile <temp rc that sources activate> -i``
zsh           ``ZDOTDIR`` pointed at a temp dir whose ``.zshrc`` sources
              the user's real ``.zshrc`` and then ``activate``
fish          ``fish -i -C "source activate.fish"``
anything else PATH/VIRTUAL_ENV are set directly and the shell is spawned
              interactively (no ``deactivate`` helper in this mode)
============  =============================================================

Configuration
-------------

``TN_VENV_CREATE_ACTIVATE``
    Set to ``0``/``no``/``false``/``off`` to disable the plugin without
    uninstalling it (useful in CI).

``TN_VENV_CREATE_ACTIVATE_SHELL``
    Force the shell used for the subshell. Accepts a shell name
    (``cmd``, ``powershell``, ``pwsh``, ``bash``, ``zsh``, ``fish``, …)
    or a full path to an executable. When unset, the plugin walks the
    process tree to find the nearest ancestor that is a known shell,
    falling back to ``$SHELL`` (POSIX), ``%ComSpec%`` (Windows), and
    finally ``/bin/sh``.

The plugin only fires for interactive sessions: when stdin/stdout are not
TTYs (pipelines, CI, the tn-venv GUI host) it stays out of the way, and it
never re-activates an environment that is already active.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tn_venv.plugins import HookContext, HookName, Plugin
from tn_venv.plugins.registry import DEFAULT_PRIORITY

__all__ = [
    "__version__",
    "__version_tuple__",
    "version",
    "version_tuple",
    "__commit_id__",
    "commit_id",
    "ENV_DISABLE",
    "ENV_SHELL",
    "ShellSpec",
    "SpawnPlan",
    "CreateActivatePlugin",
]

# The string is the single source of truth (pyproject.toml reads it back
# statically via [tool.setuptools.dynamic]); the tuple derives from it.
__version__ = version = "0.1.0"
__version_tuple__ = version_tuple = tuple(int(part) for part in version.split("."))

__commit_id__ = commit_id = None

version: str
__version__: str
__version_tuple__: tuple[int | str, ...]
version_tuple: tuple[int | str, ...]
commit_id: str | None
__commit_id__: str | None

#: Set to a falsy-looking value to turn the plugin off without uninstalling.
ENV_DISABLE: str = "TN_VENV_CREATE_ACTIVATE"

#: Force the shell used for the activation subshell (name or full path).
ENV_SHELL: str = "TN_VENV_CREATE_ACTIVATE_SHELL"

_OFF_VALUES = frozenset({"0", "no", "false", "off"})

#: Maps a shell executable's base name (lowercase, no ``.exe``) to the
#: internal "kind" that selects an activation strategy.
_KINDS_BY_NAME = {
    "cmd": "cmd",
    "powershell": "powershell",
    "pwsh": "powershell",
    "bash": "bash",
    "zsh": "zsh",
    "fish": "fish",
    "nu": "nushell",
    "nushell": "nushell",
    "csh": "csh",
    "tcsh": "csh",
    "sh": "posix",
    "dash": "posix",
    "ash": "posix",
}

#: Activation script each shell kind sources, relative to the
#: environment's bin/Scripts directory.
_SCRIPT_BY_KIND = {
    "cmd": "activate.bat",
    "powershell": "Activate.ps1",
    "bash": "activate",
    "zsh": "activate",
    "fish": "activate.fish",
    "csh": "activate.csh",
    "nushell": "activate.nu",
    "posix": "activate",
}


@dataclass(frozen=True)
class ShellSpec:
    """A shell the plugin knows how to spawn."""

    kind: str
    executable: str


@dataclass
class SpawnPlan:
    """Everything needed to launch the activation subshell."""

    argv: list[str]
    env: dict[str, str]
    #: Temp files/dirs created for the launch (bash rcfile, zsh ZDOTDIR);
    #: removed once the subshell exits.
    temp_paths: list[Path] = field(default_factory=list)
    #: True when activation happens by sourcing a generated script, False
    #: when it happens by editing the child's environment directly.
    script_based: bool = True


# --------------------------------------------------------------------------- #
# plugin
# --------------------------------------------------------------------------- #


class CreateActivatePlugin(Plugin):
    """Spawn an interactive subshell with the new environment activated.

    Hooks into :attr:`~tn_venv.plugins.HookName.SESSION_END`, i.e. after
    the environment has been created, activators written, and pip seeded,
    so the subshell starts from the finished state.
    """

    name = "create_activate"

    def register(self, hooks) -> None:  # noqa: D401 - Plugin API
        hooks.add(
            HookName.SESSION_END,
            self._on_session_end,
            priority=DEFAULT_PRIORITY,
            plugin_name=self.name,
        )
        try:
            hooks.add(HookName.HELP_EPILOG, _help_epilog, plugin_name=self.name)
        except (AttributeError, ValueError):
            # tn-venv build without the HELP_EPILOG hook — skip silently.
            pass

    # -- hook body -----------------------------------------------------------

    def _on_session_end(self, ctx: HookContext) -> None:
        reporter = ctx.reporter
        base_env = dict(os.environ)

        if _disabled(base_env):
            _debug(reporter, f"{self.name}: disabled via {ENV_DISABLE}")
            return

        paths = _session_paths(ctx)
        if paths is None:
            _debug(reporter, f"{self.name}: no environment paths in context; skipping")
            return
        env_dir, bin_path, prompt = paths

        if not _interactive():
            _debug(reporter, f"{self.name}: non-interactive session; skipping")
            return

        current = base_env.get("VIRTUAL_ENV")
        if current and _same_path(Path(current), env_dir):
            _debug(reporter, f"{self.name}: {env_dir} is already active; skipping")
            return

        spec = detect_shell(base_env)
        if spec is None:
            if reporter is not None:
                reporter.warn(
                    f"{self.name}: could not detect the current shell; "
                    f"activate manually from {bin_path}"
                )
            return

        plan = build_plan(
            spec, env_dir=env_dir, bin_path=bin_path, prompt=prompt, base_env=base_env
        )
        if reporter is not None:
            reporter.step(
                f"activating {reporter.path(env_dir)} in a new {spec.kind} shell "
                f"— type 'exit' to leave"
            )
        try:
            _spawn(plan)
        except OSError as exc:
            if reporter is not None:
                reporter.warn(
                    f"{self.name}: failed to spawn {spec.executable!r}: {exc}; "
                    f"activate manually from {bin_path}"
                )
            return
        finally:
            _cleanup(plan.temp_paths)
        _debug(reporter, f"{self.name}: subshell closed; {env_dir} deactivated")


# --------------------------------------------------------------------------- #
# shell detection
# --------------------------------------------------------------------------- #


def detect_shell(env: dict[str, str] | None = None) -> ShellSpec | None:
    """Identify the shell the user is typing into.

    Resolution order: :data:`ENV_SHELL` override → nearest shell ancestor
    in the process tree → ``$SHELL`` → ``%ComSpec%`` (Windows) →
    ``/bin/sh`` (POSIX). Returns ``None`` only when every source failed.
    """
    env = os.environ if env is None else env
    override = env.get(ENV_SHELL, "").strip()
    if override:
        return _spec_from_name_or_path(override)

    table = _process_table_windows() if os.name == "nt" else _process_table_posix()
    pid: int | None = os.getppid()
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        row = table.get(pid)
        if row is None:
            break
        ppid, exe_name = row
        kind = _kind_for_name(exe_name)
        if kind is not None:
            return ShellSpec(kind=kind, executable=shutil.which(exe_name) or exe_name)
        pid = ppid

    shell_env = env.get("SHELL", "").strip()
    if shell_env:
        return _spec_from_name_or_path(shell_env)
    if os.name == "nt":
        comspec = (env.get("ComSpec") or env.get("COMSPEC") or "").strip()
        if comspec:
            return ShellSpec(kind="cmd", executable=comspec)
        return None
    return ShellSpec(kind="posix", executable="/bin/sh")


def _kind_for_name(name: str) -> str | None:
    """Map an executable name (``pwsh.exe``) to a shell kind (``powershell``)."""
    base = name.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-4] if base.endswith(".exe") else base
    return _KINDS_BY_NAME.get(base) or _KINDS_BY_NAME.get(stem)


def _spec_from_name_or_path(value: str) -> ShellSpec:
    """Build a :class:`ShellSpec` from a shell name or an executable path."""
    looks_like_path = any(sep and sep in value for sep in (os.sep, os.altsep))
    if looks_like_path:
        kind = _kind_for_name(Path(value).name) or "generic"
        return ShellSpec(kind=kind, executable=value)
    kind = _kind_for_name(value) or "generic"
    return ShellSpec(kind=kind, executable=shutil.which(value) or value)


def _process_table_posix() -> dict[int, tuple[int, str]]:
    """pid → (ppid, command name) from ``ps``; empty on any failure."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), Path(parts[2]).name)
    return table


def _process_table_windows() -> dict[int, tuple[int, str]]:
    """pid → (ppid, exe name) via the Toolhelp32 snapshot API."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            # ULONG_PTR — a plain pointer has the right size on both arches.
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = wintypes.HANDLE(-1).value
    if not snapshot or snapshot == invalid:
        return {}
    try:
        table: dict[int, tuple[int, str]] = {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile),
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return table
    finally:
        kernel32.CloseHandle(snapshot)


# --------------------------------------------------------------------------- #
# spawn plan construction
# --------------------------------------------------------------------------- #


def build_plan(
    spec: ShellSpec,
    *,
    env_dir: Path,
    bin_path: Path,
    prompt: str,
    base_env: dict[str, str],
) -> SpawnPlan:
    """Choose the activation strategy for *spec* and materialise a plan.

    Prefers sourcing the matching tn-venv activation script; falls back to
    editing the child environment directly when the script is missing
    (e.g. the user restricted ``--activators``) or the shell is unknown.
    """
    script = bin_path / _SCRIPT_BY_KIND.get(spec.kind, "")
    env = dict(base_env)
    temp_paths: list[Path] = []

    if script.is_file() and spec.kind == "cmd":
        argv = [spec.executable, "/K", str(script)]
    elif script.is_file() and spec.kind == "powershell":
        argv = [spec.executable, "-NoExit", "-NoLogo"]
        if os.name == "nt":
            argv += ["-ExecutionPolicy", "Bypass"]
        argv += ["-File", str(script)]
    elif script.is_file() and spec.kind == "bash":
        rc = _write_temp_file(
            f'[[ -f ~/.bashrc ]] && source ~/.bashrc\nsource "{script.as_posix()}"\n',
            suffix=".bashrc",
        )
        temp_paths.append(rc)
        argv = [spec.executable, "--rcfile", rc.as_posix(), "-i"]
    elif script.is_file() and spec.kind == "zsh":
        zdotdir = Path(tempfile.mkdtemp(prefix="tn-venv-zdotdir-"))
        temp_paths.append(zdotdir)
        real_zdotdir = env.get("ZDOTDIR") or str(Path.home())
        (zdotdir / ".zshrc").write_text(
            f'[[ -f "{real_zdotdir}/.zshrc" ]] && source "{real_zdotdir}/.zshrc"\n'
            f'source "{script.as_posix()}"\n',
            encoding="utf-8",
        )
        env["ZDOTDIR"] = str(zdotdir)
        argv = [spec.executable, "-i"]
    elif script.is_file() and spec.kind == "fish":
        argv = [spec.executable, "-i", "-C", f'source "{script.as_posix()}"']
    else:
        env = _generic_env(env_dir, bin_path, prompt, base_env)
        argv = [spec.executable]
        if spec.kind in ("posix", "bash", "zsh", "fish", "csh"):
            argv.append("-i")
        return SpawnPlan(argv=argv, env=env, temp_paths=temp_paths, script_based=False)

    return SpawnPlan(argv=argv, env=env, temp_paths=temp_paths, script_based=True)


def _generic_env(
    env_dir: Path, bin_path: Path, prompt: str, base_env: dict[str, str]
) -> dict[str, str]:
    """Environment block that activates *env_dir* without a script."""
    env = dict(base_env)
    env["VIRTUAL_ENV"] = str(env_dir)
    env["VIRTUAL_ENV_PROMPT"] = prompt or env_dir.name
    env.pop("PYTHONHOME", None)
    env["PATH"] = str(bin_path) + os.pathsep + env.get("PATH", "")
    return env


def _spawn(plan: SpawnPlan) -> int:
    """Run the subshell to completion, inheriting our stdio."""
    return subprocess.call(plan.argv, env=plan.env)


def _write_temp_file(content: str, *, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="tn-venv-", suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return Path(name)


def _cleanup(temp_paths: list[Path]) -> None:
    for path in temp_paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _disabled(env: dict[str, str]) -> bool:
    return env.get(ENV_DISABLE, "").strip().lower() in _OFF_VALUES


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _session_paths(ctx: HookContext) -> tuple[Path, Path, str] | None:
    """Pull (env_dir, bin_path, prompt) out of the hook context."""
    result = ctx.result
    env_dir = getattr(result, "env_dir", None) if result is not None else None
    if env_dir is None and ctx.data:
        env_dir = ctx.data.get("env_dir")
    if env_dir is None:
        return None
    env_dir = Path(env_dir)

    bin_path = getattr(result, "bin_path", None) if result is not None else None
    if bin_path is None:
        bin_path = env_dir / ("Scripts" if os.name == "nt" else "bin")
    bin_path = Path(bin_path)

    prompt = ""
    if result is not None:
        prompt = str(getattr(result, "prompt", "") or "")
    return env_dir, bin_path, prompt or env_dir.name


def _debug(reporter, message: str) -> None:
    if reporter is not None:
        reporter.debug(message)


def _help_epilog() -> str:
    return (
        "create-activate plugin (tn-venv-create-activate):\n"
        "  after a new environment is created, an interactive subshell with the\n"
        "  environment activated is spawned automatically (type 'exit' to leave).\n"
        f"  Set {ENV_DISABLE}=0 to disable this, or {ENV_SHELL}=<name|path>\n"
        "  to force the shell used (cmd, powershell, pwsh, bash, zsh, fish, ...)."
    )
