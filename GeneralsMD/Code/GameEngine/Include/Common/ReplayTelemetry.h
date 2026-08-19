#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "Common/Recorder.h"

#include <cstddef>

// TheSuperHackers @feature Leex 18/08/2026 Provide one passive modern-only sink for authoritative replay telemetry. (#TBD)
class ReplayTelemetry
{
public:
	static void configure(const AsciiString &tracePath, const AsciiString &runId, Int movementSampleFrames);
	static Bool isEnabled();
	static const AsciiString &getTracePath();
	static const AsciiString &getEngineDataIdentity();
	static Int getReplayLocalSlotIndex();
	static AsciiString sha256Hex(const char *data, size_t length);
	static void setGameDataCatalog(const AsciiString &path, const AsciiString &sha256,
		const AsciiString &engineDataIdentity);
	static void begin(const RecorderClass::ReplayHeader &header);
	static void initialize();
	static void observeExecutedCommand();
	static void emit(UnsignedInt frame, const char *eventType, const AsciiString &payloadJson);
	static void deferCleanFinish();
	static void finishDeferred(UnsignedInt finalFrame);
	static void finish(UnsignedInt finalFrame, Bool cleanShutdown);
	static void fail(const char *code, const char *message);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
