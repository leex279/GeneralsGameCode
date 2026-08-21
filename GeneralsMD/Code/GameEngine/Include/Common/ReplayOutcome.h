#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "Common/ReplayTelemetry.h"

// TheSuperHackers @feature Leex 20/08/2026 Preserve source-grounded failures before replay playback becomes ready. (#TBD)
enum ReplayOutcomeStartupFailureReason
{
	REPLAY_OUTCOME_INPUT_UNAVAILABLE,
	REPLAY_OUTCOME_INVALID_REPLAY_HEADER,
	REPLAY_OUTCOME_TRUNCATED_INPUT,
	REPLAY_OUTCOME_STARTUP_FAILED,
};

// TheSuperHackers @feature Leex 20/08/2026 Export a passive telemetry-independent replay termination summary. (#TBD)
class ReplayOutcome
{
public:
	static void configure(const AsciiString &outputPath);
	static Bool isEnabled();
	static void beginAttempt();
	static void observePlaybackStarted();
	static void finishStartupFailure(ReplayOutcomeStartupFailureReason reason);
	static void observeExecutedCommand();
	static void observeCRCMismatch(UnsignedInt frame);
	static void finish(UnsignedInt finalFrame, ReplayTelemetryTerminationReason reason);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
