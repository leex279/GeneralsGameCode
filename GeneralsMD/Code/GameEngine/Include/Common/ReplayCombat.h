#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "Common/GameCommon.h"
#include "Common/Recorder.h"
#include "Common/ReplayTelemetry.h"

class DamageInfo;
class Object;
class Player;

enum ReplayPlayerTransitionType
{
	REPLAY_PLAYER_TRANSITION_NONE,
	REPLAY_PLAYER_DEFEATED,
	REPLAY_PLAYER_SCRIPT_DEFEATED,
	REPLAY_PLAYER_SURRENDERED,
	REPLAY_PLAYER_DISCONNECTED
};

// TheSuperHackers @feature Leex 20/08/2026 Carry one authoritative player-terminal cause across the existing killPlayer transition. (#TBD)
class ReplayPlayerTransitionScope
{
public:
	explicit ReplayPlayerTransitionScope(ReplayPlayerTransitionType type);
	~ReplayPlayerTransitionScope();

private:
	ReplayPlayerTransitionScope(const ReplayPlayerTransitionScope &) = delete;
	ReplayPlayerTransitionScope &operator=(const ReplayPlayerTransitionScope &) = delete;
};

// TheSuperHackers @feature Leex 20/08/2026 Export passive combat and authoritative replay-terminal observations without retaining engine pointers. (#TBD)
class ReplayCombat
{
public:
	static void reset();
	static void observeReplayHeader(const RecorderClass::ReplayHeader &header);
	static void initialize();
	static void observeCRCMismatch(UnsignedInt frame);
	static void observeDamage(const Object *victim, const DamageInfo *damageInfo,
		Real priorHealth, Real newHealth);
	static void observeHealing(const Object *target, const DamageInfo *damageInfo,
		Real priorHealth, Real newHealth);
	static void observeVeterancy(const Object *object, VeterancyLevel previousLevel, VeterancyLevel newLevel);
	static void observePlayerTerminalTransition(const Player *player);
	static void emitMatchOutcome(UnsignedInt finalFrame, ReplayTelemetryTerminationReason reason);
	static AsciiString completionFieldsJson(ReplayTelemetryTerminationReason reason);

private:
	friend class ReplayPlayerTransitionScope;
	static void pushPlayerTransition(ReplayPlayerTransitionType type);
	static void popPlayerTransition();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
