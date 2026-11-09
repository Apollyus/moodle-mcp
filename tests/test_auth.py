import base64

import pytest

from moodle_mcp import auth

SITE = "https://moodle.example.cz"
PASSPORT = "0123456789abcdef0123456789abcdef"


def make_url(token="tok123", private="priv", site=SITE, passport=PASSPORT, scheme="moodlemobile"):
    payload = f"{auth.site_hash(site, passport)}:::{token}"
    if private:
        payload += f":::{private}"
    return f"{scheme}://token={base64.b64encode(payload.encode()).decode()}"


def test_parse_valid_token():
    t = auth.parse_launch_token(make_url(), PASSPORT, [SITE])
    assert (t.token, t.private_token) == ("tok123", "priv")


def test_parse_without_private_token():
    assert auth.parse_launch_token(make_url(private=None), PASSPORT, [SITE]).private_token is None


def test_hash_with_trailing_slash_and_wwwroot():
    url = make_url(site=SITE + "/")
    assert auth.parse_launch_token(url, PASSPORT, [None, SITE]).token == "tok123"


def test_custom_scheme_accepted():
    assert auth.parse_launch_token(make_url(scheme="vseapp"), PASSPORT, [SITE]).token == "tok123"


def test_wrong_passport_rejected():
    with pytest.raises(auth.LoginError, match="kontrolní součet"):
        auth.parse_launch_token(make_url(), "other", [SITE])


def test_wrong_site_rejected():
    with pytest.raises(auth.LoginError):
        auth.parse_launch_token(make_url(), PASSPORT, ["https://evil.example"])


@pytest.mark.parametrize("url", ["https://token=abc", "moodlemobile://token=", "moodlemobile://token=!!!"])
def test_garbage_rejected(url):
    with pytest.raises(auth.LoginError):
        auth.parse_launch_token(url, PASSPORT, [SITE])


def test_is_launch_redirect():
    assert auth.is_launch_redirect("moodlemobile://token=abc")
    assert not auth.is_launch_redirect("https://token=abc")


def test_find_token_url_in_html():
    html = '<a href="moodlemobile://token=QUJD%3D">open</a> https://x/token=1'
    assert auth.find_token_url(html) == "moodlemobile://token=QUJD%3D"


def test_site_from_rest_url():
    assert auth.site_from_rest_url("https://m.cz/webservice/rest/server.php") == "https://m.cz"


def test_write_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text('FOO="bar"\nMOODLE_TOKEN="old"\nMOODLE_URL="https://custom/ws"\n')
    auth.write_env(env, SITE, "new")
    assert env.read_text().splitlines() == ['FOO="bar"', 'MOODLE_TOKEN="new"', 'MOODLE_URL="https://custom/ws"']
    assert (env.stat().st_mode & 0o777) == 0o600


def test_write_env_creates_file(tmp_path):
    env = tmp_path / ".env"
    auth.write_env(env, SITE, "t")
    assert env.read_text() == f'MOODLE_TOKEN="t"\nMOODLE_URL="{SITE}/webservice/rest/server.php"\n'
