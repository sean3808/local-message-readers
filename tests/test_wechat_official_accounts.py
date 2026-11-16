"""Official accounts (subscription / service accounts) are excluded from listings, search and recent."""

import pytest

from readers.wechat import is_official_account


@pytest.mark.parametrize("username", ["gh_0123abcd", "brandsessionholder", "brandservicesessionholder"])
def test_official_accounts_are_recognized(username):
    assert is_official_account(username)


@pytest.mark.parametrize("username", ["wxid_example", "123@chatroom", "filehelper", "example@openim", "someone"])
def test_people_and_groups_are_kept(username):
    assert not is_official_account(username)
