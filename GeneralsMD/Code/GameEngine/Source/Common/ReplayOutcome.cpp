#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayOutcome.h"

#include <cerrno>
#include <cstdio>
#include <string>

namespace
{
	AsciiString s_outputPath;
	unsigned long long s_commandCount = 0;
	UnsignedInt s_crcMismatchFrame = 0;
	UnsignedInt s_tempCounter = 0;
	Bool s_crcMismatchObserved = FALSE;
	Bool s_finished = FALSE;
	Bool s_attemptActive = FALSE;
	Bool s_playbackStarted = FALSE;

	const char *terminationReason(ReplayTelemetryTerminationReason reason)
	{
		switch (reason)
		{
			case REPLAY_TELEMETRY_TERMINATION_CLEAN_EOF: return "clean_completion";
			case REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH: return "crc_mismatch";
			case REPLAY_TELEMETRY_TERMINATION_TRUNCATED_INPUT: return "replay_truncated";
			case REPLAY_TELEMETRY_TERMINATION_INTERRUPTED: return "interrupted";
		}
		return "interrupted";
	}

	const char *startupFailureReason(ReplayOutcomeStartupFailureReason reason)
	{
		switch (reason)
		{
			case REPLAY_OUTCOME_INPUT_UNAVAILABLE: return "input_unavailable";
			case REPLAY_OUTCOME_INVALID_REPLAY_HEADER: return "invalid_replay_header";
			case REPLAY_OUTCOME_TRUNCATED_INPUT: return "truncated_input";
		}
		return "invalid_replay_header";
	}

	void diagnostic(const char *message)
	{
		fprintf(stderr, "ReplayOutcome: %s\n", message);
		fflush(stderr);
	}

	void publish(const std::string &payload)
	{
		AsciiString tempPath;
		FILE *output = nullptr;
		for (Int attempt = 0; attempt < 100 && output == nullptr; ++attempt)
		{
			tempPath.format("%s.tmp.%lu.%u", s_outputPath.str(),
				static_cast<unsigned long>(GetCurrentProcessId()), ++s_tempCounter);
			errno = 0;
			output = fopen(tempPath.str(), "wbx");
			if (output == nullptr && errno != EEXIST)
			{
				break;
			}
		}
		if (output == nullptr)
		{
			diagnostic("could not create an exclusive outcome transaction");
			return;
		}

		const size_t written = fwrite(payload.data(), 1, payload.size(), output);
		const Bool writeFailed = written != payload.size() || fflush(output) != 0;
		const Bool closeFailed = fclose(output) != 0;
		if (writeFailed || closeFailed)
		{
			diagnostic("could not write and close the complete outcome transaction");
			remove(tempPath.str());
			return;
		}
		// TheSuperHackers @feature Leex 20/08/2026 Publish without replacement so a late collision remains caller-owned. (#TBD)
		if (!MoveFileA(tempPath.str(), s_outputPath.str()))
		{
			diagnostic("could not exclusively publish replay outcome");
			remove(tempPath.str());
		}
	}

	// TheSuperHackers @feature Leex 20/08/2026 Serialize playback readiness with every terminal outcome transaction. (#TBD)
	void publishFinishedOutcome(UnsignedInt finalFrame, const char *reason, Bool crcMismatch)
	{
		// TheSuperHackers @feature Leex 21/08/2026 Version the independent outcome contract before strict runner ingestion. (#TBD)
		const std::string payload = "{\"schema_version\":1,\"playback_started\":"
			+ std::string(s_playbackStarted ? "true" : "false")
			+ ",\"final_frame\":" + std::to_string(finalFrame)
			+ ",\"command_count\":" + std::to_string(s_commandCount)
			+ ",\"terminal_reason\":\"" + reason + "\""
			+ ",\"crc_mismatch\":" + (crcMismatch ? "true" : "false")
			+ ",\"crc_mismatch_frame\":"
			+ (crcMismatch && s_crcMismatchObserved ? std::to_string(s_crcMismatchFrame) : "null") + "}\n";
		publish(payload);
	}
}

void ReplayOutcome::configure(const AsciiString &outputPath)
{
	s_outputPath = outputPath;
	s_commandCount = 0;
	s_crcMismatchFrame = 0;
	s_crcMismatchObserved = FALSE;
	s_finished = FALSE;
	s_attemptActive = FALSE;
	s_playbackStarted = FALSE;
}

Bool ReplayOutcome::isEnabled()
{
	return s_outputPath.isNotEmpty();
}

void ReplayOutcome::beginAttempt()
{
	if (!isEnabled())
	{
		return;
	}
	// TheSuperHackers @feature Leex 20/08/2026 Start one outcome transaction before opening or decoding the configured replay. (#TBD)
	s_commandCount = 0;
	s_crcMismatchFrame = 0;
	s_crcMismatchObserved = FALSE;
	s_finished = FALSE;
	s_attemptActive = TRUE;
	s_playbackStarted = FALSE;
}

void ReplayOutcome::observePlaybackStarted()
{
	if (s_attemptActive && !s_finished)
	{
		// TheSuperHackers @feature Leex 20/08/2026 Mark playback ready only after setup and the first command frame have decoded. (#TBD)
		s_playbackStarted = TRUE;
	}
}

void ReplayOutcome::finishStartupFailure(ReplayOutcomeStartupFailureReason reason)
{
	if (!s_attemptActive || s_finished)
	{
		return;
	}
	// TheSuperHackers @feature Leex 20/08/2026 Atomically settle failures that occur before the normal telemetry completion seam exists. (#TBD)
	s_finished = TRUE;
	publishFinishedOutcome(0, startupFailureReason(reason), FALSE);
}

void ReplayOutcome::observeExecutedCommand()
{
	if (s_attemptActive && !s_finished)
	{
		++s_commandCount;
	}
}

void ReplayOutcome::observeCRCMismatch(UnsignedInt frame)
{
	if (s_attemptActive && !s_crcMismatchObserved)
	{
		s_crcMismatchObserved = TRUE;
		s_crcMismatchFrame = frame;
	}
}

void ReplayOutcome::finish(UnsignedInt finalFrame, ReplayTelemetryTerminationReason reason)
{
	if (!s_attemptActive || s_finished)
	{
		return;
	}
	s_finished = TRUE;
	const Bool crcMismatch = reason == REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH;
	publishFinishedOutcome(finalFrame, terminationReason(reason), crcMismatch);
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)
