#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "GameLogic/AI.h"

class GameMessage;
class Player;

// TheSuperHackers @feature Leex 20/08/2026 Export source-grounded replay orders and bounded engine movement snapshots without retaining Object pointers. (#TBD)
class ReplayMovementSampler
{
public:
	static void reset();
	static void observeResolvedOrder(const GameMessage *message, const Player *sourcePlayer,
		const VecObjectID &selectedObjectIds);
	static void sampleEndOfFrame();
	static AsciiString orderCoverageJson();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
