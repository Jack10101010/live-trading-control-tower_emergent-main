"""Supervision maintenance control (M-SUPERVISE-2).

Context these tests exist to protect:

Supervision is a REPEATING Task Scheduler trigger (every 5 minutes) plus
`MultipleInstancesPolicy=IgnoreNew`, with the OS runtime lock as the
exactly-one-node authority. Task Scheduler's RestartOnFailure is deliberately
not relied upon — measured on the deployment host, three separate non-zero
action results were each logged as event 201 "successfully completed", event 203
was never emitted, and no restart ever occurred.

A repeating trigger means a node that is merely stopped comes back within one
interval. The MAINTENANCE marker is the ONLY thing that distinguishes an
intentional stop from an unexpected absence, which makes two properties
load-bearing:

  * the marker is PRESENCE-based — content is a human-readable reason and is
    never parsed, so there is no malformed-content state to mis-handle; and
  * a marker left behind by mistake must be loudly visible, because it silently
    disables automatic recovery.

The gate itself lives in the launcher (PowerShell), which must refuse to start
Python at all during maintenance; that file is git-ignored machine configuration
and is covered by `live_state\\ops\\validate-launcher.ps1`. What is testable here
is the Python-visible contract: where the marker lives, and that `live.status`
reports it honestly for every content shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import status                                          # noqa: E402
from live.config import LiveConfig                                # noqa: E402


def _cfg(tmp_path) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md",
                   kill_file=tmp_path / "state" / "KILL")
    c.ensure_dirs()
    (c.state_dir / "ops").mkdir(parents=True, exist_ok=True)
    return c


def _maintenance_row(cfg) -> tuple[str, str, str]:
    status.ROWS.clear()
    status.collect(cfg, probe_mt5=False)
    rows = [r for r in status.ROWS if r[0] == "supervision maintenance?"]
    assert len(rows) == 1, f"expected exactly one maintenance row, got {rows}"
    return rows[0]


def test_marker_lives_beside_the_other_ops_state(tmp_path):
    """The launcher hard-codes live_state\\ops\\MAINTENANCE; keep them agreeing."""
    cfg = _cfg(tmp_path)
    assert cfg.maintenance_marker == cfg.state_dir / "ops" / "MAINTENANCE"
    assert cfg.maintenance_marker.parent == cfg.kill_file.parent / "ops"


def test_absent_marker_reports_ok_and_says_recovery_is_armed(tmp_path):
    cfg = _cfg(tmp_path)
    assert not cfg.maintenance_marker.exists()
    _, verdict, detail = _maintenance_row(cfg)
    assert verdict == status.OK
    assert "absent" in detail


def test_present_marker_warns_and_surfaces_the_reason(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.maintenance_marker.write_text("swapping the engine pin\nsecond line ignored\n")
    _, verdict, detail = _maintenance_row(cfg)
    # WARN not FAIL: maintenance is a legitimate operator state, never a fault --
    # but it must never read as OK, because it means auto-recovery is disabled.
    assert verdict == status.WARN
    assert "MAINTENANCE ACTIVE" in detail
    assert "swapping the engine pin" in detail
    assert "second line ignored" not in detail


def test_empty_marker_still_blocks_and_says_so(tmp_path):
    """Presence is the signal. An empty file is a perfectly valid maintenance flag."""
    cfg = _cfg(tmp_path)
    cfg.maintenance_marker.write_text("")
    _, verdict, detail = _maintenance_row(cfg)
    assert verdict == status.WARN
    assert "none recorded" in detail


def test_malformed_marker_content_cannot_produce_ambiguity(tmp_path):
    """Undecodable bytes must not raise, and must not be read as 'not maintenance'.

    This is the fail-closed direction, and the two outcomes are not symmetric:
    leaving a dry-run shadow down is observable and costs nothing, whereas
    resurrecting a node an operator stopped on purpose can run the engine against
    half-edited state. So garbage means STOPPED, never STARTED.
    """
    cfg = _cfg(tmp_path)
    cfg.maintenance_marker.write_bytes(b"\xff\xfe\x00\x80binary junk\x00")
    _, verdict, detail = _maintenance_row(cfg)
    assert verdict == status.WARN
    assert "MAINTENANCE ACTIVE" in detail


def test_marker_content_is_never_interpreted_as_disabled(tmp_path):
    """No content may switch maintenance OFF -- otherwise the parse is a gate.

    A marker that had to be parsed would reintroduce exactly the ambiguity this
    design removes, so strings that look like an 'off' switch must still block.
    """
    cfg = _cfg(tmp_path)
    for content in ("enabled=0", "false", "off", "0", "DISABLED", "maintenance=no"):
        cfg.maintenance_marker.write_text(content)
        _, verdict, detail = _maintenance_row(cfg)
        assert verdict == status.WARN, f"{content!r} must not disable the marker"
        assert "MAINTENANCE ACTIVE" in detail


def test_removing_the_marker_re_arms_recovery(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.maintenance_marker.write_text("drill")
    assert _maintenance_row(cfg)[1] == status.WARN
    cfg.maintenance_marker.unlink()
    _, verdict, detail = _maintenance_row(cfg)
    assert verdict == status.OK
    assert "absent" in detail
