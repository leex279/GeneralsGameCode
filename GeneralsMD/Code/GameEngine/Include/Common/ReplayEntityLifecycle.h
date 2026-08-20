#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Lib/BaseType.h"

class Object;
class Team;

enum ReplayEntityCreationSource
{
	REPLAY_ENTITY_CREATION_UNKNOWN,
	REPLAY_ENTITY_CREATION_MAP_LOADED,
	REPLAY_ENTITY_CREATION_STARTING_OBJECT,
	REPLAY_ENTITY_CREATION_PLAYER_PRODUCTION
};

// TheSuperHackers @feature Leex 20/08/2026 Preserve authoritative object-creation call-site context without changing simulation state. (#TBD)
class ReplayEntityCreationScope
{
public:
	explicit ReplayEntityCreationScope(ReplayEntityCreationSource source);
	~ReplayEntityCreationScope();
	void observeReturned(const Object *object);

private:
	ReplayEntityCreationSource m_source;
};

// TheSuperHackers @feature Leex 20/08/2026 Buffer immutable entity lifecycle observations until the v2 manifest and players are published. (#TBD)
class ReplayEntityLifecycle
{
public:
	static void reset();
	static void observeRegistered(const Object *object);
	static void observePositionSet(const Object *object);
	static void observeTransform(const Object *object, Bool positionChanged);
	static void observeTeamChanged(const Object *object, const Team *previousTeam, const Team *newTeam);
	static void observeStatusChanged(const Object *object, Bool previousUnderConstruction,
		Bool newUnderConstruction, Bool previousSold, Bool newSold);
	static void observeDestroyed(const Object *object);
	static void initialize();
	static void flushPendingCreations();
	static void ensureObjectCreated(const Object *object);

private:
	friend class ReplayEntityCreationScope;
	static void beginDirectCreation();
	static void endDirectCreation();
	static void markCreationSource(const Object *object, ReplayEntityCreationSource source);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
