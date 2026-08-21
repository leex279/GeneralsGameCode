#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"

// TheSuperHackers @feature Leex 21/08/2026 Export one immutable initialized map snapshot for replay analytics. (#TBD)
class ReplayMapExport
{
public:
	static void reset();
	static Bool prepare();
	static const AsciiString &referenceJson();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
