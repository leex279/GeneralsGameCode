"""Fail-closed extraction of server-rendered Strata profile and match pages."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .contracts import MatchDocument, MatchParticipantDocument, ProfileAliasDocument, ProfileDocument
from .normalization import InvalidQueryNameError, normalize_query_name

_PLAYER_PATH = re.compile(r"^/zh/player/(\d+)$")
_MAP_PATH = re.compile(r"^/zh/map/(\d+)$")
_DURATION = re.compile(r"^(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?$")
_OCCURRENCE_COUNT = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)$")
_PLAYED = re.compile(
    r"Played\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+(\d{2}:\d{2})\s+[–-]\s+(\d{2}:\d{2})\s+GMT"
)


class SourceContractError(ValueError):
    """Raised when source HTML lacks the structural evidence the resolver requires."""

    code = "source_contract_changed"


def _contract(message: str) -> SourceContractError:
    return SourceContractError(message)


def _title(soup: BeautifulSoup) -> str:
    if soup.title is None:
        raise _contract("page title is missing")
    return soup.title.get_text(" ", strip=True)


def _exact_text_tag(soup: BeautifulSoup, value: str, names: tuple[str, ...] = ("p", "span")) -> Tag | None:
    for tag in soup.find_all(names):
        if tag.get_text(" ", strip=True) == value:
            return tag
    return None


def _label_value(soup: BeautifulSoup, label: str) -> str:
    label_tag = _exact_text_tag(soup, label, ("p", "span"))
    if label_tag is None or label_tag.parent is None:
        raise _contract(f"match field '{label}' is missing")
    for sibling in label_tag.find_next_siblings():
        text = sibling.get_text(" ", strip=True)
        if text:
            return text
    raise _contract(f"match field '{label}' has no value")


def _optional_label_value(soup: BeautifulSoup, label: str) -> str | None:
    try:
        return _label_value(soup, label)
    except SourceContractError:
        return None


# TheSuperHackers @feature Leex 27/08/2026 Extract every Known Names chip instead of trusting the search-row label.
def extract_profile(html: str, expected_player_id: int) -> ProfileDocument:
    """Extract one complete profile or fail when the known source contract is absent."""
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    suffix = " | Strata"
    if not title.endswith(suffix) or title.startswith("Match #"):
        raise _contract("profile title does not match the Strata profile contract")
    title_name = title[: -len(suffix)]
    if not title_name:
        raise _contract("profile title has no player name")
    heading = _exact_text_tag(soup, "Known Names", ("p",))
    if heading is None or heading.parent is None:
        raise _contract("Known Names section is missing")
    aliases: list[ProfileAliasDocument] = []
    alias_positions: dict[str, int] = {}
    for chip in heading.parent.find_all("div"):
        spans = chip.find_all("span", recursive=False)
        if len(spans) != 2:
            continue
        name = spans[0].get_text(" ", strip=True)
        count_text = spans[1].get_text(" ", strip=True)
        if not name or _OCCURRENCE_COUNT.fullmatch(count_text) is None:
            continue
        count = int(count_text.replace(",", ""))
        try:
            normalized = normalize_query_name(name)
        except InvalidQueryNameError as error:
            raise _contract("Known Names contains an invalid alias") from error
        existing_position = alias_positions.get(name)
        if existing_position is not None:
            existing = aliases[existing_position]
            aliases[existing_position] = replace(
                existing,
                occurrence_count=existing.occurrence_count + count,
            )
            continue
        alias_positions[name] = len(aliases)
        aliases.append(
            ProfileAliasDocument(
                name_raw=normalized.raw,
                name_nfc=normalized.nfc,
                name_casefold=normalized.casefold,
                occurrence_count=count,
                source_rank=len(aliases),
            )
        )
    if not aliases:
        raise _contract("Known Names section has no valid alias chips")
    if title_name not in alias_positions:
        raise _contract("profile title is not present in Known Names")
    most_known_name = max(aliases, key=lambda alias: alias.occurrence_count).name_raw
    return ProfileDocument(
        player_id=expected_player_id,
        profile_url=f"https://strata.gamereplays.org/zh/player/{expected_player_id}",
        most_known_name=most_known_name,
        aliases=tuple(aliases),
    )


def _duration_seconds(value: str) -> int:
    matched = _DURATION.fullmatch(value)
    if matched is None or not any(matched.groups()):
        raise _contract("match duration is invalid")
    hours, minutes, seconds = (int(item or 0) for item in matched.groups())
    if seconds >= 60:
        raise _contract("match duration is outside valid bounds")
    return hours * 3600 + minutes * 60 + seconds


def _played_times(soup: BeautifulSoup) -> tuple[datetime, datetime]:
    label = _exact_text_tag(soup, "Played", ("p", "span"))
    if label is None or label.parent is None:
        raise _contract("match Played field is missing")
    text = label.parent.get_text(" ", strip=True)
    matched = _PLAYED.search(text)
    if matched is None:
        raise _contract("match Played field is invalid")
    day, start_text, end_text = matched.groups()
    start = datetime.strptime(f"{day} {start_text}", "%b %d, %Y %H:%M").replace(tzinfo=UTC)
    end = datetime.strptime(f"{day} {end_text}", "%b %d, %Y %H:%M").replace(tzinfo=UTC)
    if end < start:
        end += timedelta(days=1)
    return start, end


def _approved_replay_url(container: Tag) -> str | None:
    for link in container.find_all("a", href=True):
        href = str(link["href"])
        parsed = urlparse(href)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "matchdata.playgenerals.online"
            and parsed.path.startswith("/replays/")
            and parsed.path.endswith("_replay.rep")
            and not parsed.query
            and not parsed.fragment
        ):
            return href
    return None


def _participants(soup: BeautifulSoup) -> tuple[MatchParticipantDocument, ...]:
    participants: list[MatchParticipantDocument] = []
    seen: set[int] = set()
    for link in soup.find_all("a", href=True):
        parsed = urlparse(str(link["href"]))
        matched = _PLAYER_PATH.fullmatch(parsed.path)
        if matched is None or parsed.hostname not in {None, "strata.gamereplays.org"}:
            continue
        player_id = int(matched.group(1))
        if player_id in seen:
            continue
        container = link.find_parent("div", attrs={"x-data": re.compile("expanded")})
        if container is None:
            raise _contract("match participant row has no expandable container")
        button = container.find("button")
        if button is None or link.find_parent("button") is not button:
            raise _contract("match participant row has no summary button")
        faction_tag = link.find_next("span")
        badge = button.select_one("[data-flux-badge]")
        displayed_name = link.get_text(" ", strip=True)
        faction = "" if faction_tag is None else faction_tag.get_text(" ", strip=True)
        result = "" if badge is None else badge.get_text(" ", strip=True)
        if not displayed_name or not faction or result not in {"Won", "Lost", "Draw"}:
            raise _contract("match participant row is incomplete")
        seen.add(player_id)
        participants.append(
            MatchParticipantDocument(
                source_rank=len(participants),
                player_id=player_id,
                displayed_name=displayed_name,
                faction=faction,
                result=result,
                replay_url=_approved_replay_url(container),
            )
        )
    if len(participants) < 2:
        raise _contract("match page contains fewer than two participant rows")
    return tuple(participants)


def _map_id(soup: BeautifulSoup) -> int | None:
    for link in soup.find_all("a", href=True):
        matched = _MAP_PATH.fullmatch(urlparse(str(link["href"])).path)
        if matched is not None:
            return int(matched.group(1))
    return None


# TheSuperHackers @feature Leex 27/08/2026 Bind match metadata and participant IDs from one validated server-rendered page.
def extract_match(html: str, expected_match_id: int) -> MatchDocument:
    """Extract one Strata match page and reject partial or mismatched documents."""
    soup = BeautifulSoup(html, "html.parser")
    if _title(soup) != f"Match #{expected_match_id} | Strata":
        raise _contract("match title does not contain the expected numeric ID")
    played_start, played_end = _played_times(soup)
    cash_text = _optional_label_value(soup, "Starting Cash")
    starting_cash = None
    if cash_text is not None:
        digits = cash_text.replace("$", "").replace(",", "")
        if not digits.isdecimal():
            raise _contract("Starting Cash is invalid")
        starting_cash = int(digits)
    return MatchDocument(
        match_id=expected_match_id,
        match_url=f"https://strata.gamereplays.org/zh/match/{expected_match_id}",
        played_start_utc=played_start,
        played_end_utc=played_end,
        map_id=_map_id(soup),
        map_name=_label_value(soup, "Map"),
        match_type=_label_value(soup, "Match Type"),
        duration_seconds=_duration_seconds(_label_value(soup, "Duration")),
        starting_cash=starting_cash,
        game_version=_optional_label_value(soup, "Game Version"),
        data_pack=_optional_label_value(soup, "Data Pack"),
        participants=_participants(soup),
    )
