"""Unit tests for tn_venv_create_activate.

Everything here is pure logic — no subshell is ever spawned. The hook
body is exercised with ``_spawn``/``_interactive``/``detect_shell``
monkeypatched out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import tn_venv_create_activate as mod
from tn_venv.plugins import HookContext, HookName
from tn_venv.plugins.registry import HookRegistry


@pytest.fixture()
def env_dir(tmp_path: Path) -> Path:
    """A fake environment with the standard activation scripts."""
    bin_name = "Scripts" if os.name == "nt" else "bin"
    bin_path = tmp_path / ".venv" / bin_name
    bin_path.mkdir(parents=True)
    for script in ("activate", "activate.bat", "Activate.ps1", "activate.fish"):
        (bin_path / script).write_text("rem fake\n", encoding="utf-8")
    return tmp_path / ".venv"


def bin_path_of(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin")


def make_ctx(env_dir: Path | None, **env: str) -> HookContext:
    result = None
    if env_dir is not None:
        result = SimpleNamespace(
            env_dir=env_dir,
            bin_path=bin_path_of(env_dir),
            prompt=env_dir.name,
        )
    return HookContext(options=None, reporter=None, result=result, data={})


# --------------------------------------------------------------------------- #
# _disabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["0", "no", "false", "off", "NO", " Off "])
def test_disabled_truthy_off_values(value: str) -> None:
    assert mod._disabled({mod.ENV_DISABLE: value}) is True


@pytest.mark.parametrize("value", ["", "1", "yes", "on", "anything"])
def test_disabled_other_values(value: str) -> None:
    assert mod._disabled({mod.ENV_DISABLE: value}) is False


def test_disabled_unset() -> None:
    assert mod._disabled({}) is False


# --------------------------------------------------------------------------- #
# shell-kind mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("cmd.exe", "cmd"),
        ("CMD.EXE", "cmd"),
        ("powershell.exe", "powershell"),
        ("pwsh", "powershell"),
        ("pwsh.exe", "powershell"),
        ("bash", "bash"),
        ("bash.exe", "bash"),
        ("/usr/bin/zsh", "zsh"),
        ("fish", "fish"),
        ("nu.exe", "nushell"),
        ("tcsh", "csh"),
        ("dash", "posix"),
    ],
)
def test_kind_for_name(name: str, kind: str) -> None:
    assert mod._kind_for_name(name) == kind


def test_kind_for_name_unknown() -> None:
    assert mod._kind_for_name("python.exe") is None
    assert mod._kind_for_name("explorer.exe") is None


def test_spec_from_name_or_path_by_name() -> None:
    spec = mod._spec_from_name_or_path("cmd")
    assert spec.kind == "cmd"
    assert spec.executable  # resolved via PATH or kept as-is


def test_spec_from_name_or_path_by_path(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "pwsh.exe"
    fake.parent.mkdir()
    fake.write_text("", encoding="utf-8")
    spec = mod._spec_from_name_or_path(str(fake))
    assert spec.kind == "powershell"
    assert spec.executable == str(fake)


def test_spec_from_name_or_path_unknown_path(tmp_path: Path) -> None:
    fake = tmp_path / "myshell"
    fake.write_text("", encoding="utf-8")
    spec = mod._spec_from_name_or_path(str(fake))
    assert spec.kind == "generic"


# --------------------------------------------------------------------------- #
# detect_shell
# --------------------------------------------------------------------------- #


def test_detect_shell_honours_override() -> None:
    spec = mod.detect_shell({mod.ENV_SHELL: "bash"})
    assert spec is not None
    assert spec.kind == "bash"


def test_detect_shell_posix_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_process_table_posix", lambda: {})
    monkeypatch.setattr(mod, "_process_table_windows", lambda: {})
    monkeypatch.setattr(mod.os, "name", "posix")
    spec = mod.detect_shell({})
    assert spec is not None
    assert spec.kind == "posix"
    assert spec.executable == "/bin/sh"


def test_detect_shell_windows_comspec_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_process_table_posix", lambda: {})
    monkeypatch.setattr(mod, "_process_table_windows", lambda: {})
    monkeypatch.setattr(mod.os, "name", "nt")
    spec = mod.detect_shell({"ComSpec": r"C:\Windows\System32\cmd.exe"})
    assert spec is not None
    assert spec.kind == "cmd"


def test_detect_shell_walks_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # our parent (pid 100) is python, its parent (pid 50) is pwsh.exe
    monkeypatch.setattr(mod.os, "getppid", lambda: 100)
    monkeypatch.setattr(
        mod,
        "_process_table_windows",
        lambda: {100: (50, "python.exe"), 50: (4, "pwsh.exe")},
    )
    monkeypatch.setattr(mod.os, "name", "nt")
    spec = mod.detect_shell({})
    assert spec is not None
    assert spec.kind == "powershell"


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #


def plan_for(kind: str, env_dir: Path, base_env: dict[str, str] | None = None) -> mod.SpawnPlan:
    return mod.build_plan(
        mod.ShellSpec(kind=kind, executable=fake_exe(kind)),
        env_dir=env_dir,
        bin_path=bin_path_of(env_dir),
        prompt=env_dir.name,
        base_env=base_env if base_env is not None else {"PATH": "/usr/bin"},
    )


def fake_exe(kind: str) -> str:
    return {
        "cmd": "cmd.exe",
        "powershell": "pwsh.exe",
        "bash": "bash",
        "zsh": "zsh",
        "fish": "fish",
    }.get(kind, kind)


def test_build_plan_cmd_uses_activate_bat(env_dir: Path) -> None:
    plan = plan_for("cmd", env_dir)
    assert plan.script_based
    assert plan.argv[0] == "cmd.exe"
    assert plan.argv[1] == "/K"
    assert plan.argv[2].endswith("activate.bat")
    # script mode does not pre-activate: PATH untouched
    assert plan.env["PATH"] == "/usr/bin"


def test_build_plan_powershell_uses_activate_ps1(env_dir: Path) -> None:
    plan = plan_for("powershell", env_dir)
    assert plan.script_based
    assert "-NoExit" in plan.argv
    assert "-File" in plan.argv
    assert plan.argv[-1].endswith("Activate.ps1")
    if os.name == "nt":
        assert "-ExecutionPolicy" in plan.argv
        assert "Bypass" in plan.argv
    else:
        assert "-ExecutionPolicy" not in plan.argv


def test_build_plan_bash_writes_rcfile(env_dir: Path) -> None:
    plan = plan_for("bash", env_dir)
    try:
        assert plan.script_based
        assert plan.argv[0] == "bash"
        assert "--rcfile" in plan.argv
        assert "-i" in plan.argv
        assert len(plan.temp_paths) == 1
        rc = plan.temp_paths[0]
        assert rc.is_file()
        content = rc.read_text(encoding="utf-8")
        assert "source" in content
        assert "activate" in content
    finally:
        mod._cleanup(plan.temp_paths)
    assert not rc.exists()


def test_build_plan_zsh_sets_zdotdir(env_dir: Path) -> None:
    plan = plan_for("zsh", env_dir)
    try:
        assert plan.script_based
        zdotdir = Path(plan.env["ZDOTDIR"])
        assert zdotdir.is_dir()
        zshrc = (zdotdir / ".zshrc").read_text(encoding="utf-8")
        assert "activate" in zshrc
        assert plan.argv == ["zsh", "-i"]
    finally:
        mod._cleanup(plan.temp_paths)
    assert not zdotdir.exists()


def test_build_plan_fish_uses_init_command(env_dir: Path) -> None:
    plan = plan_for("fish", env_dir)
    assert plan.script_based
    assert "-C" in plan.argv
    init_cmd = plan.argv[plan.argv.index("-C") + 1]
    assert "activate.fish" in init_cmd


def test_build_plan_generic_env_when_script_missing(env_dir: Path) -> None:
    # remove the bash script → generic env-var fallback
    (bin_path_of(env_dir) / "activate").unlink()
    plan = plan_for("bash", env_dir, base_env={"PATH": "/usr/bin"})
    assert not plan.script_based
    assert plan.env["VIRTUAL_ENV"] == str(env_dir)
    assert plan.env["VIRTUAL_ENV_PROMPT"] == env_dir.name
    assert plan.env["PATH"].startswith(str(bin_path_of(env_dir)))
    assert plan.argv[0] == "bash"
    assert "-i" in plan.argv


def test_build_plan_generic_strips_pythonhome(env_dir: Path) -> None:
    (bin_path_of(env_dir) / "activate").unlink()
    plan = plan_for("bash", env_dir, base_env={"PATH": "/usr/bin", "PYTHONHOME": "/x"})
    assert "PYTHONHOME" not in plan.env


def test_build_plan_unknown_kind_falls_back_to_generic(env_dir: Path) -> None:
    plan = mod.build_plan(
        mod.ShellSpec(kind="generic", executable=str(bin_path_of(env_dir))),
        env_dir=env_dir,
        bin_path=bin_path_of(env_dir),
        prompt="p",
        base_env={"PATH": "/usr/bin"},
    )
    assert not plan.script_based
    assert plan.env["VIRTUAL_ENV"] == str(env_dir)


# --------------------------------------------------------------------------- #
# plugin registration & hook body
# --------------------------------------------------------------------------- #


def test_register_adds_session_end_listener() -> None:
    registry = HookRegistry()
    mod.CreateActivatePlugin().register(registry)
    assert registry.listener_count(HookName.SESSION_END) == 1


def test_plugin_has_stable_name() -> None:
    assert mod.CreateActivatePlugin.name == "create_activate"


def test_help_epilog_mentions_env_vars() -> None:
    text = mod._help_epilog()
    assert mod.ENV_DISABLE in text
    assert mod.ENV_SHELL in text


def run_hook(
    monkeypatch: pytest.MonkeyPatch,
    ctx: HookContext,
    env: dict[str, str],
    *,
    interactive: bool = True,
) -> list[mod.SpawnPlan]:
    """Run _on_session_end with all side effects stubbed; return spawn calls."""
    spawned: list[mod.SpawnPlan] = []
    monkeypatch.setattr(mod.os, "environ", env.copy())
    monkeypatch.setattr(mod, "_interactive", lambda: interactive)
    monkeypatch.setattr(mod, "detect_shell", lambda e: mod.ShellSpec("cmd", "cmd.exe"))
    monkeypatch.setattr(mod, "_spawn", lambda plan: spawned.append(plan) or 0)
    mod.CreateActivatePlugin()._on_session_end(ctx)
    return spawned


def test_hook_spawns_shell(monkeypatch: pytest.MonkeyPatch, env_dir: Path) -> None:
    spawned = run_hook(monkeypatch, make_ctx(env_dir), {})
    assert len(spawned) == 1
    assert spawned[0].argv[0] == "cmd.exe"


def test_hook_skips_when_disabled(monkeypatch: pytest.MonkeyPatch, env_dir: Path) -> None:
    spawned = run_hook(monkeypatch, make_ctx(env_dir), {mod.ENV_DISABLE: "0"})
    assert spawned == []


def test_hook_skips_when_not_interactive(monkeypatch: pytest.MonkeyPatch, env_dir: Path) -> None:
    spawned = run_hook(monkeypatch, make_ctx(env_dir), {}, interactive=False)
    assert spawned == []


def test_hook_skips_when_same_env_already_active(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path
) -> None:
    spawned = run_hook(monkeypatch, make_ctx(env_dir), {"VIRTUAL_ENV": str(env_dir)})
    assert spawned == []


def test_hook_activates_when_other_env_active(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path, tmp_path: Path
) -> None:
    spawned = run_hook(monkeypatch, make_ctx(env_dir), {"VIRTUAL_ENV": str(tmp_path / "other")})
    assert len(spawned) == 1


def test_hook_tolerates_missing_result(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = run_hook(monkeypatch, make_ctx(None), {})
    assert spawned == []


def test_hook_warns_and_continues_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path
) -> None:
    monkeypatch.setattr(mod.os, "environ", {})
    monkeypatch.setattr(mod, "_interactive", lambda: True)
    monkeypatch.setattr(mod, "detect_shell", lambda e: mod.ShellSpec("cmd", "cmd.exe"))

    def boom(plan: mod.SpawnPlan) -> int:
        raise OSError("no such shell")

    monkeypatch.setattr(mod, "_spawn", boom)
    # must not raise — plugin failure must not break the session
    mod.CreateActivatePlugin()._on_session_end(make_ctx(env_dir))


# --------------------------------------------------------------------------- #
# misc helpers
# --------------------------------------------------------------------------- #


def test_same_path(tmp_path: Path) -> None:
    assert mod._same_path(tmp_path, tmp_path)
    assert not mod._same_path(tmp_path, tmp_path / "other")


def test_version_exposed() -> None:
    assert mod.__version__ == "0.1.0"
    assert mod.version_tuple == (0, 1, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only code path")
def test_process_table_windows_sees_own_process() -> None:
    table = mod._process_table_windows()
    assert os.getpid() in table
