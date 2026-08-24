#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayCRCDiagnostics.h"

#include "Common/AsciiString.h"
#include "Common/ReplayTelemetry.h"

#include <string>

namespace
{
	unsigned long long s_pairIndex = 0;
}

void ReplayCRCDiagnostics::reset()
{
	s_pairIndex = 0;
}

void ReplayCRCDiagnostics::observePair(UnsignedInt computedFrame, UnsignedInt computedCRC,
	UnsignedInt recordedReceiveFrame, UnsignedInt recordedCRC, Int queueDepth, Int localPlayerIndex)
{
	if (!ReplayTelemetry::isInitialized())
	{
		return;
	}
	const Bool matches = computedCRC == recordedCRC;
	const std::string payload = "{\"pair_index\":" + std::to_string(s_pairIndex++)
		+ ",\"computed_frame\":" + std::to_string(computedFrame)
		+ ",\"computed_crc\":" + std::to_string(computedCRC)
		+ ",\"recorded_receive_frame\":" + std::to_string(recordedReceiveFrame)
		+ ",\"recorded_crc\":" + std::to_string(recordedCRC)
		+ ",\"match\":" + (matches ? "true" : "false")
		+ ",\"queue_depth\":" + std::to_string(queueDepth)
		+ ",\"local_player_index\":" + std::to_string(localPlayerIndex) + "}";
	ReplayTelemetry::emit(recordedReceiveFrame, "crc_pair", AsciiString(payload.c_str()));
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
