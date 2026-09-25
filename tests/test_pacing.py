"""JitterSchedule: bounded, jittered backoff that carries settled pacing across runs.
Run: ./venv/bin/python tests/test_pacing.py"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout.pacing import JitterSchedule  # noqa: E402


def _schedule(tmp_path, **kw):
    kw.setdefault("mode", "none")  # deterministic: no randomization to assert against
    return JitterSchedule(key="test", state_path=tmp_path / "pacing_state.json", **kw)


def test_delay_grows_exponentially_up_to_the_cap():
    with tempfile.TemporaryDirectory() as tmp:
        s = _schedule(Path(tmp), base=1.0, factor=2.0, max_delay=10.0)
        seen = []
        for _ in range(5):
            seen.append(s.delay())
            s._attempt += 1
        assert seen == [1.0, 2.0, 4.0, 8.0, 10.0]  # capped at max_delay on the 5th attempt


def test_equal_jitter_stays_within_bounds():
    with tempfile.TemporaryDirectory() as tmp:
        s = JitterSchedule(key="test", state_path=Path(tmp) / "state.json", base=10.0, factor=1.0,
                           max_delay=10.0, jitter=0.5, mode="equal")
        for _ in range(200):
            d = s.delay()
            assert 2.5 <= d <= 7.5  # 10/2 +/- 0.5 * 10/2


def test_reset_on_success_persists_settled_delay_for_next_schedule():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        s1 = JitterSchedule(key="src", state_path=path, base=1.0, factor=2.0, max_delay=100.0,
                            mode="none", carryover=1.0)
        s1._attempt = 3  # pretend it waited three times (attempts 0,1,2) before succeeding
        s1.reset(success=True)
        data = json.loads(path.read_text())
        assert data["src"]["settled_delay"] == 4.0  # last delay used was 1 * 2**(3-1)

        s2 = JitterSchedule(key="src", state_path=path, base=1.0, factor=2.0, max_delay=100.0,
                            mode="none", carryover=1.0)
        assert s2.delay() == 4.0  # next run starts from where the last one settled


def test_reset_on_failure_does_not_persist_state():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        s = JitterSchedule(key="src", state_path=path, base=1.0, mode="none")
        s._attempt = 4
        s.reset(success=False)
        assert not path.exists()


def test_carryover_zero_ignores_prior_run():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(json.dumps({"src": {"settled_delay": 50.0, "updated": 0}}))
        s = JitterSchedule(key="src", state_path=path, base=1.0, mode="none", carryover=0.0)
        assert s.delay() == 1.0


def test_partial_carryover_seeds_base_between_base_and_settled():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(json.dumps({"src": {"settled_delay": 20.0, "updated": 0}}))
        s = JitterSchedule(key="src", state_path=path, base=4.0, mode="none", carryover=0.5, max_delay=100.0)
        assert s.delay() == 12.0  # 4 + (20 - 4) * 0.5


def test_wait_sleeps_and_advances_attempt(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        slept = []
        monkeypatch.setattr("scout.pacing.time.sleep", lambda d: slept.append(d))
        s = _schedule(Path(tmp), base=2.0)
        d = s.wait()
        assert slept == [d] and s._attempt == 1


def test_rejects_invalid_config():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError):
            JitterSchedule(key="x", state_path=Path(tmp) / "s.json", mode="bogus")
        with pytest.raises(ValueError):
            JitterSchedule(key="x", state_path=Path(tmp) / "s.json", jitter=2.0)
        with pytest.raises(ValueError):
            JitterSchedule(key="x", state_path=Path(tmp) / "s.json", base=0)


def test_corrupt_state_file_is_ignored_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text("{not json")
        s = JitterSchedule(key="src", state_path=path, base=3.0, mode="none")
        assert s.delay() == 3.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
