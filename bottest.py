def test_call_claude():
    from bot import call_claude
    response = call_claude("Hello, how are you?")
    assert "Hello" in response