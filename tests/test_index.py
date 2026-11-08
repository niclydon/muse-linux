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
    assert first is second
    assert len(index.list()) == 2
