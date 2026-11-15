from readers.teams.normalizer import derive_is_group, normalize_teams_message


def test_cache_one_byte_strings_preserve_non_ascii_characters():
    message = {
        "id": "1",
        "originalArrivalTime": 1700000000000,
        "content": b"<p>A\xa0B</p>",
        "imDisplayName": b"Jos\xe9",
    }
    degraded = []

    result = normalize_teams_message(message, "conversation", degraded=degraded)

    assert result["text"] == "A\u00a0B"
    assert result["sender"] == "Jos\u00e9"
    assert degraded == []


def test_cache_system_event_decodes_one_byte_content():
    message = {"id": "1", "originalArrivalTime": 1700000000000,
               "messageType": "ThreadActivity/TopicUpdate", "content": b"<value>Caf\xe9</value>"}
    result = normalize_teams_message(message, "conversation")
    assert result["text"] == 'Changed the group name to "Café"'


def test_cache_files_json_decodes_one_byte_filename():
    message = {"id": "1", "originalArrivalTime": 1700000000000,
               "properties": {"files": b'[{"fileName":"caf\xe9.txt"}]'}}
    degraded = []
    result = normalize_teams_message(message, "conversation", degraded=degraded)
    assert result["files"] == ["café.txt"]
    assert degraded == []


def test_cache_members_json_decodes_one_byte_characters():
    conversation = {"members": b'[{"id":"caf\xe9"},{"id":"b"},{"id":"c"}]'}
    assert derive_is_group(conversation) is True


def test_cache_whitespace_bytes_are_empty_for_deletion():
    message = {"id": "1", "originalArrivalTime": 1700000000000,
               "skypeeditedid": "1", "content": b" \xa0 "}
    assert normalize_teams_message(message, "conversation")["deleted"] is True
