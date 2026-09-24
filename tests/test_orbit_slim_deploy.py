"""Regression tests for the slim Orbit Vercel deploy payload."""

from scripts.build_orbit_slim_deploy import slim_signal


def test_slim_signal_preserves_supporting_data():
    supporting_data = {"rsi": 42.5, "stub": False}
    slimmed = slim_signal(
        {
            "agent_id": "momentum",
            "ticker": "IONQ",
            "direction": "long",
            "confidence": 70.0,
            "reasoning": "test",
            "supporting_data": supporting_data,
            "created_at": "2026-09-24T14:00:00Z",
        }
    )

    assert slimmed["supporting_data"] == supporting_data
