#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayGameDataExport.h"

#include "Common/KindOf.h"
#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/PlayerTemplate.h"
#include "Common/ProductionPrerequisite.h"
#include "Common/ReplayTelemetry.h"
#include "Common/Science.h"
#include "Common/ThingFactory.h"
#include "Common/ThingTemplate.h"
#include "Common/Upgrade.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Locomotor.h"
#define DEFINE_LOCOMOTORSET_NAMES
#include "GameLogic/Module/AIUpdate.h"
#undef DEFINE_LOCOMOTORSET_NAMES
#include "GameLogic/TerrainLogic.h"
#include "GameLogic/Weapon.h"
#include "GameNetwork/GameInfo.h"

#include <algorithm>
#include <charconv>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <vector>

// TheSuperHackers @feature Leex 18/08/2026 Serialize only already-loaded engine metadata without constructing gameplay state. (#TBD)
namespace
{
	Bool s_catalogReady = FALSE;
	Bool s_playersEmitted = FALSE;
	std::string s_catalogPath;
	std::string s_catalogSha256;
	std::string s_engineDataIdentity;
	UnsignedInt s_tempCounter = 0;

	std::string jsonUtf8(const char *value)
	{
		std::string result = "\"";
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
				if (character < 0x20)
				{
					char escaped[7];
					snprintf(escaped, sizeof(escaped), "\\u%04x", static_cast<UnsignedInt>(character));
					result += escaped;
				}
				else
				{
					result.push_back(static_cast<char>(character));
				}
				break;
			}
		}
		result.push_back('"');
		return result;
	}

	std::string narrowUtf8(const AsciiString &value)
	{
		std::string result;
		const UnsignedByte *cursor = reinterpret_cast<const UnsignedByte *>(value.str());
		while (*cursor != 0)
		{
			const UnsignedByte character = *cursor++;
			if (character < 0x80)
			{
				result.push_back(static_cast<char>(character));
			}
			else
			{
				result.push_back(static_cast<char>(0xc0 | (character >> 6)));
				result.push_back(static_cast<char>(0x80 | (character & 0x3f)));
			}
		}
		return result;
	}

	std::string jsonString(const AsciiString &value)
	{
		return jsonUtf8(narrowUtf8(value).c_str());
	}

	std::string unicodeUtf8(const UnicodeString &value)
	{
		if (value.isEmpty())
		{
			return std::string();
		}
		const Int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.str(), value.getLength(), nullptr, 0,
			nullptr, nullptr);
		if (length <= 0)
		{
			return std::string();
		}
		std::string result(static_cast<size_t>(length), '\0');
		if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.str(), value.getLength(), result.data(), length,
			nullptr, nullptr) != length)
		{
			return std::string();
		}
		return result;
	}

	std::string jsonReal(Real value)
	{
		char buffer[64];
		const std::to_chars_result result = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, std::numeric_limits<Real>::max_digits10);
		if (result.ec != std::errc())
		{
			ReplayTelemetry::fail("catalog_format_failed", "could not format a catalog number");
			return "0";
		}
		return std::string(buffer, result.ptr);
	}

	template <typename Collection>
	std::string stringArray(const Collection &values)
	{
		std::string result = "[";
		Bool first = TRUE;
		for (const auto &value : values)
		{
			if (!first)
			{
				result.push_back(',');
			}
			first = FALSE;
			result += jsonUtf8(value.c_str());
		}
		result.push_back(']');
		return result;
	}

	std::string namedRecords(const std::set<std::string> &names)
	{
		std::string result = "[";
		Int ordinal = 0;
		for (const std::string &name : names)
		{
			if (ordinal != 0)
			{
				result.push_back(',');
			}
			result += "{\"ordinal\":" + std::to_string(ordinal++) + ",\"name\":" + jsonUtf8(name.c_str()) + "}";
		}
		result.push_back(']');
		return result;
	}

	const AIUpdateModuleData *findAIUpdateData(const ThingTemplate *thingTemplate)
	{
		const ModuleInfo &modules = thingTemplate->getBehaviorModuleInfo();
		for (Int index = 0; index < modules.getCount(); ++index)
		{
			const ModuleData *moduleData = modules.getNthData(index);
			if (moduleData != nullptr && moduleData->isAiModuleData())
			{
				return static_cast<const AIUpdateModuleData *>(moduleData);
			}
		}
		return nullptr;
	}

	std::string prerequisitesJson(const ThingTemplate *thingTemplate)
	{
		std::string result = "[";
		for (Int prereqIndex = 0; prereqIndex < thingTemplate->getPrereqCount(); ++prereqIndex)
		{
			if (prereqIndex != 0)
			{
				result.push_back(',');
			}
			const ProductionPrerequisite *prerequisite = thingTemplate->getNthPrereq(prereqIndex);
			result += "{\"units\":[";
			for (Int unitIndex = 0; unitIndex < prerequisite->replayAnalyzerGetUnitCount(); ++unitIndex)
			{
				if (unitIndex != 0)
				{
					result.push_back(',');
				}
				const ThingTemplate *unit = prerequisite->replayAnalyzerGetUnitTemplate(unitIndex);
				const AsciiString unitName = unit != nullptr ? unit->getName() : AsciiString::TheEmptyString;
				result += "{\"name\":" + jsonString(unitName) + ",\"or_with_previous\":"
					+ (prerequisite->replayAnalyzerIsUnitOrWithPrevious(unitIndex) ? "true}" : "false}");
			}
			result += "],\"sciences\":[";
			for (Int scienceIndex = 0; scienceIndex < prerequisite->replayAnalyzerGetScienceCount(); ++scienceIndex)
			{
				if (scienceIndex != 0)
				{
					result.push_back(',');
				}
				result += jsonString(TheScienceStore->getInternalNameForScience(
					prerequisite->replayAnalyzerGetScience(scienceIndex)));
			}
			result += "]}";
		}
		result.push_back(']');
		return result;
	}

	std::string buildCatalog()
	{
		std::map<std::string, const ThingTemplate *> templates;
		for (const ThingTemplate *base = TheThingFactory->firstTemplate(); base != nullptr;
			base = base->friend_getNextTemplate())
		{
			const ThingTemplate *resolved = static_cast<const ThingTemplate *>(base->getFinalOverride());
			if (resolved != nullptr && resolved->getName().isNotEmpty())
			{
				templates[narrowUtf8(resolved->getName())] = resolved;
			}
		}
		std::set<std::string> weaponNames;
		std::map<std::string, LocomotorSurfaceTypeMask> locomotors;
		std::string templateJson = "[";
		Int templateOrdinal = 0;
		for (const auto &entry : templates)
		{
			const ThingTemplate *thingTemplate = entry.second;
			if (templateOrdinal != 0)
			{
				templateJson.push_back(',');
			}
			std::vector<std::string> kindNames;
			std::string kindJson = "[";
			Int kindOrdinal = 0;
			for (Int kind = KINDOF_FIRST; kind < KINDOF_COUNT; ++kind)
			{
				if (!thingTemplate->isKindOf(static_cast<KindOfType>(kind)))
				{
					continue;
				}
				const char *name = KindOfMaskType::getNameFromSingleBit(kind);
				if (name == nullptr)
				{
					continue;
				}
				if (kindOrdinal != 0)
				{
					kindJson.push_back(',');
				}
				kindJson += "{\"ordinal\":" + std::to_string(kindOrdinal++) + ",\"name\":" + jsonUtf8(name) + "}";
				kindNames.emplace_back(name);
			}
			kindJson.push_back(']');

			std::set<std::string> templateWeapons;
			for (const WeaponTemplateSet &weaponSet : thingTemplate->getWeaponTemplateSets())
			{
				for (Int slot = 0; slot < WEAPONSLOT_COUNT; ++slot)
				{
					const WeaponTemplate *weapon = weaponSet.getNth(static_cast<WeaponSlotType>(slot));
					if (weapon != nullptr && weapon->getName().isNotEmpty())
					{
						const std::string name = narrowUtf8(weapon->getName());
						templateWeapons.insert(name);
						weaponNames.insert(name);
					}
				}
			}

			std::string locomotorSetsJson = "[";
			const AIUpdateModuleData *aiData = findAIUpdateData(thingTemplate);
			Bool firstSet = TRUE;
			if (aiData != nullptr)
			{
				for (const auto &setEntry : aiData->m_locomotorTemplates)
				{
					std::set<std::string> setNames;
					for (const LocomotorTemplate *locomotor : setEntry.second)
					{
						if (locomotor == nullptr || locomotor->replayAnalyzerGetName().isEmpty())
						{
							continue;
						}
						const std::string name = narrowUtf8(locomotor->replayAnalyzerGetName());
						setNames.insert(name);
						const auto existing = locomotors.find(name);
						if (existing != locomotors.end() && existing->second != locomotor->replayAnalyzerGetSurfaces())
						{
							ReplayTelemetry::fail("catalog_locomotor_identity", "one locomotor name has conflicting surfaces");
							return std::string();
						}
						locomotors[name] = locomotor->replayAnalyzerGetSurfaces();
					}
					if (setNames.empty())
					{
						continue;
					}
					if (!firstSet)
					{
						locomotorSetsJson.push_back(',');
					}
					firstSet = FALSE;
					const Int setType = static_cast<Int>(setEntry.first);
					const char *setName = setType >= 0 && setType < LOCOMOTORSET_COUNT ? TheLocomotorSetNames[setType] : "SET_UNKNOWN";
					locomotorSetsJson += "{\"set\":" + jsonUtf8(setName) + ",\"names\":" + stringArray(setNames) + "}";
				}
			}
			locomotorSetsJson.push_back(']');

			std::set<std::string> categoryTags(kindNames.begin(), kindNames.end());
			if (thingTemplate->isBuildableItem()) categoryTags.insert("BUILDABLE");
			if (thingTemplate->isBuildFacility()) categoryTags.insert("PRODUCTION_CAPABLE");
			if (!templateWeapons.empty()) categoryTags.insert("WEAPON_CAPABLE");
			if (aiData != nullptr && !aiData->m_locomotorTemplates.empty()) categoryTags.insert("LOCOMOTOR_CAPABLE");
			const AsciiString &faction = thingTemplate->getDefaultOwningSide();
			templateJson += "{\"ordinal\":" + std::to_string(templateOrdinal++)
				+ ",\"name\":" + jsonUtf8(entry.first.c_str())
				+ ",\"faction\":" + (faction.isEmpty() ? "null" : jsonString(faction))
				+ ",\"kind_of_flags\":" + kindJson
				+ ",\"build_cost\":" + std::to_string(thingTemplate->friend_getBuildCost())
				+ ",\"configured_build_time_seconds\":" + jsonReal(thingTemplate->replayAnalyzerGetConfiguredBuildTimeSeconds())
				+ ",\"prerequisites\":" + prerequisitesJson(thingTemplate)
				+ ",\"locomotor_sets\":" + locomotorSetsJson
				+ ",\"production_capable\":" + (thingTemplate->isBuildFacility() ? "true" : "false")
				+ ",\"weapon_names\":" + stringArray(templateWeapons)
				+ ",\"category_tags\":" + stringArray(categoryTags) + "}";
		}
		templateJson.push_back(']');

		std::set<std::string> upgrades;
		for (const UpgradeTemplate *upgrade = TheUpgradeCenter->firstUpgradeTemplate(); upgrade != nullptr;
			upgrade = upgrade->friend_getNext())
		{
			if (upgrade->getUpgradeName().isNotEmpty())
			{
				upgrades.insert(narrowUtf8(upgrade->getUpgradeName()));
			}
		}
		std::set<std::string> sciences;
		for (const AsciiString &science : TheScienceStore->friend_getScienceNames())
		{
			if (science.isNotEmpty())
			{
				sciences.insert(narrowUtf8(science));
			}
		}
		std::string locomotorJson = "[";
		Int locomotorOrdinal = 0;
		for (const auto &entry : locomotors)
		{
			if (locomotorOrdinal != 0)
			{
				locomotorJson.push_back(',');
			}
			std::vector<std::string> surfaces;
			if (entry.second & LOCOMOTORSURFACE_GROUND) surfaces.emplace_back("GROUND");
			if (entry.second & LOCOMOTORSURFACE_WATER) surfaces.emplace_back("WATER");
			if (entry.second & LOCOMOTORSURFACE_CLIFF) surfaces.emplace_back("CLIFF");
			if (entry.second & LOCOMOTORSURFACE_AIR) surfaces.emplace_back("AIR");
			if (entry.second & LOCOMOTORSURFACE_RUBBLE) surfaces.emplace_back("RUBBLE");
			locomotorJson += "{\"ordinal\":" + std::to_string(locomotorOrdinal++) + ",\"name\":"
				+ jsonUtf8(entry.first.c_str()) + ",\"surface_capabilities\":" + stringArray(surfaces) + "}";
		}
		locomotorJson.push_back(']');

		return "{\"schema_version\":1,\"type\":\"game_data_catalog\",\"engine_data_identity\":"
			+ jsonUtf8(s_engineDataIdentity.c_str())
			+ ",\"weapon_scope\":\"referenced_by_thing_templates\",\"locomotor_scope\":\"referenced_by_thing_templates\""
			+ ",\"thing_templates\":" + templateJson
			+ ",\"upgrades\":" + namedRecords(upgrades)
			+ ",\"sciences\":" + namedRecords(sciences)
			+ ",\"weapons\":" + namedRecords(weaponNames)
			+ ",\"locomotors\":" + locomotorJson + "}\n";
	}

	Bool fileMatches(const AsciiString &path, const std::string &expected)
	{
		FILE *input = fopen(path.str(), "rb");
		if (input == nullptr)
		{
			return FALSE;
		}
		std::string actual;
		char buffer[16 * 1024];
		size_t count = 0;
		while ((count = fread(buffer, 1, sizeof(buffer), input)) > 0)
		{
			actual.append(buffer, count);
		}
		const Bool readOk = ferror(input) == 0;
		fclose(input);
		return readOk && actual == expected;
	}

	void discardCatalogTransaction(const AsciiString &path)
	{
		errno = 0;
		if (remove(path.str()) != 0 && errno != ENOENT)
		{
			ReplayTelemetry::fail("catalog_cleanup_failed", "could not remove the owned catalog transaction");
		}
	}

	Bool publishCatalog(const std::string &catalog)
	{
		s_catalogSha256 = ReplayTelemetry::sha256Hex(catalog.data(), catalog.size()).str();
		s_catalogPath = "game-data-catalog-v1-" + s_catalogSha256 + ".json";
		std::string finalPath = ReplayTelemetry::getTracePath().str();
		const size_t separator = finalPath.find_last_of("\\/");
		finalPath = separator == std::string::npos ? std::string() : finalPath.substr(0, separator + 1);
		finalPath += s_catalogPath;
		const AsciiString destination(finalPath.c_str());
		if (GetFileAttributesA(destination.str()) != INVALID_FILE_ATTRIBUTES)
		{
			if (fileMatches(destination, catalog))
			{
				return TRUE;
			}
			ReplayTelemetry::fail("catalog_collision", "catalog identity path contains unrelated bytes");
			return FALSE;
		}

		AsciiString temporary;
		FILE *output = nullptr;
		for (Int attempt = 0; attempt < 100 && output == nullptr; ++attempt)
		{
			temporary.format("%s.tmp.%lu.%u", destination.str(), static_cast<unsigned long>(GetCurrentProcessId()),
				++s_tempCounter);
			errno = 0;
			output = fopen(temporary.str(), "wbx");
			if (output == nullptr && errno != EEXIST)
			{
				break;
			}
		}
		if (output == nullptr)
		{
			ReplayTelemetry::fail("catalog_open_failed", "could not create an exclusive catalog transaction");
			return FALSE;
		}
		Bool success = fwrite(catalog.data(), 1, catalog.size(), output) == catalog.size();
		success = fflush(output) == 0 && success;
		success = fclose(output) == 0 && success;
		if (!success)
		{
			discardCatalogTransaction(temporary);
			ReplayTelemetry::fail("catalog_write_failed", "could not write the complete catalog transaction");
			return FALSE;
		}
		if (MoveFileA(temporary.str(), destination.str()))
		{
			return TRUE;
		}
		if (fileMatches(destination, catalog))
		{
			discardCatalogTransaction(temporary);
			return TRUE;
		}
		discardCatalogTransaction(temporary);
		ReplayTelemetry::fail("catalog_collision", "catalog destination appeared with unrelated bytes");
		return FALSE;
	}
}

void ReplayGameDataExport::reset()
{
	s_catalogReady = FALSE;
	s_playersEmitted = FALSE;
	s_catalogPath.clear();
	s_catalogSha256.clear();
	s_engineDataIdentity.clear();
}

Bool ReplayGameDataExport::prepareCatalog()
{
	if (s_catalogReady)
	{
		return TRUE;
	}
	if (TheThingFactory == nullptr || TheUpgradeCenter == nullptr || TheScienceStore == nullptr)
	{
		ReplayTelemetry::fail("catalog_metadata_unavailable", "loaded engine metadata stores are unavailable");
		return FALSE;
	}
	s_engineDataIdentity = ReplayTelemetry::getEngineDataIdentity().str();
	const std::string catalog = buildCatalog();
	if (catalog.empty() || !publishCatalog(catalog))
	{
		return FALSE;
	}
	s_catalogReady = TRUE;
	ReplayTelemetry::setGameDataCatalog(AsciiString(s_catalogPath.c_str()), AsciiString(s_catalogSha256.c_str()),
		AsciiString(s_engineDataIdentity.c_str()));
	return TRUE;
}

void ReplayGameDataExport::emitPlayersInitialized()
{
	if (s_playersEmitted || !s_catalogReady || !ReplayTelemetry::isEnabled())
	{
		return;
	}
	s_playersEmitted = TRUE;
	if (TheRecorder == nullptr || TheRecorder->getGameInfo() == nullptr || ThePlayerList == nullptr)
	{
		ReplayTelemetry::fail("players_unavailable", "resolved replay player state is unavailable");
		return;
	}

	const GameInfo *gameInfo = TheRecorder->getGameInfo();
	std::string players = "[";
	Bool first = TRUE;
	for (Int slotIndex = 0; slotIndex < MAX_SLOTS; ++slotIndex)
	{
		const GameSlot *slot = gameInfo->getConstSlot(slotIndex);
		Player *player = ThePlayerList->getPlayerFromSlotIndex(slotIndex);
		if (slot == nullptr || !slot->isOccupied() || player == nullptr)
		{
			continue;
		}
		if (!first)
		{
			players.push_back(',');
		}
		first = FALSE;
		const std::string replayName = unicodeUtf8(slot->getName());
		const PlayerTemplate *playerTemplate = player->getPlayerTemplate();
		AsciiString waypointName;
		waypointName.format("Player_%d_Start", slot->getStartPos() + 1);
		Waypoint *waypoint = slot->getStartPos() >= 0 && TheTerrainLogic != nullptr
			? TheTerrainLogic->getWaypointByName(waypointName) : nullptr;
		std::string startPosition = "null";
		if (waypoint != nullptr)
		{
			Coord3D position = *waypoint->getLocation();
			position.z = TheTerrainLogic->getGroundHeight(position.x, position.y);
			startPosition = "{\"x\":" + jsonReal(position.x) + ",\"y\":" + jsonReal(position.y)
				+ ",\"z\":" + jsonReal(position.z) + "}";
		}
		players += "{\"replay_name\":" + (replayName.empty() ? "null" : jsonUtf8(replayName.c_str()))
			+ ",\"player_index\":" + std::to_string(player->getPlayerIndex())
			+ ",\"team_id\":" + std::to_string(slot->getTeamNumber())
			+ ",\"faction_template_name\":"
			+ (playerTemplate != nullptr ? jsonString(playerTemplate->getName()) : "null")
			+ ",\"color\":" + (slot->getColor() >= 0 ? std::to_string(slot->getColor()) : "null")
			+ ",\"start_position_status\":" + (waypoint != nullptr ? "\"resolved\"" : "\"unknown\"")
			+ ",\"start_position\":" + startPosition
			+ ",\"controller\":" + (slot->isHuman() ? "\"human\"" : "\"ai\"")
			+ ",\"is_human\":" + (slot->isHuman() ? "true" : "false")
			+ ",\"is_local_player\":" + (player == ThePlayerList->getLocalPlayer() ? "true}" : "false}");
	}
	players.push_back(']');
	if (first)
	{
		ReplayTelemetry::fail("players_unavailable", "no occupied replay player resolved to an engine player");
		return;
	}
	const std::string payload = "{\"players\":" + players
		+ ",\"game_data_catalog\":{\"type\":\"game_data_catalog\",\"path\":" + jsonUtf8(s_catalogPath.c_str())
		+ ",\"sha256\":" + jsonUtf8(s_catalogSha256.c_str())
		+ ",\"engine_data_identity\":" + jsonUtf8(s_engineDataIdentity.c_str()) + "}}";
	ReplayTelemetry::emit(TheGameLogic != nullptr ? TheGameLogic->getFrame() : 0, "players_initialized",
		AsciiString(payload.c_str()));
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
