from dataclasses import replace

import pytest

from recorder.config import Config
from recorder.obs import ObsClient, ObsError, ObsRequestError, ObsSession, authentication


def settings(fake):
    return Config(obs_port=fake.port, obs_password=fake.password, auto_scene=False)


def test_wire_auth_and_ignored_events(fake_obs):
    client = ObsClient(settings(fake_obs))
    client.connect()
    try:
        assert client.request("GetVersion")["obsVersion"] == "mock-31.0"
        assert not client.request("GetRecordStatus")["outputActive"]
        with pytest.raises(ObsRequestError) as failure:
            client.request("UnknownRequest")
        assert failure.value.code == 204
    finally:
        client.close()


def test_bad_password_is_rejected(fake_obs):
    client = ObsClient(replace(settings(fake_obs), obs_password="wrong"))
    with pytest.raises(ObsError):
        client.connect()
    assert client.socket is None


def test_record_ownership_and_exact_path(fake_obs, tmp_path):
    session = ObsSession(settings(fake_obs), tmp_path, lambda _: None)
    session.open()
    try:
        assert session.stop() is None
        session.start()
        assert session.owned_record
        session.assert_recording()
        path = session.stop()
        assert path.name == "record_001.mp4" and path.is_file()
        assert not session.owned_record
    finally:
        session.close()


def test_existing_recording_is_not_stopped(fake_obs, tmp_path):
    fake_obs.active = True
    session = ObsSession(settings(fake_obs), tmp_path, lambda _: None)
    with pytest.raises(ObsError):
        session.open()
    session.close()
    assert fake_obs.active
    assert not any(name == "StopRecord" for name, _ in fake_obs.requests)


def test_rejected_start_does_not_claim_recording(fake_obs, tmp_path):
    session = ObsSession(settings(fake_obs), tmp_path, lambda _: None)
    session.open()
    fake_obs.fail_once = "StartRecord"
    with pytest.raises(ObsRequestError):
        session.start()
    assert not session.owned_record
    session.close()
    assert not any(name == "StopRecord" for name, _ in fake_obs.requests)


def test_scene_setup_restores_scene_and_mutes(fake_obs, tmp_path):
    session = ObsSession(replace(settings(fake_obs), auto_scene=True), tmp_path, lambda _: None)
    session.is_windows = True  # Exercise Windows request flow without modifying the host platform.
    session.open()
    assert fake_obs.current != "Original"
    assert fake_obs.inputs["Desktop Audio"]["muted"]
    assert fake_obs.inputs["Mic/Aux"]["muted"]
    assert session.journal_path.exists()
    session.close()
    assert fake_obs.current == "Original"
    assert fake_obs.scenes == {"Original"}
    assert set(fake_obs.inputs) == {"Desktop Audio", "Mic/Aux"}
    assert not any(item["muted"] for item in fake_obs.inputs.values())
    assert not session.journal_path.exists()


def test_partial_scene_setup_is_recovered(fake_obs, tmp_path):
    session = ObsSession(replace(settings(fake_obs), auto_scene=True), tmp_path, lambda _: None)
    session.is_windows = True
    fake_obs.fail_once = "SetInputVolume"
    with pytest.raises(ObsRequestError):
        session.open()
    assert session.journal_path.exists()
    session.close()
    assert fake_obs.current == "Original"
    assert fake_obs.scenes == {"Original"}
    assert set(fake_obs.inputs) == {"Desktop Audio", "Mic/Aux"}
    assert not session.journal_path.exists()
