def test_second_active_run_for_same_issue_is_rejected(components):
    store, _, _ = components
    first = store.enqueue(42, "t", "https://x/42", "body")
    second = store.enqueue(42, "t", "https://x/42", "body")
    assert first is not None
    assert second is None


def test_relabel_after_run_closes_creates_new_run(components):
    store, _, _ = components
    first = store.enqueue(42, "t", "https://x/42", "body")
    store.update(first, state="pr_open")  # run no longer active
    second = store.enqueue(42, "t", "https://x/42", "body")
    assert second is not None
    assert second != first


def test_active_states_cover_queued_and_running(components):
    store, _, _ = components
    store.enqueue(7, "t", "https://x/7", "body")
    # queued blocks a second run
    assert store.enqueue(7, "t", "https://x/7", "body") is None
