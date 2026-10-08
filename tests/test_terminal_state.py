from app.service import session_is_terminal


def test_running_and_working_is_not_terminal():
    assert not session_is_terminal("running", "working")


def test_running_finished_is_terminal():
    assert session_is_terminal("running", "finished")


def test_running_waiting_for_user_is_terminal():
    assert session_is_terminal("running", "waiting_for_user")


def test_exit_error_suspended_are_terminal():
    assert session_is_terminal("exit", None)
    assert session_is_terminal("error", None)
    assert session_is_terminal("suspended", "usage_limit_exceeded")


def test_new_claimed_resuming_are_not_terminal():
    assert not session_is_terminal("new", None)
    assert not session_is_terminal("claimed", None)
    assert not session_is_terminal("resuming", None)
