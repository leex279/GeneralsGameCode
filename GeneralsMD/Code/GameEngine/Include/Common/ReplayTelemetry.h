#pragma once

#if defined(RTS_REPLAY_ANALYZER)

#include "Common/AsciiString.h"
#include "Common/Recorder.h"

// TheSuperHackers @feature Leex 18/08/2026 Provide one passive modern-only sink for authoritative replay telemetry. (#TBD)
class ReplayTelemetry
{
public:
	static void configure(const AsciiString &tracePath, const AsciiString &runId, Int movementSampleFrames);
	static Bool isEnabled();
	static void begin(const RecorderClass::ReplayHeader &header);
	static void observeExecutedCommand();
	static void emit(UnsignedInt frame, const char *eventType, const AsciiString &payloadJson);
	static void finish(UnsignedInt finalFrame, Bool cleanShutdown);
	static void fail(const char *code, const char *message);
};

#endif // defined(RTS_REPLAY_ANALYZER)
