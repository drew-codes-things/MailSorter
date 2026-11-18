import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "client_harness.js"
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def client():
    if NODE is None:
        pytest.skip("node is not installed, so the page script cannot be run")
    run = subprocess.run([NODE, str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_the_body_frame_is_hidden_and_choices_wait_until_it_has_loaded(client):
    gate = client["frameGate"]
    assert gate["beforeLoad"] == {"hidden": True, "src": "/api/messages/aaa1/body", "choicesEnabled": False}
    assert gate["postsBeforeLoad"] == 0
    assert gate["afterLoad"]["hidden"] is False and gate["afterLoad"]["choicesEnabled"] is True


def test_the_next_email_keeps_the_choices_off_until_its_own_body_loads(client):
    gate = client["frameGate"]
    assert gate["secondBeforeLoad"] == {"hidden": True, "src": "/api/messages/bbb2/body", "choicesEnabled": False,
                                        "subject": "Second"}
    assert gate["postsDuringSecondLoad"] == 1
    assert gate["secondAfterLoad"]["choicesEnabled"] is True


def test_undo_pressed_while_the_next_email_loads_waits_and_then_runs(client):
    undo = client["undoWhileTheNextEmailLoads"]
    assert undo["nextIsLoading"] is True
    assert undo["whileLoading"]["undoSent"] is False and undo["whileLoading"]["toastHidden"] is False
    assert undo["afterStep"] == {"undoSent": True, "body": {"id": "aaa1", "action": "no"}, "toastHidden": True}
    assert undo["toastAfterUndo"] == "Undone: moved to the bin"


def test_sign_out_shows_whether_google_took_the_access_back(client):
    assert client["signOutRevoked"] == ["/?why=signedout"]
    assert client["signOutNotRevoked"] == ["/?why=notrevoked"]
