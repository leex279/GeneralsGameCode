#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Lib/BaseType.h"

// TheSuperHackers @feature Leex 23/08/2026 Export raw terminal ScoreKeeper totals without invoking score calculation. (#0)
class ReplayScoreKeeper
{
public:
	static void reset();
	static void initialize();
	static void writeTerminalSnapshot(Int frame);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
