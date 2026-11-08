from muselinux.probe import SCOPE_MESSAGE, classify


def test_classify():
    assert classify(200, {"result": {}}) == "allowed"
    assert classify(403, SCOPE_MESSAGE) == "scope_refused"
    assert classify(403, "other") == "forbidden_other"
    assert classify(404, None) == "http_404"
    assert classify(None, None) == "transport_error"
