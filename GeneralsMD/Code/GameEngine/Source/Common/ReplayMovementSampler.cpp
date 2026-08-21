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

#include "Common/ReplayMovementSampler.h"

#include "Common/KindOf.h"
#include "Common/MessageStream.h"
#include "Common/Player.h"
#include "Common/Recorder.h"
#include "Common/ReplayEntityLifecycle.h"
#include "Common/ReplayTelemetry.h"
#include "Common/ThingTemplate.h"
#include "GameLogic/AIPathfind.h"
#include "GameLogic/AIStateMachine.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Locomotor.h"
#include "GameLogic/Module/AIUpdate.h"
#include "GameLogic/Module/ContainModule.h"
#include "GameLogic/Module/PhysicsUpdate.h"
#include "GameLogic/Object.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <system_error>
#include <vector>

namespace
{
	enum OrderTargetKind
	{
		ORDER_TARGET_NONE,
		ORDER_TARGET_OBJECT,
		ORDER_TARGET_LOCATION
	};

	struct SupportedOrder
	{
		GameMessage::Type messageType;
		const char *messageName;
		OrderTargetKind targetKind;
		Int targetArgumentIndex;
	};

	// TheSuperHackers @feature Leex 20/08/2026 Declare a closed source-audited subset instead of claiming complete GameLogicDispatch order coverage. (#TBD)
	const SupportedOrder s_supportedOrders[] = {
		{ GameMessage::MSG_COMBATDROP_AT_LOCATION, "MSG_COMBATDROP_AT_LOCATION", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_COMBATDROP_AT_OBJECT, "MSG_COMBATDROP_AT_OBJECT", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_ATTACK_OBJECT, "MSG_DO_ATTACK_OBJECT", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_FORCE_ATTACK_OBJECT, "MSG_DO_FORCE_ATTACK_OBJECT", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_FORCE_ATTACK_GROUND, "MSG_DO_FORCE_ATTACK_GROUND", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_GET_REPAIRED, "MSG_GET_REPAIRED", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_GET_HEALED, "MSG_GET_HEALED", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_REPAIR, "MSG_DO_REPAIR", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_RESUME_CONSTRUCTION, "MSG_RESUME_CONSTRUCTION", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_ENTER, "MSG_ENTER", ORDER_TARGET_OBJECT, 1 },
		{ GameMessage::MSG_DOCK, "MSG_DOCK", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_MOVETO, "MSG_DO_MOVETO", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_DO_ATTACKMOVETO, "MSG_DO_ATTACKMOVETO", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_DO_FORCEMOVETO, "MSG_DO_FORCEMOVETO", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_ADD_WAYPOINT, "MSG_ADD_WAYPOINT", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_DO_GUARD_POSITION, "MSG_DO_GUARD_POSITION", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_DO_GUARD_OBJECT, "MSG_DO_GUARD_OBJECT", ORDER_TARGET_OBJECT, 0 },
		{ GameMessage::MSG_DO_STOP, "MSG_DO_STOP", ORDER_TARGET_NONE, -1 },
		{ GameMessage::MSG_DO_SCATTER, "MSG_DO_SCATTER", ORDER_TARGET_NONE, -1 },
		{ GameMessage::MSG_DO_SALVAGE, "MSG_DO_SALVAGE", ORDER_TARGET_LOCATION, 0 },
		{ GameMessage::MSG_CREATE_FORMATION, "MSG_CREATE_FORMATION", ORDER_TARGET_NONE, -1 },
	};

	struct OrderReference
	{
		UnsignedInt orderId;
		Int messageType;
		std::string messageName;
	};

	struct EngineStateSnapshot
	{
		std::string classification;
		std::string classificationSource;
		Bool hasAiState;
		Int aiStateId;
		Bool hasAiStateName;
		std::string aiStateName;
		Bool hasLocomotorSet;
		Int locomotorSetId;
		Bool hasLocomotorSetName;
		std::string locomotorSetName;
		Bool engineMoving;
	};

	struct EngineSampleSnapshot
	{
		Coord3D position;
		Real orientation;
		Int layerId;
		Bool hasSpeed;
		Real speed;
		EngineStateSnapshot state;
		Bool hasPathGoal;
		Coord3D pathGoal;
		std::string pathGoalStatus;
		Bool mobile;
		Bool structure;
		Bool disabled;
		Bool hasOrder;
		OrderReference order;
	};

	struct SampleState
	{
		Bool hasState;
		EngineStateSnapshot state;
		Bool hasSample;
		EngineSampleSnapshot sample;
		UnsignedInt lastSampleFrame;
	};

	typedef std::map<ObjectID, SampleState> SampleStateMap;
	typedef std::map<ObjectID, OrderReference> OrderReferenceMap;
	typedef std::map<ObjectID, UnsignedInt> ForcedSampleMap;

	SampleStateMap s_sampleStates;
	OrderReferenceMap s_currentOrders;
	ForcedSampleMap s_forcedSamples;
	UnsignedInt s_nextOrderId = 1;

	const UnsignedInt FORCE_ORDER = 1U << 0;
	const UnsignedInt FORCE_STATE = 1U << 1;

	const SupportedOrder *supportedOrder(GameMessage::Type type)
	{
		for (const SupportedOrder &supported : s_supportedOrders)
		{
			if (supported.messageType == type)
			{
				return &supported;
			}
		}
		return nullptr;
	}

	const char *targetKindName(OrderTargetKind kind)
	{
		switch (kind)
		{
			case ORDER_TARGET_OBJECT: return "object";
			case ORDER_TARGET_LOCATION: return "location";
			default: return "none";
		}
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
			ReplayTelemetry::fail("nonfinite_entity_sample", "order or entity sample contains a nonfinite engine Real");
			return FALSE;
		}
		char buffer[64];
		const std::to_chars_result converted = std::to_chars(buffer, buffer + sizeof(buffer), value,
			std::chars_format::general, 9);
		if (converted.ec != std::errc())
		{
			ReplayTelemetry::fail("entity_sample_number_format_failed", "could not serialize an engine Real observation");
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

	const char *aiStateName(Int stateId)
	{
		switch (stateId)
		{
			case AI_IDLE: return "AI_IDLE";
			case AI_MOVE_TO: return "AI_MOVE_TO";
			case AI_FOLLOW_WAYPOINT_PATH_AS_TEAM: return "AI_FOLLOW_WAYPOINT_PATH_AS_TEAM";
			case AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS: return "AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS";
			case AI_FOLLOW_WAYPOINT_PATH_AS_TEAM_EXACT: return "AI_FOLLOW_WAYPOINT_PATH_AS_TEAM_EXACT";
			case AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS_EXACT: return "AI_FOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS_EXACT";
			case AI_FOLLOW_PATH: return "AI_FOLLOW_PATH";
			case AI_FOLLOW_EXITPRODUCTION_PATH: return "AI_FOLLOW_EXITPRODUCTION_PATH";
			case AI_WAIT: return "AI_WAIT";
			case AI_ATTACK_POSITION: return "AI_ATTACK_POSITION";
			case AI_ATTACK_OBJECT: return "AI_ATTACK_OBJECT";
			case AI_FORCE_ATTACK_OBJECT: return "AI_FORCE_ATTACK_OBJECT";
			case AI_ATTACK_AND_FOLLOW_OBJECT: return "AI_ATTACK_AND_FOLLOW_OBJECT";
			case AI_DEAD: return "AI_DEAD";
			case AI_DOCK: return "AI_DOCK";
			case AI_ENTER: return "AI_ENTER";
			case AI_GUARD: return "AI_GUARD";
			case AI_HUNT: return "AI_HUNT";
			case AI_WANDER: return "AI_WANDER";
			case AI_PANIC: return "AI_PANIC";
			case AI_ATTACK_SQUAD: return "AI_ATTACK_SQUAD";
			case AI_GUARD_TUNNEL_NETWORK: return "AI_GUARD_TUNNEL_NETWORK";
			case AI_GET_REPAIRED: return "AI_GET_REPAIRED";
			case AI_MOVE_OUT_OF_THE_WAY: return "AI_MOVE_OUT_OF_THE_WAY";
			case AI_MOVE_AND_TIGHTEN: return "AI_MOVE_AND_TIGHTEN";
			case AI_MOVE_AND_EVACUATE: return "AI_MOVE_AND_EVACUATE";
			case AI_MOVE_AND_EVACUATE_AND_EXIT: return "AI_MOVE_AND_EVACUATE_AND_EXIT";
			case AI_MOVE_AND_DELETE: return "AI_MOVE_AND_DELETE";
			case AI_ATTACK_AREA: return "AI_ATTACK_AREA";
			case AI_HACK_INTERNET: return "AI_HACK_INTERNET";
			case AI_ATTACK_MOVE_TO: return "AI_ATTACK_MOVE_TO";
			case AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS: return "AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_INDIVIDUALS";
			case AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_TEAM: return "AI_ATTACKFOLLOW_WAYPOINT_PATH_AS_TEAM";
			case AI_FACE_OBJECT: return "AI_FACE_OBJECT";
			case AI_FACE_POSITION: return "AI_FACE_POSITION";
			case AI_RAPPEL_INTO: return "AI_RAPPEL_INTO";
			case AI_COMBATDROP: return "AI_COMBATDROP";
			case AI_EXIT: return "AI_EXIT";
			case AI_PICK_UP_CRATE: return "AI_PICK_UP_CRATE";
			case AI_MOVE_AWAY_FROM_REPULSORS: return "AI_MOVE_AWAY_FROM_REPULSORS";
			case AI_WANDER_IN_PLACE: return "AI_WANDER_IN_PLACE";
			case AI_BUSY: return "AI_BUSY";
			case AI_EXIT_INSTANTLY: return "AI_EXIT_INSTANTLY";
			case AI_GUARD_RETALIATE: return "AI_GUARD_RETALIATE";
			default: return nullptr;
		}
	}

	const char *locomotorSetName(Int locomotorSetId)
	{
		switch (locomotorSetId)
		{
			case LOCOMOTORSET_INVALID: return "LOCOMOTORSET_INVALID";
			case LOCOMOTORSET_NORMAL: return "LOCOMOTORSET_NORMAL";
			case LOCOMOTORSET_NORMAL_UPGRADED: return "LOCOMOTORSET_NORMAL_UPGRADED";
			case LOCOMOTORSET_FREEFALL: return "LOCOMOTORSET_FREEFALL";
			case LOCOMOTORSET_WANDER: return "LOCOMOTORSET_WANDER";
			case LOCOMOTORSET_PANIC: return "LOCOMOTORSET_PANIC";
			case LOCOMOTORSET_TAXIING: return "LOCOMOTORSET_TAXIING";
			case LOCOMOTORSET_SUPERSONIC: return "LOCOMOTORSET_SUPERSONIC";
			case LOCOMOTORSET_SLUGGISH: return "LOCOMOTORSET_SLUGGISH";
			default: return nullptr;
		}
	}

	Bool isDirectGarrison(const Object *object)
	{
		const Object *container = object->getContainedBy();
		const ContainModuleInterface *contain = container != nullptr ? container->getContain() : nullptr;
		return contain != nullptr && contain->isGarrisonable();
	}

	EngineStateSnapshot engineState(const Object *object)
	{
		EngineStateSnapshot result = {
			"unknown", "ai_interface_unavailable", FALSE, 0, FALSE, "", FALSE, 0, FALSE, "", FALSE
		};
		const AIUpdateInterface *ai = object->getAIUpdateInterface();
		if (ai != nullptr)
		{
			result.hasAiState = TRUE;
			result.aiStateId = static_cast<Int>(ai->getCurrentStateID());
			const char *stableName = aiStateName(result.aiStateId);
			result.hasAiStateName = stableName != nullptr;
			result.aiStateName = stableName != nullptr ? stableName : "";
			result.hasLocomotorSet = TRUE;
			result.locomotorSetId = static_cast<Int>(ai->getCurLocomotorSetType());
			const char *stableLocomotorName = locomotorSetName(result.locomotorSetId);
			result.hasLocomotorSetName = stableLocomotorName != nullptr;
			result.locomotorSetName = stableLocomotorName != nullptr ? stableLocomotorName : "";
			result.engineMoving = ai->isMoving();
		}
		if (object->isDisabled())
		{
			result.classification = "disabled";
			result.classificationSource = "object_disabled";
		}
		else if (isDirectGarrison(object))
		{
			result.classification = "garrisoned";
			result.classificationSource = "enclosing_garrison_container";
		}
		else if (ai != nullptr && ai->isAttacking())
		{
			result.classification = "attacking";
			result.classificationSource = "ai_attack_state";
		}
		else if (ai != nullptr && (result.aiStateId == AI_GUARD || result.aiStateId == AI_GUARD_TUNNEL_NETWORK
			|| result.aiStateId == AI_GUARD_RETALIATE))
		{
			result.classification = "guarding";
			result.classificationSource = "ai_guard_state";
		}
		else if (ai != nullptr && ai->isMoving())
		{
			result.classification = "moving";
			result.classificationSource = "ai_moving_state";
		}
		else if (ai != nullptr && ai->isIdle())
		{
			result.classification = "idle";
			result.classificationSource = "ai_idle_state";
		}
		else if (ai != nullptr)
		{
			result.classification = "unknown";
			result.classificationSource = "ai_state_unclassified";
		}
		return result;
	}

	Bool sameReal(Real left, Real right)
	{
		return std::memcmp(&left, &right, sizeof(Real)) == 0;
	}

	Bool samePosition(const Coord3D &left, const Coord3D &right)
	{
		return sameReal(left.x, right.x) && sameReal(left.y, right.y) && sameReal(left.z, right.z);
	}

	Bool sameState(const EngineStateSnapshot &left, const EngineStateSnapshot &right)
	{
		return left.classification == right.classification
			&& left.classificationSource == right.classificationSource
			&& left.hasAiState == right.hasAiState
			&& (!left.hasAiState || left.aiStateId == right.aiStateId)
			&& left.hasAiStateName == right.hasAiStateName
			&& (!left.hasAiStateName || left.aiStateName == right.aiStateName)
			&& left.hasLocomotorSet == right.hasLocomotorSet
			&& (!left.hasLocomotorSet || left.locomotorSetId == right.locomotorSetId)
			&& left.hasLocomotorSetName == right.hasLocomotorSetName
			&& (!left.hasLocomotorSetName || left.locomotorSetName == right.locomotorSetName)
			&& left.engineMoving == right.engineMoving;
	}

	Bool sameSample(const EngineSampleSnapshot &left, const EngineSampleSnapshot &right)
	{
		return samePosition(left.position, right.position)
			&& sameReal(left.orientation, right.orientation)
			&& left.layerId == right.layerId
			&& left.hasSpeed == right.hasSpeed
			&& (!left.hasSpeed || sameReal(left.speed, right.speed))
			&& sameState(left.state, right.state)
			&& left.hasPathGoal == right.hasPathGoal
			&& (!left.hasPathGoal || samePosition(left.pathGoal, right.pathGoal))
			&& left.pathGoalStatus == right.pathGoalStatus
			&& left.mobile == right.mobile
			&& left.structure == right.structure
			&& left.disabled == right.disabled
			&& left.hasOrder == right.hasOrder
			&& (!left.hasOrder || (left.order.orderId == right.order.orderId
				&& left.order.messageType == right.order.messageType
				&& left.order.messageName == right.order.messageName));
	}

	EngineSampleSnapshot engineSample(Object *object)
	{
		EngineSampleSnapshot result;
		result.position = *object->getPosition();
		result.orientation = object->getOrientation();
		result.layerId = static_cast<Int>(object->getLayer());
		const PhysicsBehavior *physics = object->getPhysics();
		result.hasSpeed = physics != nullptr;
		result.speed = physics != nullptr ? physics->getVelocityMagnitude() : 0.0f;
		result.state = engineState(object);
		result.hasPathGoal = FALSE;
		result.pathGoal.zero();
		AIUpdateInterface *ai = object->getAIUpdateInterface();
		if (ai == nullptr)
		{
			result.pathGoalStatus = "unavailable_no_ai";
		}
		else if (ai->getPath() == nullptr)
		{
			result.pathGoalStatus = "unavailable_no_path";
		}
		else if (ai->getPath()->getLastNode() == nullptr)
		{
			result.pathGoalStatus = "unavailable_empty_path";
		}
		else
		{
			result.hasPathGoal = TRUE;
			result.pathGoal = *ai->getPath()->getLastNode()->getPosition();
			result.pathGoalStatus = "path_tail";
		}
		result.mobile = object->isMobile();
		result.structure = object->getTemplate()->isKindOf(KINDOF_STRUCTURE);
		result.disabled = object->isDisabled();
		const OrderReferenceMap::const_iterator order = s_currentOrders.find(object->getID());
		result.hasOrder = order != s_currentOrders.end();
		result.order = result.hasOrder ? order->second : OrderReference{ 0, 0, "" };
		return result;
	}

	std::string nullableOwner(const Object *object)
	{
		const Player *owner = object->getControllingPlayer();
		return owner != nullptr ? std::to_string(owner->getPlayerIndex()) : "null";
	}

	std::string nullableAiStateId(const EngineStateSnapshot &state)
	{
		return state.hasAiState ? std::to_string(state.aiStateId) : "null";
	}

	std::string nullableAiStateName(const EngineStateSnapshot &state)
	{
		return state.hasAiStateName ? jsonString(state.aiStateName.c_str()) : "null";
	}

	const char *aiStateNameStatus(const EngineStateSnapshot &state)
	{
		return !state.hasAiState ? "unavailable_no_ai"
			: (state.hasAiStateName ? "stable" : "unknown_engine_value");
	}

	std::string nullableLocomotorSet(const EngineStateSnapshot &state)
	{
		return state.hasLocomotorSet ? std::to_string(state.locomotorSetId) : "null";
	}

	std::string nullableLocomotorSetName(const EngineStateSnapshot &state)
	{
		return state.hasLocomotorSetName ? jsonString(state.locomotorSetName.c_str()) : "null";
	}

	const char *locomotorSetNameStatus(const EngineStateSnapshot &state)
	{
		return !state.hasLocomotorSet ? "unavailable_no_ai"
			: (state.hasLocomotorSetName ? "stable" : "unknown_engine_value");
	}

	std::string nullableOrderId(ObjectID objectId)
	{
		const OrderReferenceMap::const_iterator order = s_currentOrders.find(objectId);
		return order != s_currentOrders.end() ? std::to_string(order->second.orderId) : "null";
	}

	Bool emitStateTransition(UnsignedInt frame, const Object *object, const EngineStateSnapshot &previous,
		const EngineStateSnapshot &current)
	{
		const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
			+ ",\"template_name\":" + jsonString(object->getTemplate()->getName().str())
			+ ",\"owner_player_index\":" + nullableOwner(object)
			+ ",\"previous_state\":" + jsonString(previous.classification.c_str())
			+ ",\"previous_state_source\":" + jsonString(previous.classificationSource.c_str())
			+ ",\"current_state\":" + jsonString(current.classification.c_str())
			+ ",\"current_state_source\":" + jsonString(current.classificationSource.c_str())
			+ ",\"previous_ai_state_id\":" + nullableAiStateId(previous)
			+ ",\"current_ai_state_id\":" + nullableAiStateId(current)
			+ ",\"previous_ai_state_name\":" + nullableAiStateName(previous)
			+ ",\"current_ai_state_name\":" + nullableAiStateName(current)
			+ ",\"previous_ai_state_name_status\":" + jsonString(aiStateNameStatus(previous))
			+ ",\"current_ai_state_name_status\":" + jsonString(aiStateNameStatus(current))
			+ ",\"previous_locomotor_set_id\":" + nullableLocomotorSet(previous)
			+ ",\"current_locomotor_set_id\":" + nullableLocomotorSet(current)
			+ ",\"previous_locomotor_set_name\":" + nullableLocomotorSetName(previous)
			+ ",\"current_locomotor_set_name\":" + nullableLocomotorSetName(current)
			+ ",\"previous_locomotor_set_name_status\":" + jsonString(locomotorSetNameStatus(previous))
			+ ",\"current_locomotor_set_name_status\":" + jsonString(locomotorSetNameStatus(current))
			+ ",\"previous_is_engine_moving\":" + (previous.engineMoving ? "true" : "false")
			+ ",\"current_is_engine_moving\":" + (current.engineMoving ? "true" : "false")
			+ ",\"current_order_id\":" + nullableOrderId(object->getID())
			+ ",\"transition_source\":\"end_of_game_logic_update\"}";
		ReplayTelemetry::emit(frame, "entity_state_changed", AsciiString(payload.c_str()));
		return TRUE;
	}

	const char *layerName(Int layerId)
	{
		switch (layerId)
		{
			case LAYER_INVALID: return "LAYER_INVALID";
			case LAYER_GROUND: return "LAYER_GROUND";
			case LAYER_WALL: return "LAYER_WALL";
			default: return nullptr;
		}
	}

	// TheSuperHackers @feature Leex 21/08/2026 Bind coordinate exemptions only to independently catalog-verifiable engine evidence. (#TBD)
	const char *positionBoundsPolicy(const Object *object)
	{
		if (object->isKindOf(KINDOF_AIRCRAFT)) return "exempt_kindof_aircraft";
		if (object->isKindOf(KINDOF_BRIDGE)) return "exempt_kindof_bridge";
		if (object->isKindOf(KINDOF_PROJECTILE)) return "exempt_kindof_projectile";
		if (object->isKindOf(KINDOF_PARACHUTABLE)) return "exempt_kindof_parachutable";
		const AIUpdateInterface *ai = object->getAI();
		const Locomotor *locomotor = ai != nullptr ? ai->getCurLocomotor() : nullptr;
		if (locomotor != nullptr && (locomotor->getLegalSurfaces() & LOCOMOTORSURFACE_AIR) != 0)
			return "exempt_locomotor_air_surface";
		return "pathfinder_xy_closed";
	}

	std::string currentLocomotorTemplateName(const Object *object)
	{
		const AIUpdateInterface *ai = object->getAI();
		const Locomotor *locomotor = ai != nullptr ? ai->getCurLocomotor() : nullptr;
		return locomotor != nullptr ? jsonString(locomotor->getTemplateName().str()) : "null";
	}

	Bool emitSample(UnsignedInt frame, const Object *object, const EngineSampleSnapshot &sample, const char *reason)
	{
		std::string position;
		std::string orientation;
		std::string speed;
		std::string pathGoal = "null";
		if (!positionJson(sample.position, position) || !jsonNumber(sample.orientation, orientation)
			|| (sample.hasSpeed && !jsonNumber(sample.speed, speed))
			|| (sample.hasPathGoal && !positionJson(sample.pathGoal, pathGoal)))
		{
			return FALSE;
		}
		const char *stableLayerName = layerName(sample.layerId);
		const char *layerStatus = stableLayerName != nullptr ? "stable"
			: (sample.layerId > LAYER_GROUND && sample.layerId < LAYER_WALL ? "dynamic_bridge_layer" : "unknown_engine_value");
		const std::string orderId = sample.hasOrder ? std::to_string(sample.order.orderId) : "null";
		const std::string orderType = sample.hasOrder ? std::to_string(sample.order.messageType) : "null";
		const std::string orderName = sample.hasOrder ? jsonString(sample.order.messageName.c_str()) : "null";
		const std::string payload = "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(object->getID()))
			+ ",\"template_name\":" + jsonString(object->getTemplate()->getName().str())
			+ ",\"owner_player_index\":" + nullableOwner(object)
			+ ",\"position\":" + position + ",\"orientation\":" + orientation
			+ ",\"layer_id\":" + std::to_string(sample.layerId)
			+ ",\"layer_name\":" + (stableLayerName != nullptr ? jsonString(stableLayerName) : "null")
			+ ",\"layer_name_status\":" + jsonString(layerStatus)
			+ ",\"position_bounds_policy\":" + jsonString(positionBoundsPolicy(object))
			+ ",\"speed_status\":" + jsonString(sample.hasSpeed ? "measured_physics_velocity" : "unavailable_no_physics")
			+ ",\"speed\":" + (sample.hasSpeed ? speed : "null")
			+ ",\"current_state\":" + jsonString(sample.state.classification.c_str())
			+ ",\"current_state_source\":" + jsonString(sample.state.classificationSource.c_str())
			+ ",\"ai_state_id\":" + nullableAiStateId(sample.state)
			+ ",\"ai_state_name\":" + nullableAiStateName(sample.state)
			+ ",\"ai_state_name_status\":" + jsonString(aiStateNameStatus(sample.state))
			+ ",\"locomotor_set_id\":" + nullableLocomotorSet(sample.state)
			+ ",\"locomotor_set_name\":" + nullableLocomotorSetName(sample.state)
			+ ",\"locomotor_set_name_status\":" + jsonString(locomotorSetNameStatus(sample.state))
			+ ",\"current_locomotor_template_name\":" + currentLocomotorTemplateName(object)
			+ ",\"current_order_id\":" + orderId
			+ ",\"current_order_message_type\":" + orderType
			+ ",\"current_order_message_name\":" + orderName
			+ ",\"path_goal_status\":" + jsonString(sample.pathGoalStatus.c_str())
			+ ",\"path_goal\":" + pathGoal
			+ ",\"is_mobile\":" + (sample.mobile ? "true" : "false")
			+ ",\"is_structure\":" + (sample.structure ? "true" : "false")
			+ ",\"is_disabled\":" + (sample.disabled ? "true" : "false")
			+ ",\"is_engine_moving\":" + (sample.state.engineMoving ? "true" : "false")
			+ ",\"sample_reason\":" + jsonString(reason) + "}";
		ReplayTelemetry::emit(frame, "entity_sample", AsciiString(payload.c_str()));
		return TRUE;
	}

	void pruneDeadState()
	{
		// TheSuperHackers @feature Leex 20/08/2026 Retire every copied ID-indexed observation when no current live object owns that ID. (#TBD)
		for (SampleStateMap::iterator it = s_sampleStates.begin(); it != s_sampleStates.end();)
		{
			Object *object = TheGameLogic->findObjectByID(it->first);
			if (object == nullptr || object->isDestroyed())
			{
				s_currentOrders.erase(it->first);
				s_forcedSamples.erase(it->first);
				it = s_sampleStates.erase(it);
			}
			else
			{
				++it;
			}
		}
		for (OrderReferenceMap::iterator it = s_currentOrders.begin(); it != s_currentOrders.end();)
		{
			Object *object = TheGameLogic->findObjectByID(it->first);
			if (object == nullptr || object->isDestroyed())
			{
				it = s_currentOrders.erase(it);
			}
			else
			{
				++it;
			}
		}
		for (ForcedSampleMap::iterator it = s_forcedSamples.begin(); it != s_forcedSamples.end();)
		{
			Object *object = TheGameLogic->findObjectByID(it->first);
			if (object == nullptr || object->isDestroyed())
			{
				it = s_forcedSamples.erase(it);
			}
			else
			{
				++it;
			}
		}
	}
}

void ReplayMovementSampler::reset()
{
	// TheSuperHackers @feature Leex 20/08/2026 Clear copied sampler state so replay resets cannot retain unsafe entity history. (#TBD)
	s_sampleStates.clear();
	s_currentOrders.clear();
	s_forcedSamples.clear();
	s_nextOrderId = 1;
}

AsciiString ReplayMovementSampler::orderCoverageJson()
{
	std::string commands("[");
	for (size_t index = 0; index < sizeof(s_supportedOrders) / sizeof(s_supportedOrders[0]); ++index)
	{
		const SupportedOrder &supported = s_supportedOrders[index];
		if (index != 0)
		{
			commands.push_back(',');
		}
		commands += "{\"message_type\":" + std::to_string(static_cast<Int>(supported.messageType))
			+ ",\"message_name\":" + jsonString(supported.messageName)
			+ ",\"target_kind\":" + jsonString(targetKindName(supported.targetKind))
			+ ",\"target_argument_index\":"
			+ (supported.targetArgumentIndex >= 0 ? std::to_string(supported.targetArgumentIndex) : "null") + "}";
	}
	commands.push_back(']');
	// TheSuperHackers @feature Leex 20/08/2026 Publish the post-resolution seam and current-reference provenance for every supported command family. (#TBD)
	const std::string result = "{\"coverage\":\"closed_supported_subset\""
		",\"dispatch_seam\":\"GameLogic::logicMessageDispatcher_post_resolution\""
		",\"command_frame_source\":\"GameLogic::getFrame\""
		",\"source_player_policy\":\"message_player_resolved_to_engine_player\""
		",\"selected_reference_policy\":\"current_live_post_dispatch_source_order\""
		",\"target_reference_policy\":\"current_live_post_dispatch\""
		",\"historical_provenance_policy\":\"order_facts_remain_historical_after_entity_destruction\""
		",\"sample_order_reference_policy\":\"last_supported_post_dispatch_order_not_execution_state\""
		",\"supported_commands\":" + commands + "}";
	return AsciiString(result.c_str());
}

// TheSuperHackers @feature Leex 20/08/2026 Emit only post-dispatch orders whose selected and target objects resolve as current engine entities. (#TBD)
void ReplayMovementSampler::observeResolvedOrder(const GameMessage *message, const Player *sourcePlayer,
	const VecObjectID &selectedObjectIds)
{
	if (!ReplayTelemetry::isInitialized() || TheRecorder == nullptr || !TheRecorder->isPlaybackMode()
		|| message == nullptr || sourcePlayer == nullptr || selectedObjectIds.empty() || TheGameLogic == nullptr)
	{
		return;
	}
	const SupportedOrder *supported = supportedOrder(message->getType());
	if (supported == nullptr)
	{
		return;
	}
	const char *runtimeMessageName = message->getCommandAsString();
	if (runtimeMessageName == nullptr || std::strcmp(runtimeMessageName, supported->messageName) != 0)
	{
		ReplayTelemetry::fail("order_command_catalog_mismatch", "runtime command name differs from closed supported-order catalog");
		return;
	}
	if (supported->targetArgumentIndex >= 0
		&& message->getArgumentCount() <= static_cast<UnsignedByte>(supported->targetArgumentIndex))
	{
		ReplayTelemetry::fail("order_argument_missing", "supported order is missing its source-defined target argument");
		return;
	}
	const GameMessageArgumentDataType expectedArgument = supported->targetKind == ORDER_TARGET_OBJECT
		? ARGUMENTDATATYPE_OBJECTID : ARGUMENTDATATYPE_LOCATION;
	if (supported->targetArgumentIndex >= 0
		&& message->getArgumentDataType(supported->targetArgumentIndex) != expectedArgument)
	{
		ReplayTelemetry::fail("order_argument_type_mismatch", "supported order target argument has an unexpected engine type");
		return;
	}

	std::set<ObjectID> uniqueSelectedIds;
	std::string selectedIds("[");
	std::string selectedEntities("[");
	for (size_t index = 0; index < selectedObjectIds.size(); ++index)
	{
		const ObjectID objectId = selectedObjectIds[index];
		Object *object = TheGameLogic->findObjectByID(objectId);
		if (object == nullptr || object->isDestroyed() || object->getControllingPlayer() != sourcePlayer
			|| !uniqueSelectedIds.insert(objectId).second)
		{
			ReplayTelemetry::fail("order_selected_entity_unresolved", "selected order entity was not uniquely live and owned post-dispatch");
			return;
		}
		ReplayEntityLifecycle::ensureObjectCreated(object);
		if (index != 0)
		{
			selectedIds.push_back(',');
			selectedEntities.push_back(',');
		}
		selectedIds += std::to_string(static_cast<UnsignedInt>(objectId));
		selectedEntities += "{\"object_id\":" + std::to_string(static_cast<UnsignedInt>(objectId))
			+ ",\"template_name\":" + jsonString(object->getTemplate()->getName().str()) + "}";
	}
	selectedIds.push_back(']');
	selectedEntities.push_back(']');

	std::string targetObjectId("null");
	std::string targetTemplateName("null");
	std::string targetLocation("null");
	if (supported->targetKind == ORDER_TARGET_OBJECT)
	{
		const ObjectID objectId = message->getArgument(supported->targetArgumentIndex)->objectID;
		Object *target = TheGameLogic->findObjectByID(objectId);
		if (target == nullptr || target->isDestroyed())
		{
			return;
		}
		ReplayEntityLifecycle::ensureObjectCreated(target);
		targetObjectId = std::to_string(static_cast<UnsignedInt>(objectId));
		targetTemplateName = jsonString(target->getTemplate()->getName().str());
	}
	else if (supported->targetKind == ORDER_TARGET_LOCATION
		&& !positionJson(message->getArgument(supported->targetArgumentIndex)->location, targetLocation))
	{
		return;
	}

	const UnsignedInt orderId = s_nextOrderId++;
	const UnsignedInt frame = TheGameLogic->getFrame();
	const std::string payload = "{\"order_id\":" + std::to_string(orderId)
		+ ",\"command_frame\":" + std::to_string(frame)
		+ ",\"message_type\":" + std::to_string(static_cast<Int>(supported->messageType))
		+ ",\"message_name\":" + jsonString(supported->messageName)
		+ ",\"source_player_index\":" + std::to_string(sourcePlayer->getPlayerIndex())
		+ ",\"selected_object_ids\":" + selectedIds
		+ ",\"selected_entities\":" + selectedEntities
		+ ",\"target_kind\":" + jsonString(targetKindName(supported->targetKind))
		+ ",\"target_object_id\":" + targetObjectId
		+ ",\"target_template_name\":" + targetTemplateName
		+ ",\"target_location\":" + targetLocation
		+ ",\"command_source\":\"recorded_network_player_command\""
		+ ",\"ai_command_source_id\":0,\"ai_command_source_name\":\"CMD_FROM_PLAYER\"}";
	ReplayTelemetry::emit(frame, "order_issued", AsciiString(payload.c_str()));
	const OrderReference reference = { orderId, static_cast<Int>(supported->messageType), supported->messageName };
	for (const ObjectID objectId : selectedObjectIds)
	{
		s_currentOrders[objectId] = reference;
		s_forcedSamples[objectId] |= FORCE_ORDER;
	}
}

// TheSuperHackers @feature Leex 20/08/2026 Emit deterministic changed or bounded-heartbeat snapshots without retaining live engine pointers. (#TBD)
void ReplayMovementSampler::sampleEndOfFrame()
{
	if (!ReplayTelemetry::isInitialized() || TheGameLogic == nullptr)
	{
		return;
	}
	std::vector<ObjectID> objectIds;
	for (Object *object = TheGameLogic->getFirstObject(); object != nullptr; object = object->getNextObject())
	{
		if (!object->isDestroyed())
		{
			objectIds.push_back(object->getID());
		}
	}
	std::sort(objectIds.begin(), objectIds.end());
	const UnsignedInt frame = TheGameLogic->getFrame();
	const UnsignedInt interval = static_cast<UnsignedInt>(ReplayTelemetry::getMovementSampleFrames());
	for (const ObjectID objectId : objectIds)
	{
		Object *object = TheGameLogic->findObjectByID(objectId);
		if (object == nullptr || object->isDestroyed())
		{
			continue;
		}
		ReplayEntityLifecycle::ensureObjectCreated(object);
		SampleState &retained = s_sampleStates[objectId];
		const EngineSampleSnapshot current = engineSample(object);
		const Bool stateChanged = retained.hasState && !sameState(retained.state, current.state);
		if (stateChanged)
		{
			emitStateTransition(frame, object, retained.state, current.state);
			s_forcedSamples[objectId] |= FORCE_STATE;
		}
		retained.state = current.state;
		retained.hasState = TRUE;

		const ForcedSampleMap::const_iterator forcedSample = s_forcedSamples.find(objectId);
		const UnsignedInt forced = forcedSample != s_forcedSamples.end() ? forcedSample->second : 0;
		const Bool firstSample = !retained.hasSample;
		const Bool changed = retained.hasSample && !sameSample(retained.sample, current);
		const UnsignedInt gap = retained.hasSample ? frame - retained.lastSampleFrame : 0;
		const Bool eligibleMobile = current.mobile && !current.structure && !current.disabled;
		const Bool changedAtInterval = retained.hasSample && eligibleMobile && changed && gap >= interval;
		const Bool heartbeat = retained.hasSample && eligibleMobile && current.state.engineMoving && gap >= interval;
		const Bool shouldSample = firstSample || forced != 0 || changedAtInterval || heartbeat;
		if (!shouldSample)
		{
			continue;
		}
		const char *reason = (forced & FORCE_STATE) != 0 ? "state_forced"
			: ((forced & FORCE_ORDER) != 0 ? "order_forced"
				: (firstSample ? "lifecycle_forced" : (changedAtInterval ? "changed" : "periodic_moving_heartbeat")));
		if (emitSample(frame, object, current, reason))
		{
			retained.sample = current;
			retained.hasSample = TRUE;
			retained.lastSampleFrame = frame;
			s_forcedSamples.erase(objectId);
		}
	}
	pruneDeadState();
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
