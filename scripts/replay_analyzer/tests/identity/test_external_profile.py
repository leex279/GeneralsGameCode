from datetime import datetime, timezone

import pytest
from generals_replay_analyzer.db.models import Player
from generals_replay_analyzer.identity.query import PlayerIndexQuery, PlayerQueryService
from generals_replay_analyzer.identity.service import PlayerIdentityService


def test_external_profile_round_trips_on_canonical_player(identity_session_factory):
    now = datetime.now(timezone.utc)
    with identity_session_factory() as session:
        session.add(Player(public_id="00000000-0000-0000-0000-000000000001", display_name="Alpha", updated_at=now, created_at=now))
        session.commit()

    PlayerIdentityService(identity_session_factory).update_external_profile(
        "00000000-0000-0000-0000-000000000001", "https://example.com/players/alpha", "example"
    )
    profile = PlayerQueryService(identity_session_factory).list_players(PlayerIndexQuery()).items[0]
    assert profile.external_profile_url == "https://example.com/players/alpha"
    assert profile.external_profile_source == "example"


@pytest.mark.parametrize("url", ["http://example.com/player", "example.com/player", "javascript:alert(1)"])
def test_external_profile_requires_optional_https_public_url(identity_session_factory, url):
    with pytest.raises(ValueError, match="HTTPS"):
        PlayerIdentityService(identity_session_factory).validate_external_profile(url, "example")


def test_external_profile_source_is_required_with_url(identity_session_factory):
    with pytest.raises(ValueError, match="source"):
        PlayerIdentityService(identity_session_factory).validate_external_profile("https://example.com/p", None)
