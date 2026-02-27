"""Tests for pi_decoder.fsutil — writable() context manager."""

from unittest.mock import patch, call, MagicMock
import subprocess

import pytest

from pi_decoder import fsutil
from pi_decoder.fsutil import writable


class TestWritableNonLinux:
    """writable() should be a no-op on non-Linux platforms."""

    def test_noop_no_subprocess_calls(self):
        """On Darwin, no subprocess calls should be made."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Darwin"), \
             patch("pi_decoder.fsutil.subprocess.run") as mock_run:
            with writable("/"):
                pass
            mock_run.assert_not_called()


class TestWritableLinux:
    """writable() should remount rw/ro on Linux."""

    def _make_run_mock(self, fail_ro=False, fail_rw=False):
        """Create a subprocess.run mock that succeeds by default."""
        def side_effect(cmd, **kwargs):
            result = MagicMock()
            if fail_rw and "remount,rw" in cmd:
                result.returncode = 1
                result.stderr = "mount: permission denied"
            elif fail_ro and "remount,ro" in cmd:
                result.returncode = 1
                result.stderr = "mount: device busy"
            else:
                result.returncode = 0
                result.stderr = ""
            return result
        return side_effect

    def test_remounts_rw_then_ro(self):
        """On Linux, should call remount,rw on entry and remount,ro on exit."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock()) as mock_run, \
             patch("pi_decoder.fsutil.os.sync"):
            # Reset refcounts from other tests
            fsutil._refcounts.clear()

            with writable("/"):
                # Should have called remount,rw
                mock_run.assert_called_once()
                assert "remount,rw" in mock_run.call_args_list[0][0][0]

            # After exit, should also have called remount,ro
            assert len(mock_run.call_args_list) == 2
            assert "remount,ro" in mock_run.call_args_list[1][0][0]

    def test_os_sync_called_before_ro(self):
        """os.sync() should be called before remounting ro."""
        call_order = []

        def track_run(cmd, **kwargs):
            call_order.append(("run", cmd))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        def track_sync():
            call_order.append(("sync",))

        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=track_run), \
             patch("pi_decoder.fsutil.os.sync", side_effect=track_sync):
            fsutil._refcounts.clear()
            with writable("/"):
                pass

        # sync should come before the ro remount
        sync_idx = next(i for i, c in enumerate(call_order) if c == ("sync",))
        ro_idx = next(i for i, c in enumerate(call_order) if c[0] == "run" and "remount,ro" in c[1])
        assert sync_idx < ro_idx

    def test_nested_calls_only_outermost_mounts(self):
        """Nested writable() calls should only mount/remount once."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock()) as mock_run, \
             patch("pi_decoder.fsutil.os.sync"):
            fsutil._refcounts.clear()

            with writable("/"):
                with writable("/"):
                    # Only one remount,rw call (from outermost)
                    assert mock_run.call_count == 1
                # Inner exit — no remount,ro yet
                assert mock_run.call_count == 1

            # Outermost exit — now remount,ro
            assert mock_run.call_count == 2

    def test_different_mount_points_independent(self):
        """Different mount points should be tracked independently."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock()) as mock_run, \
             patch("pi_decoder.fsutil.os.sync"):
            fsutil._refcounts.clear()

            with writable("/"):
                with writable("/boot/firmware"):
                    # Two remount,rw calls (one per mount point)
                    assert mock_run.call_count == 2

            # Four total: rw /, rw /boot/firmware, ro /boot/firmware, ro /
            assert mock_run.call_count == 4

    def test_rw_failure_raises(self):
        """If remount,rw fails, should raise RuntimeError."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock(fail_rw=True)):
            fsutil._refcounts.clear()

            with pytest.raises(RuntimeError, match="Failed to remount"):
                with writable("/"):
                    pass  # pragma: no cover — should not reach here

    def test_ro_failure_logs_warning(self):
        """If remount,ro fails, should log warning but not raise."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock(fail_ro=True)), \
             patch("pi_decoder.fsutil.os.sync"), \
             patch("pi_decoder.fsutil.log.warning") as mock_warn:
            fsutil._refcounts.clear()

            with writable("/"):
                pass  # Should not raise

            mock_warn.assert_called()

    def test_exception_inside_still_remounts_ro(self):
        """An exception inside the block should still trigger remount,ro."""
        with patch("pi_decoder.fsutil.platform.system", return_value="Linux"), \
             patch("pi_decoder.fsutil.subprocess.run", side_effect=self._make_run_mock()) as mock_run, \
             patch("pi_decoder.fsutil.os.sync"):
            fsutil._refcounts.clear()

            with pytest.raises(ValueError):
                with writable("/"):
                    raise ValueError("test error")

            # Should still have called remount,ro
            assert mock_run.call_count == 2
            assert "remount,ro" in mock_run.call_args_list[1][0][0]
