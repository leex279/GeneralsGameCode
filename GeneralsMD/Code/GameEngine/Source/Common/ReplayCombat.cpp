/*
** Command & Conquer Generals Zero Hour(tm)
** Copyright 2025 Electronic Arts Inc.
**
** This program is free software: you can redistribute it and/or modify
** it under the terms of the GNU General Public License as published by
** the Free Software Foundation, either version 3 of the License, or
** (at your option) any later version.
*/

#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayCombat.h"

#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/ReplayTelemetry.h"
#include "Common/ThingTemplate.h"
#include "GameLogic/Damage.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Object.h"
#include "GameLogic/VictoryConditions.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <set>
#include <string>
#include <system_error>
#include <vector>

namespace
{
	struct ReplayCombatState
	{
		std::vector<ReplayPlayerTransitionType> playerTransitionStack;
		std::set<Int> terminalPlayers;
		std::vector<Int> enginePlayerIndices;
		Bool headerObserved = FALSE;
		Bool quitEarly = FALSE;
		Bool replayHeaderDesync = FALSE;
		Bool disconnectedSlots[MAX_SLOTS] = {};
		Bool crcMismatchObserved = FALSE;
		UnsignedInt crcMismatchFrame = 0;
	};

	ReplayCombatState s_state;

	UnsignedInt currentFrame()
	{
		return TheGameLogic != nullptr ? TheGameLogic->getFrame() : 0;
	}

	std::string jsonString(const char *value)
	{
		std::string result("\"");
		const UnsignedByte *cursor = reinterpret_cast<const UnsignedByte *>(value != nullptr ? value : "");
		while (*cursor != 0)
		{
			const UnsignedByte character = *cursor++;
			switch (character)
			{
				case '"': result += "\\\""; break;
				case '\\': result += "\\\\"; break;
				case '\b': result += "\\b"; break;
				case '\f': result += "\\f"; break;
				case '\n': result += "\\n"; break;
				case '\r': result += "\\r"; break;
				case '\t': result += "\\t"; break;
				default:
					if (character >= 0x20 && character < 0x80)
					{
						result.push_back(static_cast<char>(character));
					}
					else if (character >= 0x80)
					{
						result.push_back(static_cast<char>(0xc0 | (character >> 6)));
						result.push_back(static_cast<char>(0x80 | (character & 0x3f)));
					}
					break;
			}
		}
		result.push_back('"');
		return result;
	}

	Bool jsonNumber(Real value, std::string &result)
	{
		if (!std::isfinite(static_cast<double>(value)))
		{
			ReplayTelemetry::fail("nonfinite_combat_number", "combat observation contains a nonfinite number");
			return FALSE;
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, 9);
		if (converted.ec != std::errc())
		{
			ReplayTelemetry::fail("combat_number_format_failed", "could not serialize a combat number");
			return FALSE;
		}
		result.assign(buffer, converted.ptr);
		return TRUE;
	}

	std::string nullableInt(Bool present, Int value)
	{
		return present ? std::to_string(value) : "null";
	}

	std::string nullableObjectId(ObjectID value)
	{
		return value != INVALID_ID ? std::to_string(static_cast<UnsignedInt>(value)) : "null";
	}

	std::string intArray(const std::vector<Int> &values)
	{
		std::string result("[");
		for (size_t index = 0; index < values.size(); ++index)
		{
			if (index != 0)
			{
				result.push_back(',');
			}
			result += std::to_string(values[index]);
		}
		result.push_back(']');
		return result;
	}

	std::string disconnectedSlotArray()
	{
		std::string result("[");
		Bool first = TRUE;
		for (Int slot = 0; slot < MAX_SLOTS; ++slot)
		{
			if (!s_state.disconnectedSlots[slot])
			{
				continue;
			}
			if (!first)
			{
				result.push_back(',');
			}
			result += std::to_string(slot);
			first = FALSE;
		}
		result.push_back(']');
		return result;
	}

	const char *deathTypeName(DeathType deathType)
	{
		static const char *const names[] = {
			"NORMAL", "NONE", "CRUSHED", "BURNED", "EXPLODED", "POISONED", "TOPPLED", "FLOODED",
			"SUICIDED", "LASERED", "DETONATED", "SPLATTED", "POISONED_BETA", "EXTRA_2", "EXTRA_3",
			"EXTRA_4", "EXTRA_5", "EXTRA_6", "EXTRA_7", "EXTRA_8", "POISONED_GAMMA"
		};
		const Int index = static_cast<Int>(deathType);
		return index >= 0 && index < DEATH_NUM_TYPES ? names[index] : nullptr;
	}

	Bool objectPlayerIndex(const Object *object, Int &playerIndex)
	{
		const Player *player = object != nullptr ? object->getControllingPlayer() : nullptr;
		if (player == nullptr)
		{
			return FALSE;
		}
		playerIndex = player->getPlayerIndex();
		return TRUE;
	}

	Bool locationJson(const Object *object, std::string &result)
	{
		if (object == nullptr || object->getPosition() == nullptr)
		{
			ReplayTelemetry::fail("combat_location_unavailable", "combat target location is unavailable");
			return FALSE;
		}
		const Coord3D *position = object->getPosition();
		std::string x;
		std::string y;
		std::string z;
		if (!jsonNumber(position->x, x) || !jsonNumber(position->y, y) || !jsonNumber(position->z, z))
		{
			return FALSE;
		}
		result = "{\"x\":" + x + ",\"y\":" + y + ",\"z\":" + z + "}";
		return TRUE;
	}

	Bool healthNumbers(Real attempted, Real calculated, Real applied, Real priorHealth, Real newHealth,
		std::string &attemptedJson, std::string &calculatedJson, std::string &appliedJson,
		std::string &priorJson, std::string &newJson)
	{
		return jsonNumber(attempted, attemptedJson) && jsonNumber(calculated, calculatedJson)
			&& jsonNumber(applied, appliedJson) && jsonNumber(priorHealth, priorJson)
			&& jsonNumber(newHealth, newJson);
	}

	const char *terminalReason(Bool cleanShutdown, Bool replayTruncated, Bool crcMismatch)
	{
		if (crcMismatch)
		{
			return "crc_mismatch";
		}
		if (replayTruncated)
		{
			return "replay_truncated";
		}
		if (!cleanShutdown || s_state.quitEarly || s_state.replayHeaderDesync)
		{
			return "interrupted";
		}
		return "clean_completion";
	}

	Bool buildEnginePlayerDomain(std::vector<Int> &indices, std::vector<Int> &winners, std::vector<Int> &losers)
	{
		if (ThePlayerList == nullptr)
		{
			if (!s_state.enginePlayerIndices.empty())
			{
				indices = s_state.enginePlayerIndices;
				return TRUE;
			}
			ReplayTelemetry::fail("outcome_players_unavailable", "full engine player state is unavailable");
			return FALSE;
		}
		std::set<Int> unique;
		for (Int index = 0; index < ThePlayerList->getPlayerCount(); ++index)
		{
			Player *player = ThePlayerList->getNthPlayer(index);
			if (player == nullptr)
			{
				continue;
			}
			const Int playerIndex = player->getPlayerIndex();
			if (playerIndex < 0 || !unique.insert(playerIndex).second)
			{
				ReplayTelemetry::fail("outcome_players_invalid", "full engine player state has an invalid player index");
				return FALSE;
			}
			indices.push_back(playerIndex);
			if (TheVictoryConditions != nullptr && TheVictoryConditions->hasAchievedVictory(player))
			{
				winners.push_back(playerIndex);
			}
			if (TheVictoryConditions != nullptr && TheVictoryConditions->hasBeenDefeated(player))
			{
				losers.push_back(playerIndex);
			}
		}
		if (indices.empty())
		{
			ReplayTelemetry::fail("outcome_players_unavailable", "full engine player state contains no players at completion");
			return FALSE;
		}
		std::sort(indices.begin(), indices.end());
		std::sort(winners.begin(), winners.end());
		std::sort(losers.begin(), losers.end());
		if (!s_state.enginePlayerIndices.empty() && indices != s_state.enginePlayerIndices)
		{
			ReplayTelemetry::fail("outcome_players_changed", "full engine player domain changed after initialization");
			return FALSE;
		}
		return TRUE;
	}

	std::string terminalFields(Bool cleanShutdown, Bool replayTruncated, Bool crcMismatch)
	{
		return "\"terminal_reason\":" + jsonString(terminalReason(cleanShutdown, replayTruncated, crcMismatch))
			+ ",\"quit_early\":" + (s_state.quitEarly ? "true" : "false")
			+ ",\"replay_header_desync\":" + (s_state.replayHeaderDesync ? "true" : "false")
			+ ",\"replay_header_disconnected_slots\":" + disconnectedSlotArray()
			+ ",\"crc_mismatch\":" + (crcMismatch ? "true" : "false")
			+ ",\"crc_mismatch_frame\":"
			+ (crcMismatch && s_state.crcMismatchObserved ? std::to_string(s_state.crcMismatchFrame) : "null")
			+ ",\"clean_shutdown\":" + (cleanShutdown ? "true" : "false");
	}
}

ReplayPlayerTransitionScope::ReplayPlayerTransitionScope(ReplayPlayerTransitionType type)
{
	ReplayCombat::pushPlayerTransition(type);
}

ReplayPlayerTransitionScope::~ReplayPlayerTransitionScope()
{
	ReplayCombat::popPlayerTransition();
}

void ReplayCombat::reset()
{
	s_state = ReplayCombatState();
}

void ReplayCombat::observeReplayHeader(const RecorderClass::ReplayHeader &header)
{
	s_state.headerObserved = TRUE;
	s_state.quitEarly = header.quitEarly;
	s_state.replayHeaderDesync = header.desyncGame;
	for (Int slot = 0; slot < MAX_SLOTS; ++slot)
	{
		s_state.disconnectedSlots[slot] = header.playerDiscons[slot];
	}
}

void ReplayCombat::initialize()
{
	std::vector<Int> domain;
	std::vector<Int> ignoredWinners;
	std::vector<Int> ignoredLosers;
	if (buildEnginePlayerDomain(domain, ignoredWinners, ignoredLosers))
	{
		// TheSuperHackers @feature Leex 20/08/2026 Freeze the exact initialized player domain for terminal output even during teardown. (#TBD)
		s_state.enginePlayerIndices = domain;
	}
}

void ReplayCombat::observeCRCMismatch(UnsignedInt frame)
{
	if (!s_state.crcMismatchObserved)
	{
		s_state.crcMismatchObserved = TRUE;
		s_state.crcMismatchFrame = frame;
	}
}

void ReplayCombat::observeDamage(const Object *victim, const DamageInfo *damageInfo,
	Real priorHealth, Real newHealth)
{
	if (!ReplayTelemetry::isInitialized() || victim == nullptr || damageInfo == nullptr)
	{
		return;
	}
	const Real calculatedAmount = damageInfo->out.m_actualDamageDealt;
	const Real appliedAmount = priorHealth - newHealth;
	if (!(appliedAmount > 0.0f))
	{
		return;
	}
	const char *damageType = DamageTypeFlags::getNameFromSingleBit(static_cast<Int>(damageInfo->in.m_damageType));
	const char *deathType = deathTypeName(damageInfo->in.m_deathType);
	if (damageType == nullptr || deathType == nullptr)
	{
		ReplayTelemetry::fail("combat_type_invalid", "combat observation contains an invalid damage or death type");
		return;
	}
	std::string attemptedJson;
	std::string calculatedJson;
	std::string appliedJson;
	std::string priorJson;
	std::string newJson;
	std::string location;
	if (!healthNumbers(damageInfo->in.m_amount, calculatedAmount, appliedAmount, priorHealth, newHealth,
		attemptedJson, calculatedJson, appliedJson, priorJson, newJson) || !locationJson(victim, location))
	{
		return;
	}
	Int victimPlayer = 0;
	const Bool hasVictimPlayer = objectPlayerIndex(victim, victimPlayer);
	const Object *attacker = damageInfo->in.m_sourceID != INVALID_ID && TheGameLogic != nullptr
		? TheGameLogic->findObjectByID(damageInfo->in.m_sourceID) : nullptr;
	Int attackerPlayer = 0;
	const Bool hasAttackerPlayer = objectPlayerIndex(attacker, attackerPlayer);
	const ThingTemplate *attackerTemplate = damageInfo->in.m_sourceTemplate;
	const std::string payload = "{\"victim_object_id\":" + std::to_string(static_cast<UnsignedInt>(victim->getID()))
		+ ",\"victim_player_index\":" + nullableInt(hasVictimPlayer, victimPlayer)
		+ ",\"attacker_object_id\":" + nullableObjectId(damageInfo->in.m_sourceID)
		+ ",\"attacker_player_index\":" + nullableInt(hasAttackerPlayer, attackerPlayer)
		+ ",\"attacker_template_name\":"
		+ (attackerTemplate != nullptr ? jsonString(attackerTemplate->getName().str()) : "null")
		+ ",\"weapon_name\":null"
		+ ",\"attempted_amount\":" + attemptedJson
		+ ",\"calculated_amount\":" + calculatedJson
		+ ",\"applied_amount\":" + appliedJson
		+ ",\"prior_health\":" + priorJson
		+ ",\"new_health\":" + newJson
		+ ",\"damage_type_id\":" + std::to_string(static_cast<Int>(damageInfo->in.m_damageType))
		+ ",\"damage_type\":" + jsonString(damageType)
		+ ",\"death_type_id\":" + std::to_string(static_cast<Int>(damageInfo->in.m_deathType))
		+ ",\"death_type\":" + jsonString(deathType)
		+ ",\"location\":" + location
		+ ",\"killing_blow\":" + (newHealth <= 0.0f && priorHealth > 0.0f ? "true}" : "false}");
	ReplayTelemetry::emit(currentFrame(), "damage_applied", AsciiString(payload.c_str()));
}

void ReplayCombat::observeHealing(const Object *target, const DamageInfo *damageInfo,
	Real priorHealth, Real newHealth)
{
	if (!ReplayTelemetry::isInitialized() || target == nullptr || damageInfo == nullptr)
	{
		return;
	}
	const Real appliedAmount = newHealth - priorHealth;
	if (!(appliedAmount > 0.0f))
	{
		return;
	}
	std::string attemptedJson;
	std::string calculatedJson;
	std::string appliedJson;
	std::string priorJson;
	std::string newJson;
	std::string location;
	if (!healthNumbers(damageInfo->in.m_amount, damageInfo->out.m_actualDamageDealt, appliedAmount,
		priorHealth, newHealth, attemptedJson, calculatedJson, appliedJson, priorJson, newJson)
		|| !locationJson(target, location))
	{
		return;
	}
	Int targetPlayer = 0;
	const Bool hasTargetPlayer = objectPlayerIndex(target, targetPlayer);
	const Object *source = damageInfo->in.m_sourceID != INVALID_ID && TheGameLogic != nullptr
		? TheGameLogic->findObjectByID(damageInfo->in.m_sourceID) : nullptr;
	Int sourcePlayer = 0;
	const Bool hasSourcePlayer = objectPlayerIndex(source, sourcePlayer);
	const std::string payload = "{\"target_object_id\":" + std::to_string(static_cast<UnsignedInt>(target->getID()))
		+ ",\"target_player_index\":" + nullableInt(hasTargetPlayer, targetPlayer)
		+ ",\"source_object_id\":" + nullableObjectId(damageInfo->in.m_sourceID)
		+ ",\"source_player_index\":" + nullableInt(hasSourcePlayer, sourcePlayer)
		+ ",\"attempted_amount\":" + attemptedJson
		+ ",\"calculated_amount\":" + calculatedJson
		+ ",\"applied_amount\":" + appliedJson
		+ ",\"prior_health\":" + priorJson
		+ ",\"new_health\":" + newJson
		+ ",\"location\":" + location + "}";
	ReplayTelemetry::emit(currentFrame(), "healing_applied", AsciiString(payload.c_str()));
}

void ReplayCombat::observeVeterancy(const Object *object, VeterancyLevel previousLevel, VeterancyLevel newLevel)
{
	if (!ReplayTelemetry::isInitialized() || object == nullptr || previousLevel == newLevel
		|| previousLevel < LEVEL_FIRST || previousLevel > LEVEL_LAST || newLevel < LEVEL_FIRST || newLevel > LEVEL_LAST)
	{
		return;
	}
	Int owner = 0;
	const Bool hasOwner = objectPlayerIndex(object, owner);
	const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
		+ ",\"owner_player_index\":" + nullableInt(hasOwner, owner)
		+ ",\"previous_level_id\":" + std::to_string(static_cast<Int>(previousLevel))
		+ ",\"previous_level\":" + jsonString(TheVeterancyNames[previousLevel])
		+ ",\"new_level_id\":" + std::to_string(static_cast<Int>(newLevel))
		+ ",\"new_level\":" + jsonString(TheVeterancyNames[newLevel]) + "}";
	ReplayTelemetry::emit(currentFrame(), "veterancy_changed", AsciiString(payload.c_str()));
}

void ReplayCombat::observePlayerTerminalTransition(const Player *player)
{
	if (!ReplayTelemetry::isInitialized() || player == nullptr || s_state.playerTransitionStack.empty())
	{
		return;
	}
	const Int playerIndex = player->getPlayerIndex();
	if (!s_state.terminalPlayers.insert(playerIndex).second)
	{
		return;
	}
	const ReplayPlayerTransitionType transition = s_state.playerTransitionStack.back();
	const Int slot = ThePlayerList != nullptr ? ThePlayerList->getSlotIndex(playerIndex) : -1;
	const Bool disconnected = transition == REPLAY_PLAYER_SURRENDERED && slot >= 0 && slot < MAX_SLOTS
		&& s_state.disconnectedSlots[slot];
	const char *eventType = "player_defeated";
	const char *newStatus = "defeated";
	const char *source = transition == REPLAY_PLAYER_SCRIPT_DEFEATED ? "script_action" : "victory_conditions";
	if (transition == REPLAY_PLAYER_SURRENDERED)
	{
		eventType = disconnected ? "player_disconnected" : "player_surrendered";
		newStatus = disconnected ? "disconnected" : "surrendered";
		source = disconnected ? "replay_header_disconnect_plus_executed_surrender" : "replay_command";
	}
	const std::string payload = "{\"player_index\":" + std::to_string(playerIndex)
		+ ",\"previous_status\":\"active\",\"new_status\":" + jsonString(newStatus)
		+ ",\"source\":" + jsonString(source)
		+ ",\"replay_slot_index\":" + (slot >= 0 && slot < MAX_SLOTS ? std::to_string(slot) : "null") + "}";
	ReplayTelemetry::emit(currentFrame(), eventType, AsciiString(payload.c_str()));
}

void ReplayCombat::emitMatchOutcome(UnsignedInt finalFrame, Bool cleanShutdown, Bool replayTruncated, Bool crcMismatch)
{
	if (!ReplayTelemetry::isInitialized())
	{
		return;
	}
	if (crcMismatch && !s_state.crcMismatchObserved)
	{
		ReplayTelemetry::fail("crc_mismatch_frame_unavailable", "CRC mismatch completion has no authoritative mismatch frame");
		return;
	}
	std::vector<Int> domain;
	std::vector<Int> winners;
	std::vector<Int> losers;
	if (!buildEnginePlayerDomain(domain, winners, losers))
	{
		return;
	}
	const Bool decided = ThePlayerList != nullptr && TheVictoryConditions != nullptr
		&& TheVictoryConditions->getEndFrame() > 0 && !winners.empty();
	if (!decided)
	{
		winners.clear();
		losers.clear();
	}
	const std::string payload = std::string("{\"status\":") + (decided ? "\"decided\"" : "\"unknown\"")
		+ ",\"source\":" + (decided ? std::string("\"victory_conditions\"") : std::string("\"unavailable\""))
		+ ",\"winner_player_indices\":" + intArray(winners)
		+ ",\"loser_player_indices\":" + intArray(losers)
		+ ",\"engine_player_indices\":" + intArray(domain)
		+ "," + terminalFields(cleanShutdown, replayTruncated, crcMismatch) + "}";
	ReplayTelemetry::emit(finalFrame, "match_outcome", AsciiString(payload.c_str()));
}

AsciiString ReplayCombat::completionFieldsJson(Bool cleanShutdown, Bool replayTruncated, Bool crcMismatch)
{
	const std::string payload = "\"crc_mismatch_frame\":"
		+ (crcMismatch && s_state.crcMismatchObserved ? std::to_string(s_state.crcMismatchFrame) : "null")
		+ ",\"quit_early\":" + (s_state.quitEarly ? "true" : "false")
		+ ",\"replay_header_desync\":" + (s_state.replayHeaderDesync ? "true" : "false")
		+ ",\"replay_header_disconnected_slots\":" + disconnectedSlotArray();
	return AsciiString(payload.c_str());
}

void ReplayCombat::pushPlayerTransition(ReplayPlayerTransitionType type)
{
	s_state.playerTransitionStack.push_back(type);
}

void ReplayCombat::popPlayerTransition()
{
	if (!s_state.playerTransitionStack.empty())
	{
		s_state.playerTransitionStack.pop_back();
	}
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
