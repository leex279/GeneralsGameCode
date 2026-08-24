#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Lib/BaseType.h"

// TheSuperHackers @feature Leex 24/08/2026 Export passive replay CRC pair evidence without affecting authoritative comparison decisions. (#TBD)
class ReplayCRCDiagnostics
{
public:
	static void reset();
	static void observePair(UnsignedInt computedFrame, UnsignedInt computedCRC,
		UnsignedInt recordedReceiveFrame, UnsignedInt recordedCRC, Int queueDepth, Int localPlayerIndex);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
