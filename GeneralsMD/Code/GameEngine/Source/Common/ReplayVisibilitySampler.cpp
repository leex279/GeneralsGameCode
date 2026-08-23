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

#include "Common/ReplayVisibilitySampler.h"

#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/Recorder.h"
#include "Common/ReplayEntityLifecycle.h"
#include "Common/ReplayTelemetry.h"
#include "Common/ThingTemplate.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Object.h"
#include "GameLogic/PartitionManager.h"
#include "GameNetwork/GameInfo.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <map>
#include <string>
#include <system_error>
#include <vector>

namespace
{
	const UnsignedInt SAMPLE_INTERVAL_FRAMES = 15;
	const size_t MAXIMUM_PAIRS_PER_PASS = 8192;

	enum VisibilityStatus
	{
		VISIBILITY_STATUS_UNSEEN,
		VISIBILITY_STATUS_CLEAR,
		VISIBILITY_STATUS_FOGGED,
		VISIBILITY_STATUS_SHROUDED
	};

	struct VisibilityState
	{
		VisibilityStatus status;
		Bool observedClear;
	};

	struct VisibilityPair
	{
		Int playerIndex;
		ObjectID objectId;

		Bool operator<(const VisibilityPair &other) const
		{
			return playerIndex < other.playerIndex
				|| (playerIndex == other.playerIndex && objectId < other.objectId);
		}
	};

	typedef std::map<VisibilityPair, VisibilityState> VisibilityStateMap;

	VisibilityStateMap s_visibilityStates;
	std::vector<VisibilityPair> s_cyclePairs;
	size_t s_cursor = 0;
	UnsignedInt s_cycleId = 0;
	Bool s_cycleActive = FALSE;

	std::string jsonString(const char *value)
	{
		std::string result("\"");
		const unsigned char *cursor = reinterpret_cast<const unsigned char *>(value != nullptr ? value : "");
		for (; *cursor != 0; ++cursor)
		{
			switch (*cursor)
			{
				case '\"': result += "\\\""; break;
				case '\\': result += "\\\\"; break;
				case '\b': result += "\\b"; break;
				case '\f': result += "\\f"; break;
				case '\n': result += "\\n"; break;
				case '\r': result += "\\r"; break;
				case '\t': result += "\\t"; break;
				default:
					if (*cursor < 0x20)
					{
						static const char digits[] = "0123456789abcdef";
						result += "\\u00";
						result.push_back(digits[(*cursor >> 4) & 0x0f]);
						result.push_back(digits[*cursor & 0x0f]);
					}
					else
					{
						result.push_back(static_cast<char>(*cursor));
					}
			}
		}
		result.push_back('\"');
		return result;
	}

	Bool jsonNumber(Real value, std::string &result)
	{
		if (!std::isfinite(static_cast<double>(value)))
		{
			ReplayTelemetry::fail("nonfinite_visibility_position", "visibility sample contains a nonfinite object position");
			return FALSE;
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, 9);
		if (converted.ec != std::errc())
		{
			ReplayTelemetry::fail("visibility_number_format_failed", "could not serialize a visibility position");
			return FALSE;
		}
		result.assign(buffer, converted.ptr);
		return TRUE;
	}

	Bool positionJson(const Coord3D &position, std::string &result)
	{
		std::string x;
		std::string y;
		std::string z;
		if (!jsonNumber(position.x, x) || !jsonNumber(position.y, y) || !jsonNumber(position.z, z))
		{
			return FALSE;
		}
		result = "{\"x\":" + x + ",\"y\":" + y + ",\"z\":" + z + "}";
		return TRUE;
	}

	const char *visibilityStatusName(VisibilityStatus status)
	{
		switch (status)
		{
			case VISIBILITY_STATUS_CLEAR: return "clear";
			case VISIBILITY_STATUS_FOGGED: return "fogged";
			case VISIBILITY_STATUS_SHROUDED: return "shrouded";
			default: return "unseen";
		}
	}

	VisibilityStatus visibilityStatus(CellShroudStatus status)
	{
		switch (status)
		{
			case CELLSHROUD_CLEAR: return VISIBILITY_STATUS_CLEAR;
			case CELLSHROUD_FOGGED: return VISIBILITY_STATUS_FOGGED;
			default: return VISIBILITY_STATUS_SHROUDED;
		}
	}

	std::vector<Int> resolvedOccupiedPlayerIndices()
	{
		std::vector<Int> playerIndices;
		GameInfo *gameInfo = TheRecorder != nullptr ? TheRecorder->getGameInfo() : nullptr;
		if (gameInfo == nullptr || ThePlayerList == nullptr)
		{
			return playerIndices;
		}
		for (Int slotIndex = 0; slotIndex < MAX_SLOTS; ++slotIndex)
		{
			const GameSlot *slot = gameInfo->getConstSlot(slotIndex);
			Player *player = slot != nullptr && slot->isOccupied()
				? ThePlayerList->getPlayerFromSlotIndex(slotIndex) : nullptr;
			if (player != nullptr)
			{
				playerIndices.push_back(player->getPlayerIndex());
			}
		}
		std::sort(playerIndices.begin(), playerIndices.end());
		playerIndices.erase(std::unique(playerIndices.begin(), playerIndices.end()), playerIndices.end());
		return playerIndices;
	}

	std::vector<ObjectID> liveObjectIds()
	{
		std::vector<ObjectID> objectIds;
		if (TheGameLogic == nullptr)
		{
			return objectIds;
		}
		for (Object *object = TheGameLogic->getFirstObject(); object != nullptr; object = object->getNextObject())
		{
			if (!object->isDestroyed())
			{
				objectIds.push_back(object->getID());
			}
		}
		std::sort(objectIds.begin(), objectIds.end());
		objectIds.erase(std::unique(objectIds.begin(), objectIds.end()), objectIds.end());
		return objectIds;
	}

	void beginCycle()
	{
		const std::vector<Int> playerIndices = resolvedOccupiedPlayerIndices();
		const std::vector<ObjectID> objectIds = liveObjectIds();
		s_cyclePairs.clear();
		s_cyclePairs.reserve(playerIndices.size() * objectIds.size());
		// TheSuperHackers @feature Leex 23/08/2026 Freeze each cycle as sorted integer identities so capped passes remain stable without retaining Object pointers. (#0)
		for (const Int playerIndex : playerIndices)
		{
			for (const ObjectID objectId : objectIds)
			{
				s_cyclePairs.push_back(VisibilityPair{ playerIndex, objectId });
			}
		}
		s_cursor = 0;
		s_cycleActive = TRUE;
	}

	void pruneDestroyedState()
	{
		for (VisibilityStateMap::iterator iterator = s_visibilityStates.begin(); iterator != s_visibilityStates.end();)
		{
			Object *object = TheGameLogic != nullptr ? TheGameLogic->findObjectByID(iterator->first.objectId) : nullptr;
			if (object == nullptr || object->isDestroyed())
			{
				iterator = s_visibilityStates.erase(iterator);
			}
			else
			{
				++iterator;
			}
		}
	}

	void samplePair(UnsignedInt frame, const VisibilityPair &pair)
	{
		Object *object = TheGameLogic->findObjectByID(pair.objectId);
		if (object == nullptr || object->isDestroyed() || ThePartitionManager == nullptr)
		{
			return;
		}
		ReplayEntityLifecycle::ensureObjectCreated(object);
		const Coord3D position = *object->getPosition();
		const VisibilityStatus status = visibilityStatus(
			ThePartitionManager->getShroudStatusForPlayer(pair.playerIndex, &position));
		const VisibilityStateMap::iterator previous = s_visibilityStates.find(pair);
		const VisibilityStatus previousStatus = previous != s_visibilityStates.end()
			? previous->second.status : VISIBILITY_STATUS_UNSEEN;
		const Bool observedClear = previous != s_visibilityStates.end() && previous->second.observedClear;
		const Bool firstObservedClear = status == VISIBILITY_STATUS_CLEAR && !observedClear;
		if (status != previousStatus)
		{
			std::string positionValue;
			if (!positionJson(position, positionValue))
			{
				return;
			}
			const std::string payload = "{\"player_index\":" + std::to_string(pair.playerIndex)
				+ ",\"object_id\":" + std::to_string(static_cast<UnsignedInt>(pair.objectId))
				+ ",\"template_name\":" + jsonString(object->getTemplate()->getName().str())
				+ ",\"previous_status\":" + jsonString(visibilityStatusName(previousStatus))
				+ ",\"status\":" + jsonString(visibilityStatusName(status))
				+ ",\"first_observed_clear\":" + (firstObservedClear ? "true" : "false")
				+ ",\"position\":" + positionValue
				+ ",\"observation_basis\":\"object_center_partition_cell\""
				+ ",\"source\":\"PartitionManager::getShroudStatusForPlayer\""
				+ ",\"sample_interval_frames\":15"
				+ ",\"sampling_cycle_id\":" + std::to_string(s_cycleId) + "}";
			ReplayTelemetry::emit(frame, "object_visibility_changed", AsciiString(payload.c_str()));
		}
		s_visibilityStates[pair] = VisibilityState{
			status, static_cast<Bool>(observedClear || status == VISIBILITY_STATUS_CLEAR)
		};
	}

	void emitSummary(UnsignedInt frame, size_t cursorStart, size_t cursorEnd, Bool cycleComplete)
	{
		const std::string payload = "{\"source\":\"PartitionManager::getShroudStatusForPlayer\""
			",\"sample_interval_frames\":15,\"maximum_pairs_per_pass\":8192"
			",\"eligible_pair_count\":" + std::to_string(s_cyclePairs.size())
			+ ",\"sampled_pair_count\":" + std::to_string(cursorEnd - cursorStart)
			+ ",\"cursor_start\":" + std::to_string(cursorStart)
			+ ",\"cursor_end\":" + std::to_string(cursorEnd)
			+ ",\"sampling_cycle_id\":" + std::to_string(s_cycleId)
			+ ",\"cycle_complete\":" + (cycleComplete ? "true" : "false") + "}";
		ReplayTelemetry::emit(frame, "visibility_sampling_summary", AsciiString(payload.c_str()));
	}
}

void ReplayVisibilitySampler::reset()
{
	// TheSuperHackers @feature Leex 23/08/2026 Clear all copied visibility history and cycle identities before Object IDs can be reused. (#0)
	s_visibilityStates.clear();
	s_cyclePairs.clear();
	s_cursor = 0;
	s_cycleId = 0;
	s_cycleActive = FALSE;
}

void ReplayVisibilitySampler::sampleEndOfFrame()
{
	if (!ReplayTelemetry::isInitialized() || TheGameLogic == nullptr || ThePartitionManager == nullptr)
	{
		return;
	}
	const UnsignedInt frame = TheGameLogic->getFrame();
	if (frame == 0 || frame % SAMPLE_INTERVAL_FRAMES != 0)
	{
		return;
	}
	pruneDestroyedState();
	if (!s_cycleActive)
	{
		beginCycle();
	}
	const size_t cursorStart = s_cursor;
	const size_t remaining = s_cyclePairs.size() - s_cursor;
	const size_t sampledPairCount = std::min(MAXIMUM_PAIRS_PER_PASS, remaining);
	const size_t cursorEnd = cursorStart + sampledPairCount;
	for (; s_cursor < cursorEnd; ++s_cursor)
	{
		samplePair(frame, s_cyclePairs[s_cursor]);
	}
	const Bool cycleComplete = s_cursor == s_cyclePairs.size();
	// TheSuperHackers @feature Leex 23/08/2026 Emit one exact pass summary even when no pairs are eligible or a capped cycle remains incomplete. (#0)
	emitSummary(frame, cursorStart, cursorEnd, cycleComplete);
	if (cycleComplete)
	{
		s_cycleActive = FALSE;
		s_cyclePairs.clear();
		s_cursor = 0;
		++s_cycleId;
	}
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
