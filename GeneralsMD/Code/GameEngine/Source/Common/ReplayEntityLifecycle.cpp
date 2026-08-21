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

#include "Common/ReplayEntityLifecycle.h"

#include "Common/KindOf.h"
#include "Common/ObjectStatusTypes.h"
#include "Common/Player.h"
#include "Common/ReplayTelemetry.h"
#include "Common/Team.h"
#include "Common/ThingTemplate.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Object.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <map>
#include <string>
#include <system_error>
#include <vector>

namespace
{
	struct NullableIdentity
	{
		Bool hasOwner;
		Int owner;
		Bool hasTeam;
		TeamID team;
	};

	struct CreationEntry
	{
		ObjectID objectId;
		UnsignedInt registrationFrame;
		unsigned long long creationOrder;
		unsigned long long initialConstructionOrder;
		std::string templateName;
		NullableIdentity initialIdentity;
		ObjectStatusMaskType initialStatus;
		std::vector<std::string> kindOfFlags;
		ReplayEntityCreationSource source;
		ObjectID initialProducerId;
		ObjectID initialBuilderId;
		Bool hasInitialProducerPlayer;
		Int initialProducerPlayer;
		Bool hasInitialResponsiblePlayer;
		Int initialResponsiblePlayer;
		Bool hasPosition;
		Coord3D position;
		Real orientation;
		Bool finalized;
		Bool sold;
		Bool destroyed;
	};

	struct PendingEvent
	{
		unsigned long long order;
		UnsignedInt frame;
		std::string eventType;
		std::string payload;
	};

	typedef std::map<ObjectID, CreationEntry> CreationMap;

	CreationMap s_creations;
	std::vector<PendingEvent> s_pendingEvents;
	unsigned long long s_nextObservationOrder = 0;
	UnsignedInt s_directCreationDepth = 0;

	UnsignedInt currentFrame()
	{
		return TheGameLogic != nullptr ? TheGameLogic->getFrame() : 0;
	}

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
			ReplayTelemetry::fail("nonfinite_lifecycle_number", "entity lifecycle contains a nonfinite coordinate or orientation");
			return FALSE;
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, 9);
		if (converted.ec != std::errc())
		{
			ReplayTelemetry::fail("lifecycle_number_format_failed", "could not serialize an entity lifecycle number");
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
		return value == INVALID_ID ? "null" : std::to_string(static_cast<UnsignedInt>(value));
	}

	NullableIdentity identityForTeam(const Team *team)
	{
		NullableIdentity result = { FALSE, 0, FALSE, 0 };
		if (team == nullptr)
		{
			return result;
		}
		result.hasTeam = TRUE;
		result.team = team->getID();
		const Player *player = team->getControllingPlayer();
		if (player != nullptr)
		{
			result.hasOwner = TRUE;
			result.owner = player->getPlayerIndex();
		}
		return result;
	}

	std::string identityFields(const NullableIdentity &identity)
	{
		return "\"owner_player_index\":" + nullableInt(identity.hasOwner, identity.owner)
			+ ",\"team_id\":" + (identity.hasTeam ? std::to_string(identity.team) : "null");
	}

	const char *creationSourceName(ReplayEntityCreationSource source)
	{
		switch (source)
		{
			case REPLAY_ENTITY_CREATION_MAP_LOADED: return "map_loaded";
			case REPLAY_ENTITY_CREATION_STARTING_OBJECT: return "starting_object";
			case REPLAY_ENTITY_CREATION_PLAYER_PRODUCTION: return "player_production";
			default: return "unknown";
		}
	}

	std::string stringArray(const std::vector<std::string> &values)
	{
		std::string result("[");
		for (size_t index = 0; index < values.size(); ++index)
		{
			if (index != 0)
			{
				result.push_back(',');
			}
			result += jsonString(values[index].c_str());
		}
		result.push_back(']');
		return result;
	}

	std::vector<std::string> statusNames(const ObjectStatusMaskType &status)
	{
		std::vector<std::string> result;
		const char *const *names = ObjectStatusMaskType::getBitNames();
		for (Int bit = OBJECT_STATUS_NONE; bit < OBJECT_STATUS_COUNT; ++bit)
		{
			if (status.test(bit) && names[bit] != nullptr)
			{
				result.emplace_back(names[bit]);
			}
		}
		return result;
	}

	std::vector<std::string> kindOfNames(const ThingTemplate *thingTemplate)
	{
		std::vector<std::string> result;
		for (Int kind = KINDOF_FIRST; kind < KINDOF_COUNT; ++kind)
		{
			if (thingTemplate->isKindOf(static_cast<KindOfType>(kind)))
			{
				const char *name = KindOfMaskType::getNameFromSingleBit(kind);
				if (name != nullptr)
				{
					result.emplace_back(name);
				}
			}
		}
		return result;
	}

	void queueEvent(unsigned long long order, UnsignedInt frame, const char *eventType, const std::string &payload)
	{
		PendingEvent event = { order, frame, eventType, payload };
		s_pendingEvents.push_back(event);
	}

	ObjectID responsibleObjectId(const Object *object)
	{
		if (object->getBuilderID() != INVALID_ID)
		{
			return object->getBuilderID();
		}
		return object->getProducerID();
	}

	std::string responsiblePlayer(const Object *object)
	{
		const ObjectID responsibleId = responsibleObjectId(object);
		const Object *responsible = responsibleId != INVALID_ID && TheGameLogic != nullptr
			? TheGameLogic->findObjectByID(responsibleId) : nullptr;
		const Player *player = responsible != nullptr ? responsible->getControllingPlayer() : nullptr;
		return player != nullptr ? std::to_string(player->getPlayerIndex()) : "null";
	}

	std::string constructionPayload(const Object *object, const char *previousState, const char *newState)
	{
		const NullableIdentity identity = identityForTeam(object->getTeam());
		return "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
			+ ",\"previous_state\":" + jsonString(previousState)
			+ ",\"new_state\":" + jsonString(newState) + "," + identityFields(identity)
			+ ",\"producer_object_id\":" + nullableObjectId(object->getProducerID())
			+ ",\"builder_object_id\":" + nullableObjectId(object->getBuilderID())
			+ ",\"responsible_player_index\":" + responsiblePlayer(object) + "}";
	}

	std::string initialConstructionPayload(const CreationEntry &entry)
	{
		return "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(entry.objectId))
			+ ",\"previous_state\":\"not_present\",\"new_state\":\"under_construction\"," + identityFields(entry.initialIdentity)
			+ ",\"producer_object_id\":" + nullableObjectId(entry.initialProducerId)
			+ ",\"builder_object_id\":" + nullableObjectId(entry.initialBuilderId)
			+ ",\"responsible_player_index\":"
			+ nullableInt(entry.hasInitialResponsiblePlayer, entry.initialResponsiblePlayer) + "}";
	}

	void flushEvents()
	{
		if (!ReplayTelemetry::isInitialized() || s_directCreationDepth != 0 || s_pendingEvents.empty())
		{
			return;
		}
		std::stable_sort(s_pendingEvents.begin(), s_pendingEvents.end(),
			[](const PendingEvent &left, const PendingEvent &right) { return left.order < right.order; });
		for (const PendingEvent &event : s_pendingEvents)
		{
			ReplayTelemetry::emit(event.frame, event.eventType.c_str(), AsciiString(event.payload.c_str()));
		}
		s_pendingEvents.clear();
	}

	void finalizeCreation(CreationEntry &entry, const Object *object)
	{
		if (entry.finalized)
		{
			return;
		}
		if (object == nullptr || object->getID() != entry.objectId)
		{
			ReplayTelemetry::fail("lifecycle_creation_missing", "registered entity was unavailable before object_created could be finalized");
			return;
		}
		std::string orientation;
		if (!jsonNumber(entry.orientation, orientation))
		{
			return;
		}
		std::string position = "null";
		if (entry.hasPosition)
		{
			std::string x;
			std::string y;
			std::string z;
			if (!jsonNumber(entry.position.x, x) || !jsonNumber(entry.position.y, y) || !jsonNumber(entry.position.z, z))
			{
				return;
			}
			position = "{\"x\":" + x + ",\"y\":" + y + ",\"z\":" + z + "}";
		}
		const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(entry.objectId))
			+ ",\"template_name\":" + jsonString(entry.templateName.c_str()) + "," + identityFields(entry.initialIdentity)
			+ ",\"position_status\":" + jsonString(entry.hasPosition ? "placed" : "unplaced")
			+ ",\"position\":" + position + ",\"orientation\":" + orientation
			+ ",\"kind_of_flags\":" + stringArray(entry.kindOfFlags)
			+ ",\"initial_status\":" + stringArray(statusNames(entry.initialStatus))
			+ ",\"creation_source\":" + jsonString(creationSourceName(entry.source))
			+ ",\"creation_context\":{\"registration_frame\":" + std::to_string(entry.registrationFrame)
			+ ",\"producer_object_id\":" + nullableObjectId(entry.initialProducerId)
			+ ",\"producer_player_index\":"
			+ nullableInt(entry.hasInitialProducerPlayer, entry.initialProducerPlayer) + "}}";
		queueEvent(entry.creationOrder, entry.registrationFrame, "object_created", payload);
		if (entry.initialConstructionOrder != 0)
		{
			queueEvent(entry.initialConstructionOrder, entry.registrationFrame, "construction_started",
				initialConstructionPayload(entry));
		}
		entry.finalized = TRUE;
	}

	CreationMap::iterator findCreation(const Object *object)
	{
		return object != nullptr ? s_creations.find(object->getID()) : s_creations.end();
	}

	void finalizeThrough(unsigned long long maximumOrder)
	{
		if (s_directCreationDepth != 0)
		{
			return;
		}
		for (CreationMap::iterator it = s_creations.begin(); it != s_creations.end(); ++it)
		{
			CreationEntry &entry = it->second;
			if (entry.finalized || entry.creationOrder > maximumOrder)
			{
				continue;
			}
			const Object *object = TheGameLogic != nullptr ? TheGameLogic->findObjectByID(entry.objectId) : nullptr;
			if (object != nullptr)
			{
				finalizeCreation(entry, object);
			}
			else if (!entry.destroyed)
			{
				ReplayTelemetry::fail("lifecycle_creation_missing", "live registered entity disappeared before object_created was emitted");
			}
		}
	}
}

ReplayEntityCreationScope::ReplayEntityCreationScope(ReplayEntityCreationSource source) :
	m_source(source)
{
	ReplayEntityLifecycle::beginDirectCreation();
}

ReplayEntityCreationScope::~ReplayEntityCreationScope()
{
	ReplayEntityLifecycle::endDirectCreation();
}

void ReplayEntityCreationScope::observeReturned(const Object *object)
{
	ReplayEntityLifecycle::markCreationSource(object, m_source);
}

void ReplayEntityLifecycle::beginDirectCreation()
{
	++s_directCreationDepth;
}

void ReplayEntityLifecycle::endDirectCreation()
{
	DEBUG_ASSERTCRASH(s_directCreationDepth != 0, ("unbalanced replay entity direct-creation scope"));
	if (s_directCreationDepth != 0)
	{
		--s_directCreationDepth;
	}
}

void ReplayEntityLifecycle::markCreationSource(const Object *object, ReplayEntityCreationSource source)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end())
	{
		return;
	}
	if (it->second.finalized)
	{
		ReplayTelemetry::fail("creation_source_after_finalization", "direct creation source arrived after object_created finalization");
		return;
	}
	it->second.source = source;
}

void ReplayEntityLifecycle::reset()
{
	// TheSuperHackers @feature Leex 20/08/2026 Reset all trace-local IDs and immutable snapshots between replay runs. (#TBD)
	s_creations.clear();
	s_pendingEvents.clear();
	s_nextObservationOrder = 0;
	s_directCreationDepth = 0;
}

ReplayEntityCreationSource ReplayEntityLifecycle::getCreationSource(const Object *object)
{
	if (object == nullptr)
	{
		return REPLAY_ENTITY_CREATION_UNKNOWN;
	}
	const CreationMap::const_iterator entry = s_creations.find(object->getID());
	return entry != s_creations.end() ? entry->second.source : REPLAY_ENTITY_CREATION_UNKNOWN;
}

void ReplayEntityLifecycle::observeRegistered(const Object *object)
{
	if (!ReplayTelemetry::isEnabled() || object == nullptr || object->getID() == INVALID_ID)
	{
		return;
	}
	if (s_creations.find(object->getID()) != s_creations.end())
	{
		ReplayTelemetry::fail("duplicate_object_registration", "an object ID was registered more than once in one replay trace");
		return;
	}
	CreationEntry entry;
	entry.objectId = object->getID();
	entry.registrationFrame = currentFrame();
	entry.creationOrder = ++s_nextObservationOrder;
	entry.initialConstructionOrder = object->getStatusBits().test(OBJECT_STATUS_UNDER_CONSTRUCTION)
		? ++s_nextObservationOrder : 0;
	entry.templateName = object->getTemplate()->getName().str();
	entry.initialIdentity = identityForTeam(object->getTeam());
	entry.initialStatus = object->getStatusBits();
	entry.kindOfFlags = kindOfNames(object->getTemplate());
	entry.source = REPLAY_ENTITY_CREATION_UNKNOWN;
	entry.initialProducerId = object->getProducerID();
	entry.initialBuilderId = object->getBuilderID();
	entry.hasInitialProducerPlayer = FALSE;
	entry.initialProducerPlayer = 0;
	entry.hasInitialResponsiblePlayer = FALSE;
	entry.initialResponsiblePlayer = 0;
	const Object *producer = entry.initialProducerId != INVALID_ID && TheGameLogic != nullptr
		? TheGameLogic->findObjectByID(entry.initialProducerId) : nullptr;
	const Player *producerPlayer = producer != nullptr ? producer->getControllingPlayer() : nullptr;
	if (producerPlayer != nullptr)
	{
		entry.hasInitialProducerPlayer = TRUE;
		entry.initialProducerPlayer = producerPlayer->getPlayerIndex();
	}
	const ObjectID responsibleId = entry.initialBuilderId != INVALID_ID
		? entry.initialBuilderId : entry.initialProducerId;
	const Object *responsible = responsibleId != INVALID_ID && TheGameLogic != nullptr
		? TheGameLogic->findObjectByID(responsibleId) : nullptr;
	const Player *responsiblePlayer = responsible != nullptr ? responsible->getControllingPlayer() : nullptr;
	if (responsiblePlayer != nullptr)
	{
		entry.hasInitialResponsiblePlayer = TRUE;
		entry.initialResponsiblePlayer = responsiblePlayer->getPlayerIndex();
	}
	entry.hasPosition = FALSE;
	entry.position.x = 0.0f;
	entry.position.y = 0.0f;
	entry.position.z = 0.0f;
	entry.orientation = object->getOrientation();
	entry.finalized = FALSE;
	entry.sold = FALSE;
	entry.destroyed = FALSE;
	s_creations.insert(std::make_pair(entry.objectId, entry));
}

void ReplayEntityLifecycle::observePositionSet(const Object *object)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end())
	{
		return;
	}
	if (!it->second.hasPosition)
	{
		it->second.position = *object->getPosition();
		it->second.orientation = object->getOrientation();
		it->second.hasPosition = TRUE;
	}
}

void ReplayEntityLifecycle::observeTransform(const Object *object, Bool positionChanged)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end())
	{
		return;
	}
	if (positionChanged && !it->second.hasPosition)
	{
		it->second.position = *object->getPosition();
		it->second.orientation = object->getOrientation();
		it->second.hasPosition = TRUE;
	}
}

void ReplayEntityLifecycle::ensureObjectCreated(const Object *object)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end())
	{
		if (ReplayTelemetry::isEnabled())
		{
			ReplayTelemetry::fail("object_reference_without_registration", "an entity reference has no authoritative registration snapshot");
		}
		return;
	}
	finalizeThrough(it->second.creationOrder);
	flushEvents();
}

void ReplayEntityLifecycle::observeTeamChanged(const Object *object, const Team *previousTeam, const Team *newTeam)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end() || it->second.destroyed)
	{
		return;
	}
	ensureObjectCreated(object);
	const NullableIdentity previous = identityForTeam(previousTeam);
	const NullableIdentity next = identityForTeam(newTeam);
	const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
		+ ",\"previous_owner_player_index\":" + nullableInt(previous.hasOwner, previous.owner)
		+ ",\"new_owner_player_index\":" + nullableInt(next.hasOwner, next.owner)
		+ ",\"previous_team_id\":" + (previous.hasTeam ? std::to_string(previous.team) : "null")
		+ ",\"new_team_id\":" + (next.hasTeam ? std::to_string(next.team) : "null") + "}";
	queueEvent(++s_nextObservationOrder, currentFrame(), "owner_changed", payload);
	flushEvents();
}

void ReplayEntityLifecycle::observeStatusChanged(const Object *object, Bool previousUnderConstruction,
	Bool newUnderConstruction, Bool previousSold, Bool newSold)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end() || it->second.destroyed)
	{
		return;
	}
	if (previousUnderConstruction != newUnderConstruction)
	{
		ensureObjectCreated(object);
		queueEvent(++s_nextObservationOrder, currentFrame(),
			newUnderConstruction ? "construction_started" : "construction_completed",
			constructionPayload(object, newUnderConstruction ? "complete" : "under_construction",
				newUnderConstruction ? "under_construction" : "complete"));
	}
	if (!previousSold && newSold && !it->second.sold)
	{
		ensureObjectCreated(object);
		const NullableIdentity identity = identityForTeam(object->getTeam());
		const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
			+ ",\"previous_state\":\"available\",\"new_state\":\"sold\"," + identityFields(identity) + "}";
		queueEvent(++s_nextObservationOrder, currentFrame(), "sold", payload);
		it->second.sold = TRUE;
	}
	flushEvents();
}

void ReplayEntityLifecycle::observeDestroyed(const Object *object)
{
	CreationMap::iterator it = findCreation(object);
	if (it == s_creations.end() || it->second.destroyed)
	{
		return;
	}
	finalizeThrough(it->second.creationOrder);
	flushEvents();
	const NullableIdentity identity = identityForTeam(object->getTeam());
	const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
		+ ",\"previous_state\":" + jsonString(it->second.sold ? "sold" : "alive")
		+ ",\"new_state\":\"destroyed\"," + identityFields(identity)
		+ ",\"destruction_source\":\"destroy_object\"}";
	queueEvent(++s_nextObservationOrder, currentFrame(), "object_destroyed", payload);
	it->second.destroyed = TRUE;
	flushEvents();
}

void ReplayEntityLifecycle::initialize()
{
	// TheSuperHackers @feature Leex 20/08/2026 Flush copied pre-initialization lifecycle observations only after players_initialized. (#TBD)
	finalizeThrough(~0ULL);
	flushEvents();
}

void ReplayEntityLifecycle::flushPendingCreations()
{
	finalizeThrough(~0ULL);
	flushEvents();
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
