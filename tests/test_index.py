from muselinux.index import MAIN_ID, ChatIndex


def test_index_seeds_main_and_persists(tmp_path):
    path = tmp_path / "chats.json"
    index = ChatIndex(path)
    assert index.list()[0]["id"] == MAIN_ID
    chat = index.create("Research")
    assert chat["session_id"] == chat["id"]
    reloaded = ChatIndex(path)
    assert reloaded.get(chat["id"])["label"] == "Research"


def test_index_add_known_is_idempotent(tmp_path):
    index = ChatIndex(tmp_path / "chats.json")
    first = index.add_known("11111111-2222-3333-4444-555555555555", "Known")
    second = index.add_known("11111111-2222-3333-4444-555555555555", "Other")
    assert first == second
    assert second["label"] == "Known"
    assert len(index.list()) == 2


def test_two_processes_sharing_an_index_keep_each_others_chats(tmp_path):
    path = tmp_path / "chats.json"
    daemon_view = ChatIndex(path)
    service_view = ChatIndex(path)
    a = daemon_view.create("From the daemon")
    b = service_view.create("From the web service")
    ids = {c["id"] for c in ChatIndex(path).list()}
    assert {a["id"], b["id"]} <= ids
    # each live view sees the other's change without being rebuilt
    assert daemon_view.get(b["id"])["label"] == "From the web service"
    assert service_view.get(a["id"])["label"] == "From the daemon"


def test_remove_drops_side_chats_but_never_main(tmp_path):
    index = ChatIndex(tmp_path / "chats.json")
    chat = index.create("Scratch")
    assert index.remove(MAIN_ID) is False
    assert index.remove(chat["id"]) is True
    assert index.get(chat["id"]) is None
    assert index.remove(chat["id"]) is False
    assert [c["id"] for c in index.list()] == [MAIN_ID]


def test_touch_only_moves_forward(tmp_path):
    index = ChatIndex(tmp_path / "chats.json")
    chat = index.create("Busy")
    assert index.touch(chat["id"], 2000) is True
    assert index.touch(chat["id"], 1000) is False
    assert index.get(chat["id"])["last_activity_ms"] == 2000
    assert index.touch("no-such-chat", 3000) is False


def test_corrupt_index_file_degrades_to_main_only(tmp_path):
    path = tmp_path / "chats.json"
    path.write_text("{not json")
    index = ChatIndex(path)
    assert [c["id"] for c in index.list()] == [MAIN_ID]
    # and a write refuses to replace the damaged file with an empty one
    import pytest
    with pytest.raises(RuntimeError):
        index.create("Would clobber")
    assert path.read_text() == "{not json"
