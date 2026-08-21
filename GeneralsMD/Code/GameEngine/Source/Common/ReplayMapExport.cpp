#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayMapExport.h"

#include "Common/KindOf.h"
#include "Common/Recorder.h"
#include "Common/ReplayEntityLifecycle.h"
#include "Common/ReplayTelemetry.h"
#include "Common/ThingTemplate.h"
#include "GameLogic/AI.h"
#include "GameLogic/AIPathfind.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Module/AutoDepositUpdate.h"
#include "GameLogic/Module/SupplyWarehouseDockUpdate.h"
#include "GameLogic/Object.h"
#include "GameLogic/TerrainLogic.h"
#include "GameNetwork/GameInfo.h"

#include <zlib.h>

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

// TheSuperHackers @feature Leex 21/08/2026 Build and transactionally publish canonical map bytes without mutating simulation state. (#TBD)
namespace
{
	const Int MAX_GRID_ELEMENTS = 16 * 1024 * 1024;
	const size_t MAX_MEMBER_BYTES = 64U * 1024U * 1024U;

	struct MapMember
	{
		std::string name;
		std::string dtype;
		std::vector<UnsignedByte> raw;
		std::vector<UnsignedByte> compressed;
		std::string rawSha256;
		std::string compressedSha256;
	};

	struct ExpectedFile
	{
		std::string name;
		std::vector<UnsignedByte> bytes;
	};

	Bool s_ready = FALSE;
	Bool s_failed = FALSE;
	AsciiString s_referenceJson;
	UnsignedInt s_tempCounter = 0;

	void fail(const char *code, const char *message)
	{
		s_failed = TRUE;
		ReplayTelemetry::fail(code, message);
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

	std::string jsonUtf8(const char *value)
	{
		std::string result("\"");
		const UnsignedByte *cursor = reinterpret_cast<const UnsignedByte *>(value != nullptr ? value : "");
		for (; *cursor != 0; ++cursor)
		{
			switch (*cursor)
			{
				case '"': result += "\\\""; break;
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
		result.push_back('"');
		return result;
	}

	std::string jsonString(const AsciiString &value)
	{
		return jsonUtf8(narrowUtf8(value).c_str());
	}

	std::string jsonReal(Real value)
	{
		if (!std::isfinite(static_cast<double>(value)))
		{
			fail("map_nonfinite_number", "map snapshot contains a nonfinite float32 value");
			return "null";
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, std::numeric_limits<Real>::max_digits10);
		if (converted.ec != std::errc())
		{
			fail("map_number_format_failed", "could not serialize a map float32 value");
			return "null";
		}
		return std::string(buffer, converted.ptr);
	}

	std::string positionJson(const Coord3D &position)
	{
		return "{\"x\":" + jsonReal(position.x) + ",\"y\":" + jsonReal(position.y)
			+ ",\"z\":" + jsonReal(position.z) + "}";
	}

	std::string position2Json(Real x, Real y)
	{
		return "{\"x\":" + jsonReal(x) + ",\"y\":" + jsonReal(y) + "}";
	}

	Bool insideXY(const Coord3D &position, Real minimumX, Real minimumY, Real maximumX, Real maximumY)
	{
		return std::isfinite(static_cast<double>(position.x)) && std::isfinite(static_cast<double>(position.y))
			&& position.x >= minimumX && position.x <= maximumX
			&& position.y >= minimumY && position.y <= maximumY;
	}

	void appendLittleU32(std::vector<UnsignedByte> &bytes, UnsignedInt value)
	{
		bytes.push_back(static_cast<UnsignedByte>(value));
		bytes.push_back(static_cast<UnsignedByte>(value >> 8));
		bytes.push_back(static_cast<UnsignedByte>(value >> 16));
		bytes.push_back(static_cast<UnsignedByte>(value >> 24));
	}

	void appendLittleFloat(std::vector<UnsignedByte> &bytes, Real value)
	{
		static_assert(sizeof(Real) == sizeof(UnsignedInt));
		UnsignedInt bits = 0;
		memcpy(&bits, &value, sizeof(bits));
		appendLittleU32(bytes, bits);
	}

	Bool compressMember(MapMember &member)
	{
		if (member.raw.empty() || member.raw.size() > MAX_MEMBER_BYTES)
		{
			fail("map_member_size", "map member exceeds its closed size bound");
			return FALSE;
		}
		// zlib 1.1.4 predates compressBound in this repository; its documented worst-case overhead is bounded here.
		uLongf outputLength = static_cast<uLongf>(member.raw.size() + member.raw.size() / 1000U + 64U);
		member.compressed.resize(static_cast<size_t>(outputLength));
		const Int result = compress2(member.compressed.data(), &outputLength, member.raw.data(),
			static_cast<uLong>(member.raw.size()), 9);
		if (result != Z_OK || outputLength == 0 || outputLength > MAX_MEMBER_BYTES)
		{
			fail("map_compression_failed", "zlib could not produce a bounded deterministic member");
			return FALSE;
		}
		member.compressed.resize(static_cast<size_t>(outputLength));
		member.rawSha256 = ReplayTelemetry::sha256Hex(reinterpret_cast<const char *>(member.raw.data()), member.raw.size()).str();
		member.compressedSha256 = ReplayTelemetry::sha256Hex(
			reinterpret_cast<const char *>(member.compressed.data()), member.compressed.size()).str();
		return TRUE;
	}

	std::string memberJson(const MapMember &member, Int elementCount)
	{
		return "{\"compressed_sha256\":" + jsonUtf8(member.compressedSha256.c_str())
			+ ",\"compressed_size\":" + std::to_string(member.compressed.size())
			+ ",\"compression\":\"zlib\",\"compression_level\":9"
			+ ",\"dtype\":" + jsonUtf8(member.dtype.c_str())
			+ ",\"element_count\":" + std::to_string(elementCount)
			+ ",\"endianness\":\"little\",\"grid\":\"pathing\",\"uncompressed_sha256\":"
			+ jsonUtf8(member.rawSha256.c_str())
			+ ",\"uncompressed_size\":" + std::to_string(member.raw.size()) + "}";
	}

	std::string intArray(const std::vector<Int> &values)
	{
		std::string result("[");
		for (size_t index = 0; index < values.size(); ++index)
		{
			if (index != 0) result.push_back(',');
			result += std::to_string(values[index]);
		}
		result.push_back(']');
		return result;
	}

	std::string stringArray(const std::vector<std::string> &values)
	{
		std::string result("[");
		for (size_t index = 0; index < values.size(); ++index)
		{
			if (index != 0) result.push_back(',');
			result += jsonUtf8(values[index].c_str());
		}
		result.push_back(']');
		return result;
	}

	std::string parentPath(const AsciiString &path)
	{
		const std::string full(path.str());
		const size_t separator = full.find_last_of("\\/");
		return separator == std::string::npos ? std::string() : full.substr(0, separator + 1);
	}

	std::string joinPath(const std::string &left, const std::string &right)
	{
		if (left.empty()) return right;
		const char last = left[left.size() - 1];
		return left + (last == '\\' || last == '/' ? "" : "\\") + right;
	}

	Bool safeDirectory(const std::string &path)
	{
		const DWORD attributes = GetFileAttributesA(path.c_str());
		return attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0
			&& (attributes & FILE_ATTRIBUTE_REPARSE_POINT) == 0;
	}

	Bool safeFileMatches(const std::string &path, const std::vector<UnsignedByte> &expected)
	{
		const DWORD attributes = GetFileAttributesA(path.c_str());
		if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0
			|| (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
		{
			return FALSE;
		}
		HANDLE handle = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
			FILE_ATTRIBUTE_NORMAL, nullptr);
		if (handle == INVALID_HANDLE_VALUE)
		{
			return FALSE;
		}
		BY_HANDLE_FILE_INFORMATION information;
		const Bool safeLinks = GetFileInformationByHandle(handle, &information) && information.nNumberOfLinks == 1;
		CloseHandle(handle);
		if (!safeLinks)
		{
			return FALSE;
		}
		FILE *input = fopen(path.c_str(), "rb");
		if (input == nullptr)
		{
			return FALSE;
		}
		std::vector<UnsignedByte> actual;
		UnsignedByte buffer[16 * 1024];
		size_t count = 0;
		while ((count = fread(buffer, 1, sizeof(buffer), input)) > 0)
		{
			actual.insert(actual.end(), buffer, buffer + count);
			if (actual.size() > expected.size()) break;
		}
		const Bool readOk = ferror(input) == 0 && fclose(input) == 0;
		return readOk && actual == expected;
	}

	Bool exactDirectoryMatches(const std::string &directory, const std::vector<ExpectedFile> &files)
	{
		if (!safeDirectory(directory)) return FALSE;
		std::set<std::string> expectedNames;
		for (const ExpectedFile &file : files) expectedNames.insert(file.name);
		std::set<std::string> actualNames;
		WIN32_FIND_DATAA data;
		HANDLE search = FindFirstFileA(joinPath(directory, "*").c_str(), &data);
		if (search == INVALID_HANDLE_VALUE) return FALSE;
		Bool safe = TRUE;
		do
		{
			const std::string name(data.cFileName);
			if (name == "." || name == "..") continue;
			if ((data.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != 0)
			{
				safe = FALSE;
				break;
			}
			actualNames.insert(name);
		} while (FindNextFileA(search, &data));
		FindClose(search);
		if (!safe || actualNames != expectedNames) return FALSE;
		for (const ExpectedFile &file : files)
		{
			if (!safeFileMatches(joinPath(directory, file.name), file.bytes)) return FALSE;
		}
		return TRUE;
	}

	void cleanupOwnedDirectory(const std::string &directory, const std::vector<ExpectedFile> &files)
	{
		for (const ExpectedFile &file : files)
		{
			DeleteFileA(joinPath(directory, file.name).c_str());
		}
		RemoveDirectoryA(directory.c_str());
	}

	Bool writeExpectedFile(const std::string &directory, const ExpectedFile &file)
	{
		const std::string path = joinPath(directory, file.name);
		FILE *output = fopen(path.c_str(), "wbx");
		if (output == nullptr) return FALSE;
		Bool success = fwrite(file.bytes.data(), 1, file.bytes.size(), output) == file.bytes.size();
		success = fflush(output) == 0 && success;
		success = fclose(output) == 0 && success;
		return success && safeFileMatches(path, file.bytes);
	}

	Bool publishAsset(const std::string &contentHash, const std::vector<ExpectedFile> &files)
	{
		const std::string root = joinPath(parentPath(ReplayTelemetry::getTracePath()), "map-assets-v1");
		Bool ownsRoot = FALSE;
		DWORD attributes = GetFileAttributesA(root.c_str());
		if (attributes == INVALID_FILE_ATTRIBUTES)
		{
			if (!CreateDirectoryA(root.c_str(), nullptr))
			{
				fail("map_cache_root", "could not create the isolated map cache root");
				return FALSE;
			}
			ownsRoot = TRUE;
		}
		else if (!safeDirectory(root))
		{
			fail("map_cache_root", "map cache root is not a safe plain directory");
			return FALSE;
		}

		const std::string destination = joinPath(root, contentHash);
		if (GetFileAttributesA(destination.c_str()) != INVALID_FILE_ATTRIBUTES)
		{
			if (exactDirectoryMatches(destination, files)) return TRUE;
			fail("map_cache_collision", "map content path contains corrupt, partial, linked, or unrelated bytes");
			return FALSE;
		}

		std::string temporary;
		for (Int attempt = 0; attempt < 100; ++attempt)
		{
			temporary = destination + ".tmp." + std::to_string(static_cast<unsigned long>(GetCurrentProcessId()))
				+ "." + std::to_string(++s_tempCounter);
			if (CreateDirectoryA(temporary.c_str(), nullptr)) break;
			if (GetLastError() != ERROR_ALREADY_EXISTS)
			{
				temporary.clear();
				break;
			}
			temporary.clear();
		}
		if (temporary.empty())
		{
			if (ownsRoot) RemoveDirectoryA(root.c_str());
			fail("map_transaction_exhausted", "could not create one of 100 exclusive map transactions");
			return FALSE;
		}

		for (const ExpectedFile &file : files)
		{
			if (!writeExpectedFile(temporary, file))
			{
				cleanupOwnedDirectory(temporary, files);
				if (ownsRoot) RemoveDirectoryA(root.c_str());
				fail("map_write_failed", "could not write and verify the complete owned map transaction");
				return FALSE;
			}
		}
		if (!exactDirectoryMatches(temporary, files))
		{
			cleanupOwnedDirectory(temporary, files);
			if (ownsRoot) RemoveDirectoryA(root.c_str());
			fail("map_validation_failed", "owned map transaction failed exact byte validation");
			return FALSE;
		}
		const char *injected = getenv("GENERALS_REPLAY_MAP_EXPORT_TEST_FAIL");
		if (injected != nullptr && strcmp(injected, "before_publish") == 0)
		{
			cleanupOwnedDirectory(temporary, files);
			if (ownsRoot) RemoveDirectoryA(root.c_str());
			fail("map_injected_failure", "failure injected before map cache publication");
			return FALSE;
		}
		if (MoveFileA(temporary.c_str(), destination.c_str())) return TRUE;
		if (exactDirectoryMatches(destination, files))
		{
			cleanupOwnedDirectory(temporary, files);
			return TRUE;
		}
		cleanupOwnedDirectory(temporary, files);
		if (ownsRoot) RemoveDirectoryA(root.c_str());
		fail("map_cache_collision", "map destination appeared with corrupt or unrelated bytes");
		return FALSE;
	}

	struct StartPositionSnapshot
	{
		Waypoint *waypoint;
		std::vector<Int> slots;
	};

	Bool waypointLess(const Waypoint *left, const Waypoint *right)
	{
		if (left->getID() != right->getID()) return left->getID() < right->getID();
		return narrowUtf8(left->getName()) < narrowUtf8(right->getName());
	}

	std::string buildWaypoints(Real minimumX, Real minimumY, Real maximumX, Real maximumY)
	{
		std::vector<Waypoint *> waypoints;
		for (Waypoint *waypoint = TheTerrainLogic->getFirstWaypoint(); waypoint != nullptr; waypoint = waypoint->getNext())
		{
			waypoints.push_back(waypoint);
		}
		std::sort(waypoints.begin(), waypoints.end(), waypointLess);
		std::string result("[");
		for (size_t index = 0; index < waypoints.size(); ++index)
		{
			Waypoint *waypoint = waypoints[index];
			if (index != 0) result.push_back(',');
			Coord3D position = *waypoint->getLocation();
			if (!std::isfinite(static_cast<double>(position.z))) position.z = TheTerrainLogic->getGroundHeight(position.x, position.y);
			std::vector<std::string> labels;
			if (waypoint->getPathLabel1().isNotEmpty()) labels.push_back(narrowUtf8(waypoint->getPathLabel1()));
			if (waypoint->getPathLabel2().isNotEmpty()) labels.push_back(narrowUtf8(waypoint->getPathLabel2()));
			if (waypoint->getPathLabel3().isNotEmpty()) labels.push_back(narrowUtf8(waypoint->getPathLabel3()));
			std::vector<Int> links;
			for (Int link = 0; link < waypoint->getNumLinks(); ++link)
			{
				Waypoint *linked = waypoint->getLink(link);
				if (linked != nullptr) links.push_back(static_cast<Int>(linked->getID()));
			}
			std::sort(links.begin(), links.end());
			result += "{\"bidirectional\":" + std::string(waypoint->getBiDirectional() ? "true" : "false")
				+ ",\"bounds_policy\":" + jsonUtf8(insideXY(position, minimumX, minimumY, maximumX, maximumY)
					? "pathfinder_xy_closed" : "not_asserted_by_source")
				+ ",\"labels\":" + stringArray(labels) + ",\"link_ids\":" + intArray(links)
				+ ",\"name\":" + jsonString(waypoint->getName()) + ",\"position\":" + positionJson(position)
				+ ",\"waypoint_id\":" + std::to_string(static_cast<Int>(waypoint->getID())) + "}";
		}
		result.push_back(']');
		return result;
	}

	std::string buildStartPositions(Real minimumX, Real minimumY, Real maximumX, Real maximumY)
	{
		std::map<Int, StartPositionSnapshot> positions;
		const GameInfo *gameInfo = TheRecorder != nullptr ? TheRecorder->getGameInfo() : nullptr;
		if (gameInfo == nullptr)
		{
			fail("map_start_positions", "replay game slots are unavailable during map export");
			return "[]";
		}
		for (Int slotIndex = 0; slotIndex < MAX_SLOTS; ++slotIndex)
		{
			const GameSlot *slot = gameInfo->getConstSlot(slotIndex);
			if (slot == nullptr || !slot->isOccupied() || slot->getStartPos() < 0) continue;
			AsciiString name;
			name.format("Player_%d_Start", slot->getStartPos() + 1);
			Waypoint *waypoint = TheTerrainLogic->getWaypointByName(name);
			if (waypoint == nullptr)
			{
				fail("map_start_positions", "one occupied replay slot has no initialized start waypoint");
				return "[]";
			}
			StartPositionSnapshot &snapshot = positions[static_cast<Int>(waypoint->getID())];
			snapshot.waypoint = waypoint;
			snapshot.slots.push_back(slotIndex);
		}
		std::string result("[");
		Bool first = TRUE;
		for (const auto &entry : positions)
		{
			Coord3D position = *entry.second.waypoint->getLocation();
			position.z = TheTerrainLogic->getGroundHeight(position.x, position.y);
			if (!insideXY(position, minimumX, minimumY, maximumX, maximumY))
			{
				fail("map_start_position_bounds", "initialized start position is outside pathfinder bounds");
				return "[]";
			}
			if (!first) result.push_back(',');
			first = FALSE;
			result += "{\"bounds_policy\":\"pathfinder_xy_closed\",\"category_source\":"
				"\"GameSlot::getStartPos + TerrainLogic::getWaypointByName\",\"name\":"
				+ jsonString(entry.second.waypoint->getName()) + ",\"position\":" + positionJson(position)
				+ ",\"slot_indices\":" + intArray(entry.second.slots)
				+ ",\"waypoint_id\":" + std::to_string(entry.first) + "}";
		}
		result.push_back(']');
		return result;
	}

	std::string buildBridges(Real minimumX, Real minimumY, Real maximumX, Real maximumY)
	{
		std::vector<Bridge *> bridges;
		for (Bridge *bridge = TheTerrainLogic->getFirstBridge(); bridge != nullptr; bridge = bridge->getNext())
		{
			bridges.push_back(bridge);
		}
		std::sort(bridges.begin(), bridges.end(), [](Bridge *left, Bridge *right) {
			const ObjectID leftId = left->peekBridgeInfo()->bridgeObjectID;
			const ObjectID rightId = right->peekBridgeInfo()->bridgeObjectID;
			if (leftId != rightId) return leftId < rightId;
			return narrowUtf8(left->getBridgeTemplateName()) < narrowUtf8(right->getBridgeTemplateName());
		});
		std::string result("[");
		for (size_t index = 0; index < bridges.size(); ++index)
		{
			Bridge *bridge = bridges[index];
			const BridgeInfo &info = *bridge->peekBridgeInfo();
			const Coord3D corners[] = { info.fromLeft, info.fromRight, info.toLeft, info.toRight };
			if (!insideXY(info.from, minimumX, minimumY, maximumX, maximumY)
				|| !insideXY(info.to, minimumX, minimumY, maximumX, maximumY))
			{
				fail("map_bridge_bounds", "initialized bridge endpoint is outside pathfinder bounds");
				return "[]";
			}
			std::string cornersJson("[");
			for (Int corner = 0; corner < 4; ++corner)
			{
				if (!insideXY(corners[corner], minimumX, minimumY, maximumX, maximumY))
				{
					fail("map_bridge_bounds", "initialized bridge corner is outside pathfinder bounds");
					return "[]";
				}
				if (corner != 0) cornersJson.push_back(',');
				cornersJson += positionJson(corners[corner]);
			}
			cornersJson.push_back(']');
			if (index != 0) result.push_back(',');
			result += "{\"bounds_policy\":\"pathfinder_xy_closed\",\"bridge_width\":" + jsonReal(info.bridgeWidth)
				+ ",\"category_source\":\"TerrainLogic::getFirstBridge\",\"corners\":" + cornersJson
				+ ",\"from\":" + positionJson(info.from) + ",\"layer_id\":" + std::to_string(bridge->getLayer())
				+ ",\"object_id\":" + (info.bridgeObjectID == INVALID_ID ? "null" : std::to_string(info.bridgeObjectID))
				+ ",\"template_name\":" + jsonString(bridge->getBridgeTemplateName())
				+ ",\"to\":" + positionJson(info.to) + "}";
		}
		result.push_back(']');
		return result;
	}

	void addCategory(std::vector<std::pair<std::string, std::string>> &categories, const char *name, const char *source)
	{
		categories.emplace_back(name, source);
	}

	std::string buildStaticObjects(Real minimumX, Real minimumY, Real maximumX, Real maximumY)
	{
		std::vector<const Object *> objects;
		for (const Object *object = TheGameLogic->getFirstObject(); object != nullptr; object = object->getNextObject())
		{
			if (ReplayEntityLifecycle::getCreationSource(object) == REPLAY_ENTITY_CREATION_MAP_LOADED)
			{
				objects.push_back(object);
			}
		}
		std::sort(objects.begin(), objects.end(), [](const Object *left, const Object *right) {
			return left->getID() < right->getID();
		});
		static const NameKeyType warehouseKey = NAMEKEY("SupplyWarehouseDockUpdate");
		static const NameKeyType autoDepositKey = NAMEKEY("AutoDepositUpdate");
		std::string result("[");
		Bool firstObject = TRUE;
		for (const Object *object : objects)
		{
			std::vector<std::pair<std::string, std::string>> categories;
			if (object->isKindOf(KINDOF_BRIDGE)) addCategory(categories, "bridge", "ThingTemplate::isKindOf(KINDOF_BRIDGE)");
			if (object->isKindOf(KINDOF_CAPTURABLE)) addCategory(categories, "capturable", "ThingTemplate::isKindOf(KINDOF_CAPTURABLE)");
			if (object->isKindOf(KINDOF_CASH_GENERATOR)) addCategory(categories, "cash_generator", "ThingTemplate::isKindOf(KINDOF_CASH_GENERATOR)");
			if (object->findUpdateModule(autoDepositKey) != nullptr
				&& (object->isKindOf(KINDOF_CAPTURABLE) || object->isKindOf(KINDOF_TECH_BUILDING)))
			{
				addCategory(categories, "oil_income", "Object::findUpdateModule(AutoDepositUpdate)+capturable_or_tech_KindOf");
			}
			if (object->isKindOf(KINDOF_OBSTACLE)) addCategory(categories, "static_blocker", "ThingTemplate::isKindOf(KINDOF_OBSTACLE)");
			if (object->isKindOf(KINDOF_SUPPLY_SOURCE)) addCategory(categories, "supply_source", "ThingTemplate::isKindOf(KINDOF_SUPPLY_SOURCE)");
			if (object->findUpdateModule(warehouseKey) != nullptr) addCategory(categories, "supply_warehouse", "Object::findUpdateModule(SupplyWarehouseDockUpdate)");
			if (object->isKindOf(KINDOF_TECH_BUILDING)) addCategory(categories, "tech_building", "ThingTemplate::isKindOf(KINDOF_TECH_BUILDING)");
			if (categories.empty()) continue;
			const Coord3D &position = *object->getPosition();
			if (!insideXY(position, minimumX, minimumY, maximumX, maximumY))
			{
				fail("map_static_object_bounds", "classified static map object is outside pathfinder bounds");
				return "[]";
			}
			std::string categoryJson("[");
			for (size_t category = 0; category < categories.size(); ++category)
			{
				if (category != 0) categoryJson.push_back(',');
				categoryJson += "{\"name\":" + jsonUtf8(categories[category].first.c_str())
					+ ",\"source\":" + jsonUtf8(categories[category].second.c_str()) + "}";
			}
			categoryJson.push_back(']');
			if (!firstObject) result.push_back(',');
			firstObject = FALSE;
			const ThingTemplate *thingTemplate = object->getTemplate();
			if (thingTemplate == nullptr || thingTemplate->getName().isEmpty())
			{
				fail("map_static_object_template", "classified static map object has no stable template name");
				return "[]";
			}
			result += "{\"bounds_policy\":\"pathfinder_xy_closed\",\"categories\":" + categoryJson
				+ ",\"creation_source\":\"map_loaded\",\"object_id\":" + std::to_string(object->getID())
				+ ",\"orientation\":" + jsonReal(object->getOrientation()) + ",\"position\":" + positionJson(position)
				+ ",\"snapshot_scope\":\"post_map_initialization\",\"template_name\":"
				+ jsonString(thingTemplate->getName()) + "}";
		}
		result.push_back(']');
		return result;
	}

	Bool buildAsset(std::string &contentHash, std::string &manifest, std::vector<ExpectedFile> &files)
	{
		if (TheAI == nullptr || TheAI->pathfinder() == nullptr || TheTerrainLogic == nullptr || TheGameLogic == nullptr)
		{
			fail("map_not_initialized", "terrain, pathfinder, or map objects are unavailable");
			return FALSE;
		}
		IRegion2D extent;
		const Pathfinder *pathfinder = TheAI->pathfinder();
		if (!pathfinder->replayAnalyzerGetExtent(&extent))
		{
			fail("map_not_initialized", "pathfinder cells are not authoritatively initialized");
			return FALSE;
		}
		const long long width64 = static_cast<long long>(extent.hi.x) - extent.lo.x + 1;
		const long long height64 = static_cast<long long>(extent.hi.y) - extent.lo.y + 1;
		const long long count64 = width64 * height64;
		if (width64 <= 0 || height64 <= 0 || width64 > 16384 || height64 > 16384
			|| count64 <= 0 || count64 > MAX_GRID_ELEMENTS)
		{
			fail("map_dimensions", "pathfinder dimensions exceed the closed map-asset bounds");
			return FALSE;
		}
		const Int width = static_cast<Int>(width64);
		const Int height = static_cast<Int>(height64);
		const Int elementCount = static_cast<Int>(count64);
		MapMember heights = { "height.f32.zlib", "float32" };
		MapMember terrain = { "terrain.u8.zlib", "uint8" };
		MapMember ground = { "pathing-ground.u8.zlib", "uint8" };
		MapMember amphibious = { "pathing-amphibious.u8.zlib", "uint8" };
		MapMember zones = { "zones.i32.zlib", "int32" };
		heights.raw.reserve(static_cast<size_t>(elementCount) * 4);
		terrain.raw.reserve(static_cast<size_t>(elementCount));
		ground.raw.reserve(static_cast<size_t>(elementCount));
		amphibious.raw.reserve(static_cast<size_t>(elementCount));
		zones.raw.reserve(static_cast<size_t>(elementCount) * 4);
		Real minimumZ = std::numeric_limits<Real>::infinity();
		Real maximumZ = -std::numeric_limits<Real>::infinity();
		for (Int y = extent.lo.y; y <= extent.hi.y; ++y)
		{
			for (Int x = extent.lo.x; x <= extent.hi.x; ++x)
			{
				const PathfindCell *cell = pathfinder->replayAnalyzerGetGroundCell(x, y);
				if (cell == nullptr)
				{
					fail("map_cell_unavailable", "one initialized ground pathfinder cell is unavailable");
					return FALSE;
				}
				Real heightValue = TheTerrainLogic->getGroundHeight((static_cast<Real>(x) + 0.5f) * PATHFIND_CELL_SIZE_F,
					(static_cast<Real>(y) + 0.5f) * PATHFIND_CELL_SIZE_F);
				const char *injected = getenv("GENERALS_REPLAY_MAP_EXPORT_TEST_FAIL");
				if (x == extent.lo.x && y == extent.lo.y && injected != nullptr && strcmp(injected, "nonfinite") == 0)
				{
					heightValue = std::numeric_limits<Real>::infinity();
				}
				if (!std::isfinite(static_cast<double>(heightValue)))
				{
					fail("map_nonfinite_height", "initialized terrain height contains a nonfinite float32 value");
					return FALSE;
				}
				minimumZ = std::min(minimumZ, heightValue);
				maximumZ = std::max(maximumZ, heightValue);
				appendLittleFloat(heights.raw, heightValue);
				const PathfindCell::CellType type = cell->getType();
				if (type < PathfindCell::CELL_CLEAR || type > PathfindCell::CELL_IMPASSABLE || cell->getZone() > 16383)
				{
					fail("map_pathfinder_value", "initialized pathfinder cell has an out-of-contract type or zone");
					return FALSE;
				}
				terrain.raw.push_back(static_cast<UnsignedByte>(type));
				ground.raw.push_back(type == PathfindCell::CELL_CLEAR ? 1 : 0);
				amphibious.raw.push_back(type == PathfindCell::CELL_CLEAR || type == PathfindCell::CELL_WATER ? 1 : 0);
				appendLittleU32(zones.raw, static_cast<UnsignedInt>(cell->getZone()));
			}
		}
		std::vector<MapMember *> members = { &heights, &amphibious, &ground, &terrain, &zones };
		for (MapMember *member : members)
		{
			if (!compressMember(*member)) return FALSE;
		}

		const Real minimumX = static_cast<Real>(extent.lo.x) * PATHFIND_CELL_SIZE_F;
		const Real minimumY = static_cast<Real>(extent.lo.y) * PATHFIND_CELL_SIZE_F;
		const Real maximumX = static_cast<Real>(extent.hi.x + 1) * PATHFIND_CELL_SIZE_F;
		const Real maximumY = static_cast<Real>(extent.hi.y + 1) * PATHFIND_CELL_SIZE_F;
		const std::string bridges = buildBridges(minimumX, minimumY, maximumX, maximumY);
		const std::string starts = buildStartPositions(minimumX, minimumY, maximumX, maximumY);
		const std::string staticObjects = buildStaticObjects(minimumX, minimumY, maximumX, maximumY);
		const std::string waypoints = buildWaypoints(minimumX, minimumY, maximumX, maximumY);
		if (s_failed) return FALSE;

		const std::string gridBounds = "{\"maximum_exclusive\":" + position2Json(maximumX, maximumY)
			+ ",\"minimum_inclusive\":" + position2Json(minimumX, minimumY) + "}";
		const std::string grid = "{\"bounds\":" + gridBounds + ",\"cell_size\":"
			+ position2Json(PATHFIND_CELL_SIZE_F, PATHFIND_CELL_SIZE_F)
			+ ",\"dimension_source\":\"Pathfinder::replayAnalyzerGetExtent initialized inclusive IRegion2D\""
			+ ",\"height\":" + std::to_string(height) + ",\"index_origin\":{\"x\":" + std::to_string(extent.lo.x)
			+ ",\"y\":" + std::to_string(extent.lo.y) + "},\"sample_point\":\"cell_center\",\"width\":"
			+ std::to_string(width) + "}";
		std::string memberObject("{");
		for (size_t index = 0; index < members.size(); ++index)
		{
			if (index != 0) memberObject.push_back(',');
			memberObject += jsonUtf8(members[index]->name.c_str()) + ":" + memberJson(*members[index], elementCount);
		}
		memberObject.push_back('}');

		const std::string placeholderHash(64, '0');
		manifest = "{\"classification\":{\"amphibious_passable_cell_types\":[0,1],\"cell_types\":["
			"{\"name\":\"CELL_CLEAR\",\"value\":0},{\"name\":\"CELL_WATER\",\"value\":1},"
			"{\"name\":\"CELL_CLIFF\",\"value\":2},{\"name\":\"CELL_RUBBLE\",\"value\":3},"
			"{\"name\":\"CELL_OBSTACLE\",\"value\":4},{\"name\":\"CELL_BRIDGE_IMPASSABLE\",\"value\":5},"
			"{\"name\":\"CELL_IMPASSABLE\",\"value\":6}],\"ground_passable_cell_types\":[0],"
			"\"pathing_derivation_source\":\"Pathfinder::validLocomotorSurfacesForCellType\","
			"\"raw_cell_type_source\":\"PathfindCell::getType\",\"raw_zone_source\":\"PathfindCell::getZone\"},"
			"\"content_sha256\":\"" + placeholderHash + "\",\"coordinate_system\":{\"axes\":[\"engine_world_x\","
			"\"engine_world_y\",\"engine_world_z\"],\"bounds\":{\"maximum\":"
			+ positionJson({ maximumX, maximumY, maximumZ }) + ",\"maximum_inclusive\":true,\"minimum\":"
			+ positionJson({ minimumX, minimumY, minimumZ }) + ",\"minimum_inclusive\":true},"
			"\"entity_sample_policy\":{\"bounded_layer_statuses\":[\"stable\",\"dynamic_bridge_layer\"],"
			"\"bounded_position_policies\":[\"pathfinder_xy_closed\"],"
			"\"exempt_position_policies\":[\"exempt_kindof_aircraft\",\"exempt_kindof_bridge\","
			"\"exempt_kindof_projectile\",\"exempt_locomotor_air_surface\",\"exempt_module_wander_ai\","
			"\"exempt_physics_without_ai_pathing\"],"
			"\"policy\":\"pathfinder_xy_closed_except_explicit_engine_category\","
			"\"policy_source\":\"ReplayMovementSampler KindOf, current locomotor AIR surface, WanderAIUpdate, or physics without AI pathing\"},"
			"\"float_encoding\":\"IEEE-754-binary32\",\"units\":\"engine_world_unit\"},"
			"\"engine_data_identity\":" + jsonString(ReplayTelemetry::getEngineDataIdentity())
			+ ",\"features\":{\"bridges\":" + bridges + ",\"start_positions\":" + starts
			+ ",\"static_objects\":" + staticObjects + ",\"waypoints\":" + waypoints + "},"
			"\"grids\":{\"pathing\":" + grid + ",\"terrain\":" + grid + "},\"map_identity\":"
			+ jsonString(ReplayTelemetry::getMapIdentity()) + ",\"members\":" + memberObject
			+ ",\"producer\":{\"name\":\"zero-hour-replay-map-export\",\"version\":1,\"zlib_version\":"
			+ jsonUtf8(zlibVersion()) + "},\"schema_version\":1,\"type\":\"map_asset\"}\n";
		if (s_failed) return FALSE;
		contentHash = ReplayTelemetry::sha256Hex(manifest.data(), manifest.size()).str();
		const std::string hashField = "\"content_sha256\":\"" + placeholderHash + "\"";
		const size_t hashOffset = manifest.find(hashField);
		if (hashOffset == std::string::npos || manifest.find(hashField, hashOffset + 1) != std::string::npos)
		{
			fail("map_manifest_hash", "canonical manifest has an ambiguous content hash field");
			return FALSE;
		}
		manifest.replace(hashOffset + strlen("\"content_sha256\":\""), 64, contentHash);

		for (MapMember *member : members)
		{
			files.push_back({ member->name, member->compressed });
		}
		files.push_back({ "manifest.json", std::vector<UnsignedByte>(manifest.begin(), manifest.end()) });
		std::sort(files.begin(), files.end(), [](const ExpectedFile &left, const ExpectedFile &right) {
			return left.name < right.name;
		});
		return TRUE;
	}
}

void ReplayMapExport::reset()
{
	s_ready = FALSE;
	s_failed = FALSE;
	s_referenceJson.clear();
}

Bool ReplayMapExport::prepare()
{
	if (s_ready) return TRUE;
	s_failed = FALSE;
	std::string contentHash;
	std::string manifest;
	std::vector<ExpectedFile> files;
	if (!buildAsset(contentHash, manifest, files) || !publishAsset(contentHash, files)) return FALSE;
	const std::string manifestSha256 = ReplayTelemetry::sha256Hex(manifest.data(), manifest.size()).str();
	const std::string reference = "{\"type\":\"map_asset\",\"schema_version\":1,\"path\":\"map-assets-v1/"
		+ contentHash + "/manifest.json\",\"sha256\":\"" + manifestSha256 + "\",\"content_sha256\":\""
		+ contentHash + "\",\"engine_data_identity\":" + jsonString(ReplayTelemetry::getEngineDataIdentity())
		+ ",\"map_identity\":" + jsonString(ReplayTelemetry::getMapIdentity()) + "}";
	s_referenceJson = reference.c_str();
	s_ready = TRUE;
	return TRUE;
}

const AsciiString &ReplayMapExport::referenceJson()
{
	return s_referenceJson;
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
