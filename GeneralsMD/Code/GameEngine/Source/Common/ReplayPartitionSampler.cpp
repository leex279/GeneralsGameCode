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

#include "Common/ReplayPartitionSampler.h"

#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/Recorder.h"
#include "Common/ReplayTelemetry.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/PartitionManager.h"
#include "GameNetwork/GameInfo.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <string>
#include <system_error>
#include <vector>

namespace
{
	const UnsignedInt SAMPLE_INTERVAL_FRAMES = 300;
	const size_t MAXIMUM_SAMPLED_CELLS = ReplayPartitionSampler::MAXIMUM_SAMPLED_CELLS;
	const char *SAMPLING_SCHEME = "uniform_partition_lattice_v1";
	const char *HEURISTIC_SEMANTICS = "engine_ai_owner_contribution_heuristic";
	const char *SHROUD_PROVIDER = "PartitionCell::getShroudStatusForPlayer";
	const char *THREAT_PROVIDER = "PartitionCell::getThreatValue";
	const char *CASH_PROVIDER = "PartitionCell::getCashValue";

	struct ReplayPartitionSamplerState
	{
		Bool s_hasSampledFrame;
		UnsignedInt s_lastSampleFrame;
	};

	ReplayPartitionSamplerState s_samplerState = { FALSE, 0 };

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
			ReplayTelemetry::fail("nonfinite_partition_cell_position",
				"partition sample contains a nonfinite cell-center position");
			return FALSE;
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, 9);
		if (converted.ec != std::errc())
		{
			ReplayTelemetry::fail("partition_cell_number_format_failed",
				"could not serialize a partition cell-center position");
			return FALSE;
		}
		result.assign(buffer, converted.ptr);
		return TRUE;
	}

	const char *shroudStatusName(CellShroudStatus status)
	{
		switch (status)
		{
			case CELLSHROUD_CLEAR: return "clear";
			case CELLSHROUD_FOGGED: return "fogged";
			default: return "shrouded";
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

	Bool buildPayload(Int playerIndex, Int cellCountX, Int cellCountY,
		const std::vector<ReplayPartitionLatticeCell> &coordinates, std::string &payload)
	{
		std::string cellsJson("[");
		Bool firstCell = TRUE;
		for (const ReplayPartitionLatticeCell &coordinate : coordinates)
		{
			PartitionCell *cell = ThePartitionManager->getCellAt(coordinate.cellX, coordinate.cellY);
			if (cell == nullptr)
			{
				ReplayTelemetry::fail("partition_cell_unavailable",
					"selected partition lattice cell is unavailable");
				return FALSE;
			}
			Real worldX = 0.0f;
			Real worldY = 0.0f;
			ThePartitionManager->getCellCenterPos(coordinate.cellX, coordinate.cellY, worldX, worldY);
			std::string worldXJson;
			std::string worldYJson;
			if (!jsonNumber(worldX, worldXJson) || !jsonNumber(worldY, worldYJson))
			{
				return FALSE;
			}
			const CellShroudStatus shroudStatus = cell->getShroudStatusForPlayer(playerIndex);
			const UnsignedInt threatValue = cell->getThreatValue(playerIndex);
			const UnsignedInt cashValue = cell->getCashValue(playerIndex);
			if (!firstCell)
			{
				cellsJson.push_back(',');
			}
			firstCell = FALSE;
			cellsJson += "{\"cell_x\":" + std::to_string(coordinate.cellX)
				+ ",\"cell_y\":" + std::to_string(coordinate.cellY)
				+ ",\"world_position\":{\"x\":" + worldXJson
				+ ",\"y\":" + worldYJson + ",\"z\":0}"
				+ ",\"shroud_status\":" + jsonString(shroudStatusName(shroudStatus))
				+ ",\"threat_value\":" + std::to_string(threatValue)
				+ ",\"cash_value\":" + std::to_string(cashValue) + "}";
		}
		cellsJson.push_back(']');

		const long long totalCellCount = static_cast<long long>(cellCountX) * cellCountY;
		const Bool complete = static_cast<long long>(coordinates.size()) == totalCellCount;
		payload = "{\"player_index\":" + std::to_string(playerIndex)
			+ ",\"sample_interval_frames\":" + std::to_string(SAMPLE_INTERVAL_FRAMES)
			+ ",\"sampling_scheme\":" + jsonString(SAMPLING_SCHEME)
			+ ",\"heuristic_semantics\":" + jsonString(HEURISTIC_SEMANTICS)
			+ ",\"grid\":{\"cell_count_x\":" + std::to_string(cellCountX)
			+ ",\"cell_count_y\":" + std::to_string(cellCountY)
			+ ",\"total_cell_count\":" + std::to_string(totalCellCount)
			+ ",\"sampled_cell_count\":" + std::to_string(coordinates.size())
			+ ",\"maximum_sampled_cells\":" + std::to_string(MAXIMUM_SAMPLED_CELLS)
			+ ",\"complete\":" + (complete ? "true" : "false") + "}"
			+ ",\"providers\":{\"shroud\":" + jsonString(SHROUD_PROVIDER)
			+ ",\"threat\":" + jsonString(THREAT_PROVIDER)
			+ ",\"cash\":" + jsonString(CASH_PROVIDER) + "}"
			+ ",\"cells\":" + cellsJson + "}";
		return TRUE;
	}

	void emitSamples(UnsignedInt frame)
	{
		if (!ReplayTelemetry::isInitialized() || ThePartitionManager == nullptr
			|| (s_samplerState.s_hasSampledFrame && s_samplerState.s_lastSampleFrame == frame))
		{
			return;
		}
		const Int cellCountX = ThePartitionManager->getCellCountX();
		const Int cellCountY = ThePartitionManager->getCellCountY();
		const std::vector<ReplayPartitionLatticeCell> coordinates =
			ReplayPartitionSampler::selectLatticeCells(cellCountX, cellCountY);
		if (coordinates.empty())
		{
			ReplayTelemetry::fail("partition_grid_unavailable",
				"partition manager reported an empty grid for lattice sampling");
			return;
		}
		const std::vector<Int> playerIndices = resolvedOccupiedPlayerIndices();
		// TheSuperHackers @feature Leex 23/08/2026 Emit sorted players and row-major cells with exact direct provider values and no debug-display normalization. (#0)
		for (const Int playerIndex : playerIndices)
		{
			std::string payload;
			if (!buildPayload(playerIndex, cellCountX, cellCountY, coordinates, payload))
			{
				return;
			}
			ReplayTelemetry::emit(frame, "partition_engine_grid_sample", AsciiString(payload.c_str()));
		}
		s_samplerState.s_hasSampledFrame = TRUE;
		s_samplerState.s_lastSampleFrame = frame;
	}
}

void ReplayPartitionSampler::reset()
{
	s_samplerState.s_hasSampledFrame = FALSE;
	s_samplerState.s_lastSampleFrame = 0;
}

void ReplayPartitionSampler::sampleEndOfFrame()
{
	if (TheGameLogic == nullptr)
	{
		return;
	}
	const UnsignedInt frame = TheGameLogic->getFrame();
	if (frame == 0 || frame % SAMPLE_INTERVAL_FRAMES != 0)
	{
		return;
	}
	emitSamples(frame);
}

void ReplayPartitionSampler::emitTerminalSample(UnsignedInt finalFrame)
{
	emitSamples(finalFrame);
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
