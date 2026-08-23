#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayScoreKeeper.h"

#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/Recorder.h"
#include "Common/ReplayTelemetry.h"
#include "Common/ScoreKeeper.h"
#include "GameLogic/GameLogic.h"
#include "GameNetwork/GameInfo.h"

#include <algorithm>
#include <set>
#include <string>
#include <vector>

namespace
{
	struct ReplayScoreKeeperState
	{
		Bool initialized = FALSE;
		Bool terminalWritten = FALSE;
		std::vector<Int> playerIndices;
	};

	ReplayScoreKeeperState s_state;

	Player *findResolvedPlayer(Int playerIndex)
	{
		if (ThePlayerList == nullptr)
		{
			return nullptr;
		}
		for (Int index = 0; index < ThePlayerList->getPlayerCount(); ++index)
		{
			Player *player = ThePlayerList->getNthPlayer(index);
			if (player != nullptr && player->getPlayerIndex() == playerIndex)
			{
				return player;
			}
		}
		return nullptr;
	}

	std::string playerJson(Player *player)
	{
		ScoreKeeper *scoreKeeper = player->getScoreKeeper();
		return "{\"player_index\":" + std::to_string(player->getPlayerIndex())
			+ ",\"money_earned\":" + std::to_string(scoreKeeper->getTotalMoneyEarned())
			+ ",\"money_spent\":" + std::to_string(scoreKeeper->getTotalMoneySpent())
			+ ",\"units_built\":" + std::to_string(scoreKeeper->getTotalUnitsBuilt())
			+ ",\"units_lost\":" + std::to_string(scoreKeeper->getTotalUnitsLost())
			+ ",\"units_destroyed\":" + std::to_string(scoreKeeper->getTotalUnitsDestroyed())
			+ ",\"buildings_built\":" + std::to_string(scoreKeeper->getTotalBuildingsBuilt())
			+ ",\"buildings_lost\":" + std::to_string(scoreKeeper->getTotalBuildingsLost())
			+ ",\"buildings_destroyed\":" + std::to_string(scoreKeeper->getTotalBuildingsDestroyed())
			+ ",\"tech_buildings_captured\":" + std::to_string(scoreKeeper->getTotalTechBuildingsCaptured())
			+ ",\"faction_buildings_captured\":"
			+ std::to_string(scoreKeeper->getTotalFactionBuildingsCaptured()) + "}";
	}
}

void ReplayScoreKeeper::reset()
{
	s_state = ReplayScoreKeeperState();
}

void ReplayScoreKeeper::initialize()
{
	if (s_state.initialized || !ReplayTelemetry::isInitialized())
	{
		return;
	}
	if (TheRecorder == nullptr || TheRecorder->getGameInfo() == nullptr || ThePlayerList == nullptr)
	{
		ReplayTelemetry::fail("scorekeeper_players_unavailable", "resolved replay players are unavailable for ScoreKeeper telemetry");
		return;
	}

	std::set<Int> unique;
	const GameInfo *gameInfo = TheRecorder->getGameInfo();
	for (Int slotIndex = 0; slotIndex < MAX_SLOTS; ++slotIndex)
	{
		const GameSlot *slot = gameInfo->getConstSlot(slotIndex);
		if (slot == nullptr || !slot->isOccupied())
		{
			continue;
		}
		Player *player = ThePlayerList->getPlayerFromSlotIndex(slotIndex);
		if (player == nullptr)
		{
			continue;
		}
		if (!unique.insert(player->getPlayerIndex()).second)
		{
			ReplayTelemetry::fail("scorekeeper_players_invalid", "resolved replay slots contain a duplicate ScoreKeeper player");
			return;
		}
		s_state.playerIndices.push_back(player->getPlayerIndex());
	}
	if (s_state.playerIndices.empty())
	{
		ReplayTelemetry::fail("scorekeeper_players_unavailable", "no resolved occupied replay player has ScoreKeeper telemetry");
		return;
	}
	std::sort(s_state.playerIndices.begin(), s_state.playerIndices.end());
	s_state.initialized = TRUE;
}

void ReplayScoreKeeper::writeTerminalSnapshot(Int frame)
{
	if (!ReplayTelemetry::isInitialized() || s_state.terminalWritten)
	{
		return;
	}
	s_state.terminalWritten = TRUE;
	if (!s_state.initialized || TheGameLogic == nullptr || ThePlayerList == nullptr)
	{
		ReplayTelemetry::fail("scorekeeper_state_unavailable", "terminal ScoreKeeper state is unavailable");
		return;
	}

	std::string players = "[";
	for (size_t index = 0; index < s_state.playerIndices.size(); ++index)
	{
		Player *player = findResolvedPlayer(s_state.playerIndices[index]);
		if (player == nullptr)
		{
			ReplayTelemetry::fail("scorekeeper_players_changed", "resolved ScoreKeeper player domain changed before completion");
			return;
		}
		if (index != 0)
		{
			players.push_back(',');
		}
		players += playerJson(player);
	}
	players.push_back(']');

	const std::string payload = "{\"source\":\"Player::getScoreKeeper\""
		",\"player_scope\":\"resolved_occupied_replay_slots\""
		",\"scoring_enabled\":" + std::string(TheGameLogic->isScoringEnabled() ? "true" : "false")
		+ ",\"players\":" + players + "}";
	ReplayTelemetry::emit(static_cast<UnsignedInt>(frame), "scorekeeper_snapshot", AsciiString(payload.c_str()));
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
