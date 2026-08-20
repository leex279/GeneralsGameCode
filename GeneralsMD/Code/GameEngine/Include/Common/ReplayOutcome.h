#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "Common/ReplayTelemetry.h"

// TheSuperHackers @feature Leex 20/08/2026 Export a passive telemetry-independent replay termination summary. (#TBD)
class ReplayOutcome
{
public:
	static void configure(const AsciiString &outputPath);
	static Bool isEnabled();
	static void observeExecutedCommand();
	static void observeCRCMismatch(UnsignedInt frame);
	static void finish(UnsignedInt finalFrame, ReplayTelemetryTerminationReason reason);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
