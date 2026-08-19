#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Lib/BaseType.h"

// TheSuperHackers @feature Leex 18/08/2026 Export immutable semantic engine metadata and resolved replay players passively. (#TBD)
class ReplayGameDataExport
{
public:
	static void reset();
	static Bool prepareCatalog();
	static void emitPlayersInitialized();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
